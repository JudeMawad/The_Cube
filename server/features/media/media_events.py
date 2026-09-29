"""Version-checked Arr webhook parsing and a durable, acknowledged outbox.

Radarr 6.3.0.10514 / Sonarr 4.0.19.2979 use Grab and Download.
Download means imported, including Sonarr's grouped import-complete payload.
Only normalized identities are stored; raw webhook bodies are never persisted.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import secrets
import sqlite3
from threading import Condition
import time

from .media_wording import download_started, ready_to_watch

STATE_PATH = Path.home() / ".local/state/cube/media-events.sqlite3"


def positive_id(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class MediaEvent:
    media_type: str
    external_id: int
    episode: str
    stage: str
    event_id: str


def parse_events(service, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("eventType"), str):
        raise ValueError("Invalid webhook envelope")
    kind = payload["eventType"]
    if kind not in {"Grab", "Download"}:
        return []  # Test, rename, delete, health, etc. never speak.
    stage = "started" if kind == "Grab" else "ready"
    movie = service == "radarr"
    media = payload.get("movie" if movie else "series")
    if not isinstance(media, dict):
        raise ValueError("Missing media identity")
    external_id = media.get("tmdbId" if movie else "tvdbId")
    if not positive_id(external_id):
        raise ValueError("Missing external identity")
    if kind == "Download":
        files = [payload.get("movieFile" if movie else "episodeFile")]
        if not movie and "episodeFiles" in payload:
            files = payload["episodeFiles"]
        if (not isinstance(files, list) or not files
                or any(not isinstance(f, dict) or not positive_id(f.get("id")) for f in files)):
            raise ValueError("Missing imported file identity")
    else:
        files = []
    download_id = payload.get("downloadId")
    if download_id is not None and (not isinstance(download_id, str) or len(download_id) > 256):
        raise ValueError("Invalid download identity")
    event_id = download_id or ",".join(str(f["id"]) for f in files)
    episodes = [""]
    if not movie:
        raw_episodes = payload.get("episodes")
        if not isinstance(raw_episodes, list) or not raw_episodes:
            raise ValueError("Missing episodes")
        episodes = []
        for episode in raw_episodes:
            if (not isinstance(episode, dict)
                    or type(episode.get("seasonNumber")) is not int or episode["seasonNumber"] < 0
                    or not positive_id(episode.get("episodeNumber"))):
                raise ValueError("Invalid episode identity")
            episodes.append(f"S{episode['seasonNumber']:02}E{episode['episodeNumber']:02}")
    return [MediaEvent("movie" if movie else "episode", external_id, ep, stage, event_id)
            for ep in sorted(set(episodes))]


class EventStore:
    def __init__(self, path=STATE_PATH, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.changed = Condition()
        self.initialized = False  # Lazy: broken storage must not break /health or lights.

    @contextmanager
    def _db(self):
        # All callers hold changed, so initialization and read/check/write are serialized.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            self.path.chmod(0o600)
            db.execute("PRAGMA foreign_keys=ON")
            if not self.initialized:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS requests (
                        id INTEGER PRIMARY KEY, media_type TEXT NOT NULL,
                        external_id INTEGER NOT NULL, episode TEXT NOT NULL DEFAULT '',
                        title TEXT NOT NULL, year TEXT, requested_at REAL NOT NULL,
                        client_id TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
                        started_announced INTEGER NOT NULL DEFAULT 0,
                        ready_announced INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(media_type, external_id, episode, client_id)
                    );
                    CREATE TABLE IF NOT EXISTS client_movie_context (
                        client_id TEXT PRIMARY KEY, external_id INTEGER,
                        title TEXT, year TEXT, updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS notifications (
                        id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id),
                        stage TEXT NOT NULL, event_id TEXT NOT NULL, response TEXT NOT NULL,
                        ack_token TEXT NOT NULL, delivered_at REAL,
                        UNIQUE(request_id, stage)
                    );
                """)
                self._migrate_cancellation(db)
                self.initialized = True
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _migrate_cancellation(db):
        """Add attempt generations atomically, preserving every old notification ID."""
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            for name, declaration in [
                ("generation", "INTEGER NOT NULL DEFAULT 1"),
                ("overseerr_request_id", "INTEGER"),
                ("cancellation_state", "TEXT NOT NULL DEFAULT 'active'"),
            ]:
                if name not in columns:
                    db.execute(f"ALTER TABLE requests ADD COLUMN {name} {declaration}")
            if "generation" not in {row[1] for row in db.execute("PRAGMA table_info(notifications)")}:
                db.execute("""CREATE TABLE notifications_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    generation INTEGER NOT NULL DEFAULT 1,
                    stage TEXT NOT NULL, event_id TEXT NOT NULL, response TEXT NOT NULL,
                    ack_token TEXT NOT NULL, delivered_at REAL,
                    UNIQUE(request_id, generation, stage))""")
                db.execute("""INSERT INTO notifications_v2
                    (id, request_id, stage, event_id, response, ack_token, delivered_at)
                    SELECT id, request_id, stage, event_id, response, ack_token, delivered_at FROM notifications""")
                db.execute("DROP TABLE notifications")
                db.execute("ALTER TABLE notifications_v2 RENAME TO notifications")
            db.execute("""CREATE TABLE IF NOT EXISTS movie_cancellations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL REFERENCES requests(id), generation INTEGER NOT NULL,
                target_json TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'prepared',
                outcome TEXT NOT NULL DEFAULT 'pending', updated_at REAL NOT NULL,
                UNIQUE(request_id, generation))""")
            db.execute("""CREATE TABLE IF NOT EXISTS cancelled_downloads (
                external_id INTEGER NOT NULL, download_id TEXT NOT NULL,
                PRIMARY KEY(external_id, download_id))""")

    def request_record(self, client_id, media_id):
        with self.changed, self._db() as db:
            row = db.execute("""SELECT * FROM requests WHERE client_id=? AND external_id=?
                AND media_type='movie'""", (client_id, media_id)).fetchone()
            return dict(row) if row else None

    def owned_request(self, client_id, media_id):
        row = self.request_record(client_id, media_id)
        return row if row and row["confirmed"] else None

    def owned_movies(self, client_id):
        with self.changed, self._db() as db:
            return [self._movie(row) for row in db.execute("""SELECT * FROM requests
                WHERE client_id=? AND media_type='movie' AND confirmed=1 ORDER BY requested_at DESC""",
                (client_id,))]

    def has_other_owner(self, client_id, media_id):
        with self.changed, self._db() as db:
            return db.execute("""SELECT 1 FROM requests WHERE external_id=? AND media_type='movie'
                AND client_id!=? AND confirmed=1 AND cancellation_state!='cancelled' LIMIT 1""",
                (media_id, client_id)).fetchone() is not None

    def cancellation(self, client_id, media_id):
        with self.changed, self._db() as db:
            row = db.execute("""SELECT c.* FROM movie_cancellations c JOIN requests r ON r.id=c.request_id
                WHERE r.client_id=? AND r.external_id=? AND r.media_type='movie'
                ORDER BY c.generation DESC LIMIT 1""", (client_id, media_id)).fetchone()
            return {**dict(row), "target": json.loads(row["target_json"])} if row else None

    def begin_cancellation(self, client_id, target):
        with self.changed, self._db() as db:
            row = db.execute("""SELECT * FROM requests WHERE id=? AND client_id=? AND generation=?
                AND confirmed=1""", (target["request_id"], client_id, target["generation"])).fetchone()
            if not row or row["external_id"] != target["media_id"]:
                raise ValueError("Request identity changed")
            db.execute("""INSERT INTO movie_cancellations
                (request_id, generation, target_json, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(request_id, generation) DO UPDATE SET
                target_json=excluded.target_json, updated_at=excluded.updated_at,
                stage='prepared', outcome='pending'""",
                (row["id"], row["generation"], json.dumps(target), self.clock()))
            db.execute("UPDATE requests SET cancellation_state='pending' WHERE id=?", (row["id"],))
            identities = [item.get("downloadId") for item in target["queue"]]
            identities += [item[0] for item in db.execute("""SELECT event_id FROM notifications
                WHERE request_id=? AND generation=?""", (row["id"], row["generation"]))]
            for identity in identities:
                if identity:
                    db.execute("INSERT OR IGNORE INTO cancelled_downloads VALUES (?, ?)",
                               (row["external_id"], identity))
            return db.execute("SELECT id FROM movie_cancellations WHERE request_id=? AND generation=?",
                              (row["id"], row["generation"])).fetchone()[0]

    def cancellation_progress(self, operation_id, stage, outcome):
        with self.changed, self._db() as db:
            row = db.execute("SELECT * FROM movie_cancellations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise ValueError("Cancellation not found")
            db.execute("UPDATE movie_cancellations SET stage=?, outcome=?, updated_at=? WHERE id=?",
                       (stage, outcome, self.clock(), operation_id))
            state = "cancelled" if outcome == "complete" else outcome
            db.execute("UPDATE requests SET cancellation_state=? WHERE id=? AND generation=?",
                       (state, row["request_id"], row["generation"]))

    def event_valid(self, client_id, event_id, token):
        with self.changed, self._db() as db:
            row = db.execute("""SELECT n.ack_token FROM notifications n JOIN requests r ON r.id=n.request_id
                WHERE n.id=? AND r.client_id=? AND r.confirmed=1 AND r.cancellation_state='active'
                AND n.generation=r.generation AND n.delivered_at IS NULL""", (event_id, client_id)).fetchone()
            return bool(row and secrets.compare_digest(row["ack_token"], token))

    def remember_movie(self, client_id, movie):
        """A NULL identity is an explicit forget, distinct from no saved reference."""
        with self.changed, self._db() as db:
            db.execute("""INSERT INTO client_movie_context
                (client_id, external_id, title, year, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET external_id=excluded.external_id,
                title=excluded.title, year=excluded.year, updated_at=excluded.updated_at""",
                (client_id, movie.media_id if movie else None,
                 movie.title if movie else None, movie.year if movie else None, self.clock()))

    @staticmethod
    def _movie(row):
        from integrations.overseerr_client import Movie
        return Movie(row["external_id"], row["title"], row["year"]) if row and row["external_id"] else None

    def last_requested_movie(self, client_id):
        with self.changed, self._db() as db:
            return self._movie(db.execute("""SELECT * FROM requests WHERE client_id=?
                AND media_type='movie' AND confirmed=1 ORDER BY requested_at DESC, id DESC LIMIT 1""",
                (client_id,)).fetchone())

    def remembered_movie(self, client_id):
        with self.changed, self._db() as db:
            row = db.execute("SELECT * FROM client_movie_context WHERE client_id=?",
                             (client_id,)).fetchone()
            if row is None:
                row = db.execute("""SELECT * FROM requests WHERE client_id=?
                    AND media_type='movie' AND confirmed=1 ORDER BY requested_at DESC, id DESC LIMIT 1""",
                    (client_id,)).fetchone()
            return self._movie(row)

    def movie_milestone(self, client_id, media_id):
        with self.changed, self._db() as db:
            row = db.execute("""SELECT n.stage FROM notifications n JOIN requests r ON r.id=n.request_id
                WHERE r.client_id=? AND r.media_type='movie' AND r.external_id=? AND r.confirmed=1
                AND n.generation=r.generation AND r.cancellation_state='active'
                ORDER BY CASE n.stage WHEN 'ready' THEN 0 ELSE 1 END LIMIT 1""",
                (client_id, media_id)).fetchone()
            return row["stage"] if row else None

    def prepare_request(self, movie, client_id):
        return self.prepare("movie", movie.media_id, movie.title, movie.year, client_id)

    def prepare(self, media_type, external_id, title, year, client_id, episode=""):
        """Persist intent BEFORE POST; early webhooks wait for confirmed success.

        TV callers must register individual SxxExx identities, never whole-series
        readiness. There is intentionally no HTTP endpoint to fabricate requests.
        """
        if media_type not in {"movie", "episode"} or not positive_id(external_id):
            raise ValueError("Invalid request identity")
        if media_type == "episode" and not episode:
            raise ValueError("Episode identity required")
        with self.changed, self._db() as db:
            db.execute("""INSERT OR IGNORE INTO requests
                (media_type, external_id, episode, title, year, requested_at, client_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (media_type, external_id, episode, title, year, self.clock(), client_id))
            row = db.execute("""SELECT * FROM requests WHERE media_type=?
                AND external_id=? AND episode=? AND client_id=?""",
                (media_type, external_id, episode, client_id)).fetchone()
            if row["cancellation_state"] not in {"active", "cancelled"}:
                raise ValueError("Resolve the outstanding cancellation first")
            if not row["confirmed"] or row["cancellation_state"] == "cancelled":
                # Generations preserve history and make buffered events obsolete.
                has_events = db.execute("SELECT 1 FROM notifications WHERE request_id=? AND generation=? LIMIT 1",
                                        (row["id"], row["generation"])).fetchone()
                advance = bool(has_events or row["cancellation_state"] == "cancelled")
                db.execute("""UPDATE requests SET requested_at=?, confirmed=0, generation=generation+?,
                    overseerr_request_id=NULL, cancellation_state='active', started_announced=0,
                    ready_announced=0 WHERE id=?""", (self.clock(), int(advance), row["id"]))
            return row["id"]

    def confirm_request(self, request_id, overseerr_request_id=None):
        with self.changed:
            with self._db() as db:
                if overseerr_request_id is not None and not positive_id(overseerr_request_id):
                    raise ValueError("Invalid Overseerr request identity")
                db.execute("UPDATE requests SET confirmed=1, overseerr_request_id=COALESCE(?, overseerr_request_id) WHERE id=?",
                           (overseerr_request_id, request_id))
            self.changed.notify_all()

    def accept(self, events):
        created = 0
        with self.changed:
            with self._db() as db:
                for event in events:
                    rows = db.execute("""SELECT * FROM requests WHERE media_type=?
                        AND external_id=? AND episode=?""",
                        (event.media_type, event.external_id, event.episode)).fetchall()
                    for row in rows:
                        if row["cancellation_state"] != "active":
                            continue
                        if row["media_type"] == "movie" and db.execute("SELECT 1 FROM cancelled_downloads WHERE external_id=? AND download_id=?",
                                      (row["external_id"], event.event_id)).fetchone():
                            continue
                        cancelled_before = row["media_type"] == "movie" and db.execute(
                            "SELECT 1 FROM movie_cancellations WHERE request_id=? AND generation<? AND outcome='complete'",
                            (row["id"], row["generation"])).fetchone()
                        if cancelled_before and event.stage == "ready" and not db.execute(
                            """SELECT 1 FROM notifications WHERE request_id=? AND generation=?
                            AND stage='started' AND event_id=?""", (row["id"], row["generation"], event.event_id)
                        ).fetchone():
                            continue  # Re-requests need a fresh matching download identity.
                        # A late/reordered Grab cannot announce a ready movie as downloading.
                        if event.stage == "started" and db.execute(
                            "SELECT 1 FROM notifications WHERE request_id=? AND generation=? AND stage='ready'",
                            (row["id"], row["generation"]),
                        ).fetchone():
                            continue
                        title = row["title"] + (f" {row['episode']}" if row["episode"] else "")
                        text = (download_started(title, row["requested_at"], self.clock(), event.media_type)
                                if event.stage == "started" else ready_to_watch(title))
                        created += db.execute("""INSERT OR IGNORE INTO notifications
                            (request_id, generation, stage, event_id, response, ack_token) VALUES (?, ?, ?, ?, ?, ?)""",
                            (row["id"], row["generation"], event.stage, event.event_id, text, secrets.token_urlsafe(24))).rowcount
            if created:
                self.changed.notify_all()
        return created

    def next_event(self, client_id, wait=25):
        deadline = time.monotonic() + wait
        with self.changed:
            while True:
                with self._db() as db:
                    row = db.execute("""SELECT n.id, n.response, n.ack_token FROM notifications n
                        JOIN requests r ON r.id=n.request_id WHERE r.client_id=? AND r.confirmed=1
                        AND r.cancellation_state='active' AND n.generation=r.generation
                        AND n.delivered_at IS NULL ORDER BY n.id LIMIT 1""", (client_id,)).fetchone()
                if row:
                    return dict(row)  # GET is non-destructive, including across restarts.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.changed.wait(remaining)

    def acknowledge(self, client_id, event_id, token):
        with self.changed, self._db() as db:
            row = db.execute("""SELECT n.* FROM notifications n JOIN requests r ON r.id=n.request_id
                WHERE n.id=? AND r.client_id=? AND r.confirmed=1 AND r.cancellation_state='active'
                AND n.generation=r.generation""", (event_id, client_id)).fetchone()
            if not row or not secrets.compare_digest(row["ack_token"], token):
                return False
            db.execute("UPDATE notifications SET delivered_at=COALESCE(delivered_at, ?) WHERE id=?",
                       (self.clock(), event_id))
            column = "started_announced" if row["stage"] == "started" else "ready_announced"
            db.execute(f"UPDATE requests SET {column}=1 WHERE id=?", (row["request_id"],))
            return True
