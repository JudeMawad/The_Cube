"""Local movie dialogue with short-lived choices and durable status references."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from functools import wraps
import json
from pathlib import Path
import logging
import re
from threading import Lock, local
import time
from uuid import uuid4
from weakref import WeakValueDictionary

from integrations.overseerr_client import Movie, OverseerrClient, OverseerrError, RequestUncertain, upstream_seconds
from .media_wording import request_ack, clarification
from integrations.arr_client import ArrError
from .movie_cancellation import MovieCancellation, CancellationRefused, AlreadyCancelled
from core.response_options import approved_response_options

log = logging.getLogger("movie_commands")
def _load_rules():
    path = Path(__file__).resolve().parents[2] / "core/voice_commands.json"
    rows = json.loads(path.read_text())["commands"]
    return {row["id"]: row for row in rows if row["feature"] == "movie"}


RULES = _load_rules()
YES = set(RULES["yes"]["exact"])
NO = set(RULES["cancel"]["exact"])
REJECT = set(RULES["reject"]["exact"])


def normalize(text):
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower().replace("’", "'")).split())


def polite(text):
    command = normalize(text)
    command = re.sub(r"^(?:(?:can|could|would) you (?:please )?|please )", "", command)
    return re.sub(r" (?:please|thanks|thank you)$", "", command).strip()


@dataclass(frozen=True)
class Intent:
    kind: str
    query: str = ""


def _pattern_match(rule_id, command):
    for pattern in RULES[rule_id].get("patterns", []):
        match = getattr(re, pattern["mode"])(pattern["regex"], command)
        if match:
            return match
    return None


def _exact(rule_id, command):
    return command in RULES[rule_id].get("exact", [])


def parse_intent(command):
    # Ordered classification preserves contextual movie dialogue precedence.
    if _exact("cancel_last", command):
        return Intent("cancel_last")
    if _exact("cancel_download", command):
        return Intent("cancel_download")
    match = _pattern_match("cancel_download", command)
    if match:
        return Intent("cancel_download", match[1])
    for kind in ("forget", "cancel", "reject", "yes", "last", "status"):
        if _exact(kind, command):
            return Intent(kind)
    match = _pattern_match("status", command)
    if match:
        query = match[1] or (match[2] if match.lastindex and match.lastindex >= 2 else None)
        return Intent("status", "" if query in {"it", "that movie", "the movie"} else query)
    match = _pattern_match("correct", command)
    if match:
        return Intent("correct", re.sub(r"^(?:download|get) ", "", match[1]))
    for kind in ("request", "availability"):
        match = _pattern_match(kind, command)
        if match:
            return Intent(kind, match[1] or "")
        if _exact(kind, command):
            return Intent(kind)
    # Device exclusions are guards, not executable voice aliases.
    if (re.search(r"\b(?:lights?|bulbs?|spots?)\b", command)
            and re.search(r"\b(?:on|off|red|green|blue|white|yellow|orange|purple|pink|cyan|percent)\b|\d", command)
            or re.match(r"(?:connect|disconnect)\b.*\bspeaker\b", command)
            or re.fullmatch(r"(?:turn (?:the )?volume (?:up|down)|(?:set )?(?:the )?volume (?:to )?\d+(?: percent)?|mute|unmute)", command)):
        return Intent("device")
    return Intent("answer", command)


@dataclass(frozen=True)
class PendingMovie:
    movies: tuple[Movie, ...]
    expires_at: float
    intent: str = "request"
    state: str = "choose"
    failures: int = 0
    force_confirmation: bool = False
    cancellation_target: dict | None = None
    pending_id: str = field(default_factory=lambda: uuid4().hex)


def reply(action, response, success=True, *, alternatives=(), **extra):
    return {"action": action, "response": response, "success": success,
            "response_options": approved_response_options(response, alternatives), **extra}


def failure_reply(action, response):
    # Retain the full authoritative explanation, including uncertainty and any
    # instructions to check status before retrying. Never infer a failure stage.
    return reply(action, response, False, alternatives=[f"Sorry. {response}"])


def structured_operation(function):
    """Public operations share the outermost synchronous client boundary."""
    @wraps(function)
    def execute(self, client_id, *args, **kwargs):
        return self.run_operation(lambda: function(self, client_id, *args, **kwargs), client_id)
    return execute


class MovieCommands:
    def __init__(self, client=None, timeout=120, clock=time.monotonic, store=None, radarr=None):
        self.client = client if client is not None else OverseerrClient()
        self.timeout, self.clock, self.store = timeout, clock, store
        self.pending, self.recent, self.memory = {}, {}, {}
        self.recent_intent, self.misses = {}, {}
        self.lock = Lock()
        self._operation_owner = local()
        self.client_locks, self.movie_locks = WeakValueDictionary(), WeakValueDictionary()
        # Protect against a second submission while upstream status is catching up.
        self.submitted = {}
        self.cancellations = MovieCancellation(self.client, store, radarr)

    @contextmanager
    def _serial(self, registry, key):
        with self.lock:
            lock = registry.setdefault(key, Lock())
        with lock:
            yield

    def continuation(self, client_id):
        with self._serial(self.client_locks, client_id):
            pending = self.pending.get(client_id)
            expiry = pending.expires_at if pending else self.recent.get(client_id, 0)
            remaining = max(0, expiry - self.clock())
            return {"listen_for_seconds": 10 if remaining else 0, "context_expires_in": remaining}

    def handle(self, text, client_id="cube"):
        return self.run_operation(lambda: self._handle(polite(text), client_id), client_id)

    @contextmanager
    def _operation_scope(self, client_id):
        if getattr(self._operation_owner, "active", None) == (client_id,):
            yield
            return
        with self._serial(self.client_locks, client_id):
            previous = getattr(self._operation_owner, "active", None)
            self._operation_owner.active = (client_id,)
            try:
                yield
            finally:
                self._operation_owner.active = previous

    def run_operation(self, operation, client_id):
        """Serialize and finalize once, including nested legacy/public dispatch."""
        if getattr(self._operation_owner, "active", None) == (client_id,):
            return operation()
        started = time.perf_counter()
        upstream_seconds.set(0.0)
        with self._operation_scope(client_id):
            try:
                result = operation()
            except OverseerrError as error:
                result = failure_reply("movie_error", str(error))
            except Exception:
                log.error("Movie dialogue failed")
                result = failure_reply("movie_error", "I couldn't check the movie right now. Please try again.")
            if result is not None:
                closed = result["action"] in {"movie_cancelled", "movie_cancellation_refused", "movie_error", "movie_available", "movie_download_cancelled", "movie_forgotten", "movie_requested", "movie_no_pending", "movie_conversation_ended"}
                pending = self.pending.get(client_id)
                result.update(listen_for_seconds=0 if closed else 10,
                              context_expires_in=max(0, pending.expires_at - self.clock()) if pending else 0)
                result["timings"] = {"movie": round(time.perf_counter() - started, 3),
                                     "upstream": round(upstream_seconds.get(), 3)}
            return result

    def _waiting(self, client_id, intent="request", force=False):
        self.pending[client_id] = PendingMovie((), self.clock() + self.timeout, intent, "title",
                                               force_confirmation=force)
        return self._prompt(self.pending[client_id])

    def _remember(self, movie, client_id):
        if self.store is not None:
            self.store.remember_movie(client_id, movie)
        self.memory[client_id] = movie

    def _remembered(self, client_id):
        return self.store.remembered_movie(client_id) if self.store is not None else self.memory.get(client_id)

    def _expire_pending(self, client_id):
        pending = self.pending.get(client_id)
        expired = pending is not None and pending.expires_at <= self.clock()
        if expired:
            self.pending.pop(client_id, None)
        return self.pending.get(client_id), expired

    @staticmethod
    def _permitted_decisions(pending):
        if pending.state == "title":
            return ["title", "reject"]
        if pending.state in {"offer", "cancel_download"}:
            return ["confirm", "reject"]
        return (["confirm", "reject", "select", "title"] if len(pending.movies) == 1
                else ["reject", "select", "title"])

    def _pending_for_token(self, client_id, pending_id):
        pending, _ = self._expire_pending(client_id)
        return pending if pending is not None and pending_id == pending.pending_id else None

    @staticmethod
    def _invalid_choice():
        return reply("movie_no_pending", "That movie choice is no longer valid. Please ask again.", False,
                     alternatives=["That movie choice isn't valid anymore. Please ask me again."])

    def media_context(self, client_id):
        """Detached backend facts, with no private cancellation target exposed."""
        def movie(value):
            return None if value is None else {
                "media_id": value.media_id, "title": value.title, "year": value.year}
        with self._operation_scope(client_id):
            pending = self.pending.get(client_id)
            snapshot = None
            if pending is not None and pending.expires_at > self.clock():
                snapshot = {
                    "pending_id": pending.pending_id, "intent": pending.intent, "state": pending.state,
                    "candidates": [movie(value) for value in pending.movies],
                    "permitted_decisions": self._permitted_decisions(pending),
                    "expires_in": max(0, pending.expires_at - self.clock()),
                }
            return {
                "pending": snapshot, "remembered_movie": movie(self._remembered(client_id)),
                "last_requested_movie": movie(self.store.last_requested_movie(client_id))
                if self.store is not None else None,
            }

    def _begin_movie(self, client_id, kind, title, year, reference=None):
        if (year is not None and title is None) or (title is not None and reference is not None):
            raise ValueError("Conflicting movie target")
        if reference not in {None, "remembered", "last_requested"}:
            raise ValueError("Unknown movie reference")
        self.pending.pop(client_id, None)
        self.recent[client_id] = self.clock() + self.timeout
        self.recent_intent[client_id] = kind
        if kind != "cancel_download":
            self.misses.pop(client_id, None)
        if title:
            return self._search(title, client_id, kind, year=year)
        if kind in {"status", "cancel_download"}:
            if reference == "last_requested":
                movie = self.store.last_requested_movie(client_id) if self.store is not None else None
            else:
                movie = self._remembered(client_id)
            if movie:
                if kind == "status":
                    if reference == "last_requested":
                        self._remember(movie, client_id)
                    return self._status(movie, client_id)
                return self._select(movie, client_id, kind)
        return self._waiting(client_id, kind)

    @structured_operation
    def request_movie(self, client_id, *, title=None, year=None):
        return self._begin_movie(client_id, "request", title, year)

    @structured_operation
    def check_availability(self, client_id, *, title=None, year=None):
        return self._begin_movie(client_id, "availability", title, year)

    @structured_operation
    def movie_status(self, client_id, *, title=None, year=None, reference=None):
        return self._begin_movie(client_id, "status", title, year, reference)

    @structured_operation
    def begin_movie_cancellation(self, client_id, *, title=None, year=None, reference=None):
        return self._begin_movie(client_id, "cancel_download", title, year, reference)

    @structured_operation
    def last_requested_movie(self, client_id):
        """Legacy identity-only question; not a separate AI tool."""
        self.pending.pop(client_id, None)
        self.recent_intent[client_id] = "status"
        self.misses.pop(client_id, None)
        movie = self.store.last_requested_movie(client_id) if self.store is not None else None
        if not movie:
            return reply("movie_status", "I don't have a previous movie request for this Cube.")
        self._remember(movie, client_id)
        self.recent[client_id] = self.clock() + self.timeout
        return reply("movie_status", f"The last movie you requested was {movie.label}.")

    @structured_operation
    def end_conversation(self, client_id, *, forget=False):
        self.pending.pop(client_id, None)
        self.recent.pop(client_id, None)
        self.recent_intent.pop(client_id, None)
        self.misses.pop(client_id, None)
        if forget:
            self._remember(None, client_id)
            return reply("movie_forgotten", "I've forgotten that movie. Existing requests are unchanged.",
                         alternatives=["I've cleared that movie from memory. Your existing requests are unchanged."])
        return reply("movie_cancelled", "Okay, cancelled this conversation.",
                     alternatives=["Okay, we'll leave this conversation here. Your movie requests are unchanged."])

    @structured_operation
    def repeat_pending(self, client_id, *, pending_id):
        pending = self._pending_for_token(client_id, pending_id)
        return self._prompt(pending) if pending else self._invalid_choice()

    @structured_operation
    def select_pending_movie(self, client_id, *, pending_id, media_id):
        """Resolve a backend candidate; selection never authorizes deletion."""
        pending = self._pending_for_token(client_id, pending_id)
        if pending is None:
            return self._invalid_choice()
        selected = next((movie for movie in pending.movies if movie.media_id == media_id), None)
        if selected is None:
            return self._invalid_choice()
        self.pending.pop(client_id, None)
        return self._select(selected, client_id, pending.intent, pending.force_confirmation)

    @structured_operation
    def respond_to_pending(self, client_id, *, pending_id=None, decision, media_id=None,
                           title=None, year=None):
        pending, expired = self._expire_pending(client_id)
        if pending_id is None and pending is None:
            # The legacy parser can ask with no current choice; AI requires a token.
            if decision == "confirm":
                return reply("movie_no_pending", "That movie confirmation expired. Please ask for the movie again."
                             if expired else "There isn't a movie waiting for confirmation.")
            if decision == "reject":
                return reply("movie_no_pending", "Which movie did you mean? Say get and its title.")
        if (pending is None or pending.pending_id != pending_id
                or decision not in self._permitted_decisions(pending)):
            return self._invalid_choice()
        if decision == "confirm":
            self.pending.pop(client_id, None)
            if pending.state == "cancel_download":
                return self._cancel(pending.cancellation_target, client_id)
            return self._select(pending.movies[0], client_id,
                                "request" if pending.state == "offer" else pending.intent)
        if decision == "reject":
            if pending.intent == "cancel_download":
                self.pending.pop(client_id, None)
                self.recent.pop(client_id, None)
                return reply("movie_cancelled", "Okay, I'll leave that request alone.",
                             alternatives=["Okay, I won't cancel that movie request."])
            return self._waiting(client_id, pending.intent, pending.force_confirmation)
        if decision == "select":
            return self.select_pending_movie(client_id, pending_id=pending_id, media_id=media_id)
        if not title:
            return self._invalid_choice()
        if pending.state != "title":
            return self.correct_movie(client_id, title=title, year=year, pending_id=pending_id)
        result = self._search(title, client_id, pending.intent, pending.force_confirmation, year=year)
        if result["action"] == "movie_not_found":
            if pending.failures >= 1:
                return self._end(client_id)
            self.pending[client_id] = replace(self.pending[client_id], failures=pending.failures + 1,
                                               expires_at=pending.expires_at)
        return result

    @structured_operation
    def correct_movie(self, client_id, *, title, year=None, pending_id=None, selected_media_id=None):
        pending, _ = self._expire_pending(client_id)
        if ((pending is not None and pending_id != pending.pending_id)
                or (pending is None and pending_id is not None)):
            return self._invalid_choice()
        if pending:
            kind, force = pending.intent, pending.force_confirmation
            # Catalog matching only. Ordinals and spoken aliases are resolved by
            # the legacy parser before it calls this operation with a candidate ID.
            if selected_media_id is not None:
                matches = [movie for movie in pending.movies if movie.media_id == selected_media_id]
            else:
                matches = [movie for movie in pending.movies
                           if normalize(movie.title) == normalize(title) and (year is None or movie.year == year)]
            if selected_media_id is not None and not matches:
                return self._invalid_choice()
            if len(matches) == 1:
                return self.select_pending_movie(client_id, pending_id=pending_id, media_id=matches[0].media_id)
        else:
            kind = self.recent_intent.get(client_id, "request") if self.recent.get(client_id, 0) > self.clock() else "request"
            force = kind == "request"
        self.pending.pop(client_id, None)
        return self._search(title, client_id, kind, force, year=year)

    @structured_operation
    def record_unrecognized(self, client_id, *, has_input=True):
        pending, _ = self._expire_pending(client_id)
        if pending:
            if pending.failures + 1 >= 2:
                return self._end(client_id)
            self.pending[client_id] = replace(pending, failures=pending.failures + 1)
            return self._prompt(self.pending[client_id])
        if has_input and self.recent.get(client_id, 0) > self.clock():
            self.misses[client_id] = self.misses.get(client_id, 0) + 1
            if self.misses[client_id] >= 2:
                return self._end(client_id)
            return reply("movie_retry", "I can check the movie or help request another. What would you like?")
        return None

    @staticmethod
    def _parse_title_year(query):
        match = re.fullmatch(r"(.+?)\s+(?:from\s+)?((?:18|19|20)\d{2})", query)
        return match.groups() if match else (query or None, None)

    def _handle(self, command, client_id):
        """Legacy language entry point; structured operations never call this."""
        return self.run_operation(lambda: self._dispatch_legacy(command, client_id), client_id)

    def _dispatch_legacy(self, command, client_id):
        # Keep language classification and utterance-specific restrictions here.
        pending = self.pending.get(client_id)
        if pending is not None and pending.expires_at <= self.clock():
            pending = None
        intent = parse_intent(command)
        if intent.kind == "yes":
            if pending and (len(pending.movies) != 1 or pending.state == "cancel_download"
                            and command in RULES["yes"]["restricted_when_cancelling"]):
                return self.repeat_pending(client_id, pending_id=pending.pending_id)
            return self.respond_to_pending(client_id, pending_id=pending.pending_id if pending else None,
                                           decision="confirm")
        # Other legacy branches historically prune expired pending state first.
        pending, _ = self._expire_pending(client_id)
        if intent.kind == "device":
            return None
        if intent.kind in {"forget", "cancel"}:
            return self.end_conversation(client_id, forget=intent.kind == "forget")
        if intent.kind == "reject":
            return self.respond_to_pending(client_id, pending_id=pending.pending_id if pending else None,
                                           decision="reject")
        if intent.kind == "last":
            return self.last_requested_movie(client_id)
        title, year = self._parse_title_year(intent.query)
        if intent.kind in {"cancel_download", "cancel_last"}:
            # Retain the legacy no-store fallback for this legacy-only phrase.
            reference = "last_requested" if self.store is not None and intent.kind == "cancel_last" else None
            return self.begin_movie_cancellation(client_id, title=title, year=year, reference=reference)
        if intent.kind in {"request", "status", "availability"}:
            operation = {"request": self.request_movie, "status": self.movie_status,
                         "availability": self.check_availability}[intent.kind]
            return operation(client_id, title=title, year=year)
        if intent.kind == "correct":
            selected = self._resolve(intent.query, pending.movies) if pending else None
            return self.correct_movie(client_id, title=title, year=year,
                                      pending_id=pending.pending_id if pending else None,
                                      selected_media_id=selected.media_id if selected else None)
        if pending:
            if pending.state == "title" and command and not self._unsafe_answer(command):
                title, year = self._parse_title_year(command)
                return self.respond_to_pending(client_id, pending_id=pending.pending_id,
                                               decision="title", title=title, year=year)
            selected = self._resolve(command, pending.movies)
            if selected:
                return self.select_pending_movie(client_id, pending_id=pending.pending_id, media_id=selected.media_id)
        return self.record_unrecognized(client_id, has_input=bool(command))

    def _end(self, client_id):
        self.pending.pop(client_id, None)
        self.recent.pop(client_id, None)
        self.misses.pop(client_id, None)
        return reply("movie_conversation_ended", "Let's try again later. Say Hey Cube and the movie request when you're ready.",
                     alternatives=["We can try again later. When you're ready, say Hey Cube and ask for the movie."])

    @staticmethod
    def _unsafe_answer(command):
        return bool(re.search(r"\b(?:don't|not|tomorrow|later)\b", command) or command in YES | REJECT)

    def _prompt(self, pending):
        movies = pending.movies
        if pending.state == "cancel_download":
            action = "movie_cancellation_confirmation"
            message = f"I found {movies[0].label}, Should I delete it now?"
            alternatives = [f"I found {movies[0].label}. Do you want me to cancel it now?"]
        elif pending.state == "title":
            action, message = "movie_title_needed", "Which movie?"
            alternatives = ["What movie did you have in mind?", "What's the movie title?"]
        elif pending.state == "offer":
            action, message = "movie_confirmation", f"{movies[0].label} isn't available. Shall I request it?"
            alternatives = [f"{movies[0].label} isn't available yet. Would you like me to request it?"]
        elif pending.force_confirmation and len(movies) == 1:
            action, message = "movie_confirmation", f"Earlier requests are unchanged. Shall I also request {movies[0].label}?"
            alternatives = [f"Your earlier requests are unchanged. Would you also like me to request {movies[0].label}?"]
        else:
            action = "movie_confirmation" if len(movies) == 1 else "movie_ambiguous"
            message = f"Did you mean {movies[0].label}?" if len(movies) == 1 else clarification(movies)
            choices = " or ".join(movie.label for movie in movies)
            alternatives = ([f"Is {movies[0].label} the movie you meant?"] if len(movies) == 1
                            else [f"Did you mean {choices}?"])
        return reply(action, message, alternatives=alternatives, follow_up=True,
                     expires_in=max(0, pending.expires_at - self.clock()),
                     candidates=[{"title": m.title, "year": m.year} for m in movies])

    def _resolve(self, command, movies):
        if not movies:
            return None
        ordinal = _pattern_match("select_ordinal", command)
        year = _pattern_match("select_year", command)
        newer = set(RULES["select_newer"]["exact"])
        older = set(RULES["select_older"]["exact"])
        if ordinal:
            index = {"first": 0, "second": 1, "third": 2}[ordinal[1]]
            return movies[index] if index < len(movies) else None
        if year:
            matches = [m for m in movies if m.year == year[1]]
        elif (len({normalize(m.title) for m in movies}) == 1
              and all(m.year for m in movies) and command in newer | older):
            target = (max if command in newer else min)(m.year for m in movies)
            matches = [m for m in movies if m.year == target]
        else:
            matches = [m for m in movies if command in {
                normalize(m.title), normalize(m.label), normalize(f"{m.title} {m.year}"),
            }]
        return matches[0] if len(matches) == 1 else None

    def _existing(self, movie):
        if movie.state == "available":
            return reply("movie_available", f"{movie.label} is already available.",
                         alternatives=[f"{movie.label} is already ready to watch."])
        if movie.state == "requested":
            return reply("movie_already_requested", f"{movie.label} has already been requested.",
                         alternatives=[f"There's already a request for {movie.label}."])
        if movie.state == "failed":
            return failure_reply("movie_error", f"The existing request for {movie.label} needs attention in Overseerr.")
        return None

    def _search(self, title, client_id, intent="request", force=False, *, year=None):
        self.pending.pop(client_id, None)
        title = normalize(title)
        try:
            if intent == "cancel_download":
                movies = self.store.owned_movies(client_id) if self.store else []
            else:
                movies = self.client.search_movies(title)
        except OverseerrError:
            self._waiting(client_id, intent, force)
            raise
        movies = list({m.media_id: m for m in movies if year is None or m.year == year}.values())
        ranked = sorted(
            ((SequenceMatcher(None, normalize(title), normalize(m.title)).ratio(), m) for m in movies),
            key=lambda item: item[0], reverse=True,
        )
        if not ranked or ranked[0][0] < 0.6:
            result = self._waiting(client_id, intent, force)
            return {**result, **reply("movie_not_found", "I couldn't find a close match. What's the full title and year?",
                                     alternatives=["I couldn't find a close match. Could you give me the full title and year?"])}
        best_score, best = ranked[0]
        close = [m for score, m in ranked if best_score - score < 0.12]
        if len(close) > 3:
            result = self._waiting(client_id, intent, force)
            return {**result, **reply(result["action"], "I found several matches. What's the full title and year?",
                                     alternatives=["There are several matches. Could you give me the full title and year?"])}
        if len(close) > 1 or best_score < 1.0:
            if len({normalize(m.title) for m in close}) == 1:
                close.sort(key=lambda m: m.year or "")
            self.pending[client_id] = PendingMovie(tuple(close), self.clock() + self.timeout, intent,
                                                   force_confirmation=force)
            return self._prompt(self.pending[client_id])
        return self._select(best, client_id, intent, force)

    def _select(self, movie, client_id, intent, force=False):
        self._remember(movie, client_id)
        self.recent[client_id] = self.clock() + self.timeout
        self.recent_intent[client_id] = intent
        self.misses.pop(client_id, None)
        if intent == "cancel_download":
            try:
                with self._serial(self.movie_locks, movie.media_id):
                    target = self.cancellations.preview(movie, client_id)
                self.pending[client_id] = PendingMovie((movie,), self.clock() + self.timeout,
                    "cancel_download", "cancel_download", cancellation_target=target)
                return self._prompt(self.pending[client_id])
            except AlreadyCancelled as error:
                return reply("movie_download_cancelled", str(error), alternatives=[f"No need to cancel it again. {error}"])
            except CancellationRefused as error:
                return failure_reply("movie_cancellation_refused", str(error))
            except (ArrError, OverseerrError):
                return failure_reply("movie_cancellation_refused", "I couldn't verify that request safely. Please try again.")
        if intent == "status":
            return self._status(movie, client_id)
        if intent == "availability":
            current = self.client.get_movie(movie.media_id)
            existing = self._existing(current)
            if existing:
                return existing
            self.pending[client_id] = PendingMovie((movie,), self.clock() + self.timeout, "availability", "offer")
            return self._prompt(self.pending[client_id])
        if force:
            self.pending[client_id] = PendingMovie((movie,), self.clock() + self.timeout,
                                                   force_confirmation=True)
            return self._prompt(self.pending[client_id])
        tracked = self.store.request_record(client_id, movie.media_id) if self.store else None
        if tracked and (tracked["cancellation_state"] != "active" or not tracked["confirmed"]):
            return self._confirm(movie, client_id)
        existing = self._existing(movie)
        return existing or self._confirm(movie, client_id)

    def _cancel(self, target, client_id):
        try:
            with self._serial(self.movie_locks, target["media_id"]):
                result = self.cancellations.execute(target, client_id)
                if result["action"] == "movie_download_cancelled":
                    self.submitted.pop(target["media_id"], None)
            self.recent_intent[client_id] = "status"
            return result
        except CancellationRefused as error:
            return failure_reply("movie_cancellation_refused", str(error))
        except (ArrError, OverseerrError):
            return failure_reply("movie_cancellation_uncertain", "I couldn't verify the cancellation. Ask me to try cancelling it again.")

    def _status(self, movie, client_id):
        milestone = self.store.movie_milestone(client_id, movie.media_id) if self.store is not None else None
        try:
            current = self.client.get_movie(movie.media_id)
        except OverseerrError:
            cancellation = self.cancellations.status(movie, client_id)
            if cancellation:
                return cancellation
            known = {"ready": "was imported and marked ready", "started": "started downloading"}.get(milestone)
            message = f"The last update for {movie.label} says it {known}." if known else f"I don't have a download update for {movie.label}."
            return reply("movie_status", message + " I can't check its current status right now.",
                         alternatives=[message + " I can't verify its current status at the moment."])
        cancellation = self.cancellations.status(movie, client_id) if current.state != "available" else None
        if cancellation:
            return cancellation
        if current.state == "available":
            message = f"{movie.label} is ready to watch."
            alternative = f"You can watch {movie.label} now."
        elif current.state == "failed":
            message = f"The request for {movie.label} needs attention in Overseerr."
            alternative = f"The request for {movie.label} needs checking in Overseerr."
        elif milestone == "ready":
            message = f"{movie.label} was imported earlier, but it isn't currently marked available."
            alternative = f"{movie.label} was imported before, but it isn't showing as available now."
        elif milestone == "started":
            message = f"{movie.label} started downloading. It isn't marked ready yet."
            alternative = f"The download for {movie.label} started, but it isn't marked ready yet."
        elif current.state == "requested":
            message = f"{movie.label} has been requested, but I haven't received a download update yet."
            alternative = f"{movie.label} has been requested. I haven't had a download update yet."
        else:
            message = f"{movie.label} isn't available and I can't see an active request."
            alternative = f"{movie.label} isn't available, and I can't find an active request for it."
        return reply("movie_status", message, alternatives=[alternative])

    def _confirm(self, movie, client_id):
        with self._serial(self.movie_locks, movie.media_id):
            return self._submit(movie, client_id)

    def _submit(self, movie, client_id):
        # Dialogue is consumed before I/O; uncertain POSTs are never retried here.
        try:
            current = self.client.get_movie(movie.media_id)
            if current.state == "available":
                return self._existing(current)
            rerequest = self.cancellations.before_rerequest(movie, client_id) if self.store else False
            existing = self._existing(current)
            if existing and not rerequest:
                return existing
            previous = self.submitted.get(movie.media_id)
            if previous is not None and (previous[0] == "uncertain" or self.clock() - previous[1] < self.timeout):
                if previous[0] == "uncertain":
                    raise RequestUncertain("I still can't verify the earlier request. Please check Overseerr before trying again.")
                return reply("movie_already_requested", f"{movie.label} has already been requested.",
                             alternatives=[f"There's already a request for {movie.label}."])
            request_id = self.store.prepare_request(movie, client_id) if self.store is not None else None
            if rerequest:
                self.cancellations.enable_rerequest(movie)
            self.submitted[movie.media_id] = ("uncertain", self.clock())
            try:
                overseerr_request_id = self.client.request_movie(movie.media_id)
            except RequestUncertain:
                existing = self._existing(self.client.get_movie(movie.media_id))
                if existing:
                    return existing
                raise
            except OverseerrError:
                # A definite rejection (e.g. permissions) can be retried explicitly.
                self.submitted.pop(movie.media_id, None)
                raise
            self.submitted[movie.media_id] = ("accepted", self.clock())
            if self.store is not None:
                self.store.confirm_request(request_id, overseerr_request_id)
            return reply("movie_requested", request_ack(movie.label, self.store is not None),
                         alternatives=[f"I've requested {movie.label}." +
                                       (" I'll let you know when it's ready." if self.store is not None else "")],
                         media_id=movie.media_id, title=movie.title, year=movie.year)
        except (OverseerrError, ArrError, CancellationRefused) as error:
            return failure_reply("movie_error", str(error))
        except Exception:
            log.error("Movie request could not be verified")
            return failure_reply("movie_error", "The movie request could not be verified. Please check Overseerr before trying again.")
