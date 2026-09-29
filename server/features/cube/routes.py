"""Authenticated client-only control endpoints; no HTTP command submission API."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from core.client_identity import client_identity
from core.http_auth import authenticate as authenticate_token
from .channel import ClientUnavailable
from .protocol import Session, Result


def authenticate(request: Request):
    authenticate_token(request)


def control_router(channel):
    router = APIRouter(prefix="/cube/control", dependencies=[Depends(authenticate)])

    @router.post("/session")
    def register(body: Session, request: Request):
        try:
            channel.register(client_identity(request), body.session_id)
        except ClientUnavailable:
            raise HTTPException(503, "Cube control unavailable") from None
        return {"registered": True}

    @router.get("/next")
    def next_command(request: Request, session_id: str = Query(pattern=r"^[0-9a-f]{32}$"),
                     wait: float = Query(default=25, ge=0, le=25)):
        try:
            command = channel.next(client_identity(request), session_id, wait)
        except ClientUnavailable:
            raise HTTPException(409, "Cube session expired") from None
        return command.model_dump(exclude_none=True) if command else Response(status_code=204)

    @router.post("/{command_id}/result")
    def result(command_id: str, body: Result, request: Request):
        try:
            accepted = channel.complete(client_identity(request), command_id, body)
        except ClientUnavailable:
            accepted = False
        if not accepted:
            raise HTTPException(409, "Cube command expired")
        return {"accepted": True}

    return router
