"""Shared discussion guards and catalog-based music-title safety boundary.

No tool execution or free-form intent extraction happens here.
"""
import re
from dataclasses import dataclass, field
from typing import Literal

_ACTION = re.compile(
    r"\b(?:turn|switch|set|make|dim|download|request|cancel|stop|delete|"
    r"get|play|pause|resume|skip|next|previous|connect|disconnect|enable|disable|"
    r"reboot|restart|shutdown|shut|power|plug|mute|unmute|increase|decrease|raise|lower|louder|quieter|volume|sound|lights?|bulbs?|spots?)\b"
)
_LIGHT = re.compile(r"\b(?:lights?|bulbs?|spots?)\b")
_POLITE_ACTION = re.compile(
    r"^(?:can|could|would|will) you (?:please )?"
    r"(?:turn|switch|set|make|dim|download|get|request|cancel|connect|disconnect|play|pause|resume|skip|stop|increase|decrease|raise|lower|mute|unmute)\b"
)


def _non_action_request(text: str) -> bool:
    """Withhold side effects for clear discussion rather than an instruction."""
    lower = text.strip().lower().replace("’", "'")
    if re.search(r'(?:\"[^\"]+\"|“[^”]+”|‘[^’]+’|\'[^\']+\')', text) and _ACTION.search(lower):
        return True
    if re.search(r"\b(?:what if|suppose|imagine|hypothetical(?:ly)?|in theory|if i|if you)\b", lower):
        return True
    if re.search(r"\b(?:(?:i|you|he|she|they) said|the phrase|when i say|what i said)\b", lower) and _ACTION.search(lower):
        return True
    if (re.search(r"\b(?:don't|do not|never|not)\b", lower) or re.match(r"^no\b", lower)) and _ACTION.search(lower):
        return True
    if _POLITE_ACTION.match(lower):
        return False
    if re.match(r"^(?:why|what|how|when|where|who|is|are|does|do|did|should|would|could|can)\b", lower):
        if _LIGHT.search(lower) or re.search(r"\b(?:turn|switch|set|make|dim)\b", lower):
            return True
        if re.match(r"^(?:why|how|should|can i|could i)\b", lower) and _ACTION.search(lower):
            return True
    return False


def _status_question(text: str) -> bool:
    """Keep informational questions away from mutating AI tools."""
    lower = text.strip().lower()
    if _POLITE_ACTION.match(lower):
        return False
    if re.match(r"^(?:can|could|would|will) you\b", lower):
        return bool(re.match(r"^(?:can|could|would|will) you (?:please )?(?:tell|explain|describe|show|check|confirm|say|let me know)\b", lower))
    return bool(re.match(
        r"^(?:why|what|how|when|where|who|is|are|does|do|did|should|has|have|was|were|can i|could i)\b",
        lower,
    ))


def normalized_command(text):
    # Keep quotation marks and numeric punctuation: these controls must match
    # the whole request, without turning quoted or malformed text into actions.
    command = " ".join(text.lower().replace("’", "'").split()).rstrip(".?! ")
    command = re.sub(r"^(?:(?:can|could|would|will) you (?:please )?|please )", "", command)
    command = re.sub(r" please$", "", command)
    return command


MUSIC_HELP = "Try play a song by an artist, play track followed by a title, play artist, or play playlist."
MUSIC_BLOCKED = "I didn't execute a music command. Please give a complete command without conditions."
MUSIC_VOLUME_HELP = "Please use a whole music volume percentage from 0 to 100."


@dataclass(frozen=True)
class MusicDecision:
    """Pure routing result. Only executable decisions contain tool arguments."""
    state: Literal["not_music", "executable", "blocked", "clarification"]
    operation: str | None = None
    arguments: dict = field(default_factory=dict)
    reply: str | None = None


_MUSIC_CLAUSE = re.compile(
    r"\b(?:if|unless|until|when|once|provided that|on condition|hypothetical(?:ly)?|"
    r"suppose|in theory|the phrase|what i said|but|then|actually|instead|no thanks|just kidding|just joking|"
    r"for example|as an example|(?:after|before) (?:i|you|we))\b|"
    r"(?:\b(?:and|but|then|actually|instead)\b|[,;:—–])\s*"
    r"(?:(?:please|just|actually)\s+)*(?:(?:don't|do not|not|never|wait)\b|" + _ACTION.pattern + r")"
)
_MUSIC_PREFIX = re.compile(
    r"(?:(?:play|put on) (?:me )?(?:(?:some|the) )?(?:track|song|artist|playlist|my|music|spotify)\b|"
    r"(?:pause|resume|stop) (?:the )?(?:music|spotify)\b|"
    r"(?:set )?(?:the )?(?:music|spotify) volume\b|"
    r"(?:play|put on) (?:some(?:thing)? (?:relaxing|calm|upbeat|similar|from|i might like)\b|"
    r"(?:some )?(?:relaxing|calm|upbeat) music\b))"
)
_EMBEDDED_COMMAND = re.compile(r"\b(?:play|put on|pause|resume|stop|next|previous|skip|music volume|spotify volume|set)\b")


