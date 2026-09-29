"""Authenticated LAN webhooks and long-poll delivery; single server worker."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .media_events import parse_events
from core.http_auth import authenticate as authenticate_token
from core.client_identity import client_identity  # Compatibility export.

TOKEN_PATH = Path.home() / ".config/cube/events.token"
log = logging.getLogger(__name__)


def authenticate(request: Request):
    return authenticate_token(request, TOKEN_PATH)


class Acknowledgement(BaseModel):
    ack_token: str = Field(min_length=1, max_length=128)


def event_router(store):
    router = APIRouter(dependencies=[Depends(authenticate)])

    def receive(service, payload):
        try:
            count = store.accept(parse_events(service, payload))
            return {"queued": count}
        except ValueError:
            log.warning("%s webhook rejected: invalid payload", service)
            raise HTTPException(400, "Invalid media event") from None
        except Exception:
            log.error("%s webhook persistence failed", service)
            raise HTTPException(503, "Event storage unavailable") from None

    @router.post("/webhooks/radarr")
    def radarr(payload: dict):
        return receive("radarr", payload)

    @router.post("/webhooks/sonarr")
    def sonarr(payload: dict):
        return receive("sonarr", payload)

    @router.get("/events/next")
    def next_event(request: Request, wait: float = Query(default=25, ge=0, le=25)):
        identity = client_identity(request)
        try:
            event = store.next_event(identity, wait)
        except Exception:
            log.error("Event delivery storage failed")
            raise HTTPException(503, "Event storage unavailable") from None
        return event if event else Response(status_code=204)

    @router.post("/events/{event_id}/validate")
    def validate(event_id: int, body: Acknowledgement, request: Request):
        try:
            valid = store.event_valid(client_identity(request), event_id, body.ack_token)
        except HTTPException:
            raise
        except Exception:
            log.error("Event validation storage failed")
            raise HTTPException(503, "Event storage unavailable") from None
        return {"valid": valid}

    @router.post("/events/{event_id}/ack")
    def acknowledge(event_id: int, body: Acknowledgement, request: Request):
        identity = client_identity(request)
        try:
            accepted = store.acknowledge(identity, event_id, body.ack_token)
        except Exception:
            log.error("Event acknowledgement storage failed")
            raise HTTPException(503, "Event storage unavailable") from None
        if not accepted:
            raise HTTPException(404, "Notification not found")
        return {"delivered": True}

    return router
