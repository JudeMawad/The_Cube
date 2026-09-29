"""One personal account and one Cube; credentials stay on the backend host."""
from dataclasses import dataclass
from pathlib import Path
import re

from .errors import SpotifyError
from .storage import private_directory, read_private

DIRECTORY = Path.home() / ".config/cube/spotify-web"
SCOPES = ("user-read-playback-state", "user-modify-playback-state",
          "user-read-currently-playing")
PLAYLIST_SCOPES = ("playlist-read-private",)
REDIRECT_URI = "http://127.0.0.1:8768/callback"


@dataclass(frozen=True)
class Settings:
    client_id: str
    cube_id: str
    device_name: str = "Cube"

    def __post_init__(self):
        if (not isinstance(self.client_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", self.client_id)
                or not isinstance(self.cube_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", self.cube_id)
                or self.device_name != "Cube"):
            raise SpotifyError("configuration_invalid")

    @classmethod
    def load(cls, directory=DIRECTORY):
        try:
            private_directory(directory)
            return cls(**read_private(Path(directory) / "config.json"))
        except FileNotFoundError:
            raise SpotifyError("not_linked") from None
        except (TypeError, OSError):
            raise SpotifyError("configuration_invalid") from None