def _music_matches(command, rules):
    matches = []
    for (feature, operation), rule in rules.items():
        if feature != "music":
            continue
        if rule.get("fast_path_exact") and command in rule.get("exact", []):
            matches.append((operation, {}))
        for pattern in rule.get("patterns", []):
            if pattern.get("fast_path") and (found := re.fullmatch(pattern["regex"], command)):
                matches.append((operation, found.groupdict()))
    return matches


def _owns_music(command, rules):
    if _MUSIC_PREFIX.match(command) or _music_matches(command, rules):
        return True
    # An exact control with an attached refusal is still owned by music, but
    # ordinary phrases such as 'next week' must not become music requests.
    for (feature, _), rule in rules.items():
        if feature == "music" and rule.get("fast_path_exact"):
            for exact in rule.get("exact", []):
                if command.startswith(exact) and command[len(exact):len(exact) + 1] in {" ", ",", ";", "—", "–"}:
                    suffix = command[len(exact):]
                    if _MUSIC_CLAUSE.search(suffix) or _non_action_request("play " + suffix):
                        return True
    return False


def _music_name(value):
    """Return a safe literal name, or None when it resembles another clause.

    A leading negative title word is allowed only inside a parsed argument;
    the remaining words must pass the same discussion checks. This deliberately
    clarifies unusual titles rather than interpreting a conditional instruction.
    """
    value = value.strip()
    quoted = False
    for opening, closing in (("\"", "\""), ("“", "”"), ("'", "'")):
        if value.startswith(opening) and value.endswith(closing) and len(value) > 1:
            value = value[1:-1].strip()
            quoted = True
            break
    if (not value or re.search(r'["“”]', value) or _MUSIC_CLAUSE.search(value)
            or not quoted and re.search(r"[,;:—–]", value)):
        return None
    checked = re.sub(r"^(?:don't|do not|never|imagine)\b\s*", "", value, count=1)
    if _non_action_request("play " + checked):
        return None
    return value


def classify_music(text, rules):
    """One decision for local matching, execution, rejection, and AI routing.

    Wrapper inspection establishes ownership only: it never yields executable
    arguments. Names and safety are decided without Spotify or model calls.
    """
    command = normalized_command(text)
    if not _owns_music(command, rules):
        if _non_action_request(text) or _status_question(text):
            for found in _EMBEDDED_COMMAND.finditer(command):
                embedded = command[found.start():].strip('\"“”‘’\' .?!')
                quoted = re.split(r'["“”‘’\']', embedded, maxsplit=1)[0]
                if _owns_music(embedded, rules) or _owns_music(quoted, rules):
                    return MusicDecision("blocked", reply=MUSIC_BLOCKED)
        return MusicDecision("not_music")
    if _status_question(text) or _MUSIC_CLAUSE.search(command):
        return MusicDecision("blocked", reply=MUSIC_BLOCKED)
    matches = _music_matches(command, rules)
    if len(matches) == 1 and matches[0][0] == "play_request":
        _, captures = matches[0]
        query = _music_name(captures["query"])
        raw_artist = captures.get("artist")
        artist = _music_name(raw_artist) if raw_artist is not None else None
        if query is None or raw_artist is not None and artist is None:
            return MusicDecision("blocked", reply=MUSIC_BLOCKED)
        if artist and " by " in artist:
            return MusicDecision("clarification", reply="Please say a complete track and artist command with one by separator.")
        personal = captures.get("personal") == "my"
        kind = captures.get("kind") or ("playlist" if personal else "track")
        return MusicDecision("executable", "play_request", {
            "query": query, "kind": kind, "artist": artist, "personal": personal,
            "selection": "top" if kind == "track" and artist is None else "exact"})
    if _non_action_request(text):
        return MusicDecision("blocked", reply=MUSIC_BLOCKED)
    if len(matches) == 1:
        operation, arguments = matches[0]
        if operation == "set_volume":
            raw = arguments["percent"]
            if not re.fullmatch(r"[0-9]+", raw) or int(raw) > 100:
                return MusicDecision("clarification", reply=MUSIC_VOLUME_HELP)
            arguments = {"percent": int(raw)}
        return MusicDecision("executable", operation, arguments)
    for pattern in rules.get(("music", "set_volume"), {}).get("patterns", []):
        if pattern["label"] == "malformed numeric setting guard" and re.fullmatch(pattern["regex"], command):
            return MusicDecision("clarification", reply=MUSIC_VOLUME_HELP)
    return MusicDecision("clarification", reply=MUSIC_HELP)
