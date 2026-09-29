"""Stable errors; never retain upstream bodies, headers or credentials."""


class SpotifyError(Exception):
    def __init__(self, code, *, retry_after=None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after
