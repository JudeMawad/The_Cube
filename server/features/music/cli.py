"""Authenticated manual commissioning; this is not a phone remote or voice tool."""
import argparse
import json
from pathlib import Path
from uuid import uuid4
import sys
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--cube-id", required=True)
    parser.add_argument("operation", choices=["state", "devices", "search", "play_music", "resume", "play_uri",
                                              "pause", "next", "previous", "seek", "volume", "transfer", "queue"])
    parser.add_argument("--uri")
    parser.add_argument("--value", type=int)
    parser.add_argument("--query")
    parser.add_argument("--kind", choices=["track", "artist", "playlist"])
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Invalid backend URL")
    try:
        token = (Path.home() / ".config/cube/events.token").read_text().strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError()
        with httpx.Client(timeout=40, trust_env=False, follow_redirects=False,
                          headers={"X-Cube-Token": token, "X-Cube-Client-ID": args.cube_id}) as http:
            base = args.url.rstrip("/") + "/music"
            if args.operation in {"state", "devices"}:
                response = http.get(base + "/" + args.operation)
            elif args.operation == "search":
                response = http.post(base + "/search", json={"query": args.query, "kind": args.kind})
            else:
                body = {"request_id": uuid4().hex, "operation": args.operation}
                if args.uri is not None:
                    body["uri"] = args.uri
                if args.value is not None:
                    body["value"] = args.value
                response = http.post(base + "/commands", json=body)
            print(json.dumps(response.json(), indent=2))
            if response.is_error:
                raise SystemExit(1)
    except (OSError, ValueError, httpx.HTTPError):
        print("Music commissioning request unavailable.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
