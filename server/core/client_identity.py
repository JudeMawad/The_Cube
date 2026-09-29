"""Cube identity shared by voice and event HTTP routes."""
import re

from fastapi import HTTPException


def client_identity(request):
    identity = request.headers.get("X-Cube-Client-ID")
    if identity is not None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", identity):
            raise HTTPException(400, "Invalid Cube identity")
        return identity
    return request.client.host if request.client else "cube"
