"""Run as backend service user: python -m integrations.spotify.setup ..."""
import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
import sys

import httpx

from .config import DIRECTORY, Settings, SCOPES, PLAYLIST_SCOPES
from .errors import SpotifyError
from .oauth import Authorization, Tokens
from .storage import TokenStore, private_directory, write_private


def authorize(tokens, client_id, *, server_class=HTTPServer, output=print, playlists=False):
    flow = Authorization(client_id, scopes=SCOPES + PLAYLIST_SCOPES if playlists else SCOPES)
    result = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # OAuth codes/state must never enter access logs.

        def do_GET(self):
            try:
                code = flow.callback(self.path)
                tokens.authorize(code, flow.verifier)
                result.append(None)
                status, message = 200, b"Spotify linked. You can close this window."
            except SpotifyError as error:
                if flow.used:
                    result.append(error)
                status, message = 400, b"Authorization failed. Return to the setup terminal."
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(message)
            except OSError:
                pass  # Closing the browser cannot undo a successful exchange.

        def handle(self):
            self.connection.settimeout(2)
            super().handle()

    # Bind before printing the URL. No permanent listener or LAN callback.
    with server_class(("127.0.0.1", 8768), Callback) as server:
        server.timeout = 1
        output("Open this URL in your browser within five minutes:")
        output(flow.url())
        while not result and monotonic() < flow.deadline:
            server.handle_request()
    if not result:
        raise SpotifyError("authorization_timeout")
    if result[0]:
        raise result[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    config = sub.add_parser("configure")
    config.add_argument("--client-id", required=True)
    config.add_argument("--cube-id", required=True)
    auth = sub.add_parser("authorize")
    auth.add_argument("--playlists", action="store_true", help="Request optional private-playlist read access")
    sub.add_parser("logout")
    args = parser.parse_args()
    try:
        if args.action == "configure":
            settings = Settings(args.client_id, args.cube_id)
            private_directory(DIRECTORY, create=True)
            with TokenStore(DIRECTORY).locked():
                write_private(DIRECTORY / "config.json", asdict(settings))
            print("Spotify backend configured. Run authorize next.")
            return
        settings = Settings.load()
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as http:
            tokens = Tokens(settings, TokenStore(DIRECTORY), http)
            if args.action == "logout":
                tokens.logout()
                print("Local Web API tokens removed. Phone/Soloist playback is unchanged.")
            else:
                authorize(tokens, settings.client_id, playlists=args.playlists)
                print("Spotify linked. Tokens saved privately on this backend.")
    except (SpotifyError, OSError) as error:
        print("Spotify setup failed: " + (error.code if isinstance(error, SpotifyError) else "local_io_error"), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
