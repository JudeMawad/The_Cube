"""Shared token validation for authenticated client HTTP channels."""
from pathlib import Path
import secrets
from fastapi import HTTPException

TOKEN_PATH = Path.home() / ".config/cube/events.token"


def authenticate(request, token_path=TOKEN_PATH):
    try:
        token = token_path.read_text().strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError()
    except (OSError, UnicodeError, ValueError):
        raise HTTPException(503, "Event authentication is not configured") from None
    supplied = request.headers.get("X-Cube-Token", "")
    if not secrets.compare_digest(token.encode(), supplied.encode()):
        raise HTTPException(401, "Invalid event credentials")
