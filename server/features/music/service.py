"""Lazy optional integration: missing configuration cannot break voice startup."""
from threading import RLock
import httpx

from integrations.spotify.api import SpotifyWebApi
from integrations.spotify.config import DIRECTORY, Settings
from integrations.spotify.oauth import Tokens
from integrations.spotify.storage import TokenStore
from .controller import MusicController, ReceiverState


class MusicService:
    def __init__(self, directory=DIRECTORY):
        self.directory, self.lock = directory, RLock()
        self.controller, self.http = None, None
        self.display = None
        self.receiver = ReceiverState()

    def settings(self):
        return Settings.load(self.directory)

    def get(self):
        with self.lock:
            if self.controller is None:
                settings = self.settings()
                from core.cancellation import check
                self.http = httpx.Client(timeout=httpx.Timeout(5, connect=2),
                                         follow_redirects=False, trust_env=False)
                tokens = Tokens(settings, TokenStore(self.directory), self.http)
                self.controller = MusicController(SpotifyWebApi(tokens, self.http, check=check),
                    device_name=settings.device_name, check=check,
                    aliases_path=self.directory / "playlist-aliases.json")
            return self.controller

    def get_display(self):
        with self.lock:
            if self.display is None:
                from .display import MusicDisplay
                self.display = MusicDisplay(self.get(), httpx.Client(
                    timeout=httpx.Timeout(3, connect=2), follow_redirects=False, trust_env=False))
            return self.display

    def close(self):
        with self.lock:
            if self.display:
                self.display.close()
                self.display = None
            if self.http:
                self.http.close()
            self.controller, self.http = None, None
