"""Small deterministic resolver; only normalized choices remain in the backend."""
import re
import unicodedata
from time import monotonic

from integrations.spotify.api import valid_uri
from integrations.spotify.errors import SpotifyError
from integrations.spotify.storage import read_private


def normalized(value):
    return " ".join(re.sub(r"[^\w\s]", "", unicodedata.normalize("NFKC", value).casefold()).split())


def read_aliases(path):
    if path is None:
        return {}
    try:
        data = read_private(path)
    except SpotifyError as error:
        if error.code == "not_linked":
            return {}
        raise SpotifyError("aliases_invalid") from None
    if len(data) > 64:
        raise SpotifyError("aliases_invalid")
    result = {}
    for name, uri in data.items():
        if (not isinstance(name, str) or not 1 <= len(name) <= 120
                or not normalized(name) or normalized(name) in result
                or normalized(name) in {"music", "some music", "spotify"}):
            raise SpotifyError("aliases_invalid")
        try:
            valid_uri(uri)
            if not uri.startswith("spotify:playlist:"):
                raise SpotifyError("aliases_invalid")
        except SpotifyError:
            raise SpotifyError("aliases_invalid") from None
        result[normalized(name)] = uri
    return result


class MusicResolver:
    def __init__(self, api, *, aliases_path=None, clock=monotonic):
        self.api, self.aliases_path, self.clock = api, aliases_path, clock
        self.library, self.library_until = [], 0

    def resolve(self, *, query, kind, artist=None, personal=False, selection="exact"):
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 120
                or not isinstance(kind, str) or kind not in {"track", "artist", "playlist", "auto"}
                or selection not in ("exact", "top")
                or selection == "top" and (kind != "track" or artist is not None or personal)
                or type(personal) is not bool
                or artist is not None and (not isinstance(artist, str) or not 1 <= len(artist.strip()) <= 80)
                or artist is not None and kind not in {"track", "auto"}
                or personal and kind not in {"playlist", "auto"}):
            raise SpotifyError("invalid_request")
        query = query.strip().strip('"“”')
        key = normalized(query)
        if not key:
            raise SpotifyError("invalid_request")
        if selection == "top":
            choices = self.api.search(query, "track", details=True)
            if not choices:
                raise SpotifyError("not_found")
            return valid_uri(choices[0]["uri"], track_only=True)
        # Resolve user-configured aliases only for playlist/unspecified requests.
        # An explicit track or artist must not be captured by a playlist alias.
        if kind in {"playlist", "auto"}:
            alias_key = key.removeprefix("my ")
            aliases = read_aliases(self.aliases_path)
            if key in aliases or alias_key in aliases:
                return aliases.get(key, aliases.get(alias_key))
            if alias_key in {"discover weekly", "release radar"} or personal or key.startswith("my "):
                kind, personal, key = "playlist", True, alias_key
        if personal:
            if self.clock() >= self.library_until:
                self.library = self.api.playlists()
                self.library_until = self.clock() + 120
            choices = [(kind, item) for item in self.library]
        else:
            kinds = ("track", "artist") if kind == "auto" and artist is None else ("track",) if kind == "auto" else (kind,)
            choices = []
            for candidate_kind in kinds:
                search = query
                if candidate_kind == "track" and artist:
                    # Field filters narrow retrieval; exact local checks still
                    # decide execution, even if search syntax is misinterpreted.
                    title = query.replace('"', ' ').replace('\\', ' ')
                    performer = artist.replace('"', ' ').replace('\\', ' ')
                    search = f'track:"{title}" artist:"{performer}"'
                choices.extend((candidate_kind, item) for item in self.api.search(search, candidate_kind, details=True))
        matches = []
        for candidate_kind, item in choices:
            if normalized(item["name"]) != key:
                continue
            if candidate_kind == "track" and artist and normalized(artist) not in {normalized(a) for a in item.get("artists", [])}:
                continue
            matches.append((candidate_kind, item))
        if not matches:
            raise SpotifyError("playlist_not_found" if personal else "not_found")
        distinct = {}
        for candidate_kind, item in matches:
            # Different releases of the same exact title/performers are treated
            # as equivalent. Other artists, titles and playlist IDs are distinct.
            artists = tuple(sorted(normalized(a) for a in item.get("artists", [])))
            identity = (candidate_kind, key, artists) if candidate_kind == "track" and artists else (candidate_kind, item["uri"])
            distinct.setdefault(identity, item["uri"])
        if len(distinct) != 1:
            raise SpotifyError("ambiguous")
        uri = valid_uri(next(iter(distinct.values())))
        return uri
