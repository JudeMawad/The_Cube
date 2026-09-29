"""Trusted voice context to MusicController, with content-free spoken replies."""
from hashlib import sha256

from integrations.spotify.errors import SpotifyError
from .protocol import identifier

ERRORS = {
    "not_linked": "Spotify isn't linked yet.",
    "reauthorize": "Please link Spotify again.",
    "storage_invalid": "Please check the Spotify setup.",
    "storage_unavailable": "I can't access the Spotify setup right now.",
    "configuration_invalid": "Please check the Spotify setup.",
    "auth_unavailable": "I can't connect to Spotify right now.",
    "device_unavailable": "Select Cube in Spotify on your phone, then try again.",
    "device_ambiguous": "More than one Spotify device is named Cube. Please rename one.",
    "nothing_to_resume": "What would you like me to play?",
    "not_found": "I couldn't find an exact match. Try a title and artist, or say play artist or play playlist.",
    "playlist_not_found": "I couldn't find that in your Spotify playlists. Try your phone or configure a playlist alias.",
    "ambiguous": "I found more than one match. Try a title and artist, or use a unique playlist alias.",
    "aliases_invalid": "Please check your Spotify playlist aliases.",
    "playlist_library_large": "Please use a playlist alias or your phone for that playlist.",
    "playlist_scope_required": "Please link Spotify with playlist access, or use a playlist alias.",
    "network_unavailable": "I can't reach Spotify right now.",
    "service_unavailable": "Spotify isn't responding right now.",
    "restricted": "Spotify won't allow that right now. Please check the app.",
    "rate_limited": "Spotify needs a moment. Please try again later.",
    "quota_exceeded": "Spotify's request allowance is used up. Please use your phone for now.",
    "request_conflict": "That request has already been handled.",
    "invalid_request": "Please give a specific music request.",
    "voice_unavailable": "I can't verify this Cube's music request. Please try again.",
}
CLARIFY = {"nothing_to_resume", "not_found", "playlist_not_found", "ambiguous", "invalid_request"}
SUCCESS = {"play_music": "Playing.", "resume": "Playing.", "play_request": "Playing.",
           "pause": "Paused.", "next": "Skipped.", "previous": "Previous song.", "set_volume": "Music volume set."}


class MusicCommands:
    def __init__(self, service, *, check=lambda: None):
        self.service, self.check = service, check

    def execute(self, context, operation, arguments):
        try:
            self.check()
            # /voice constructs these fields only after authenticating the
            # existing control headers. Anonymous/model-provided IDs are denied.
            if not identifier(context.control_session) or not identifier(context.request_id):
                raise SpotifyError("voice_unavailable")
            if context.client_id != self.service.settings().cube_id:
                raise SpotifyError("voice_unavailable")
            if operation not in SUCCESS:
                raise SpotifyError("invalid_request")
            request_id = sha256((context.client_id + ":" + context.control_session + ":" + context.request_id).encode()).hexdigest()[:32]
            controller = self.service.get()
            self.check()
            if operation == "play_request":
                controller.play_request(request_id, **arguments)
            else:
                data = {"request_id": request_id, "operation": "volume" if operation == "set_volume" else operation}
                if operation == "set_volume":
                    data["value"] = arguments["percent"]
                controller.execute(data)
            return {"success": True, "response": SUCCESS[operation],
                    "action": "music_" + operation, "listen_for_seconds": 0}
        except SpotifyError as error:
            return {"success": False, "response": ERRORS.get(error.code, "I couldn't complete that Spotify request."),
                    "action": "music_failed", "listen_for_seconds": 10 if error.code in CLARIFY else 0,
                    "error": error.code}
