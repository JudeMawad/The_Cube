"""Private authenticated commissioning API. No voice catalog/tool changes."""
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from core.http_auth import authenticate
from core.client_identity import client_identity
from integrations.spotify.errors import SpotifyError


def music_router(service):
    def authorize(request: Request):
        authenticate(request)
        try:
            if client_identity(request) != service.settings().cube_id:
                raise HTTPException(403, "Cube music identity mismatch")
        except SpotifyError as error:
            raise HTTPException(503, error.code) from None

    router = APIRouter(prefix="/music", dependencies=[Depends(authorize)])

    def call(action):
        try:
            return action()
        except SpotifyError as error:
            status = (422 if error.code == "invalid_request" else
                      409 if error.code in {"receiver_session_expired", "request_conflict"} else
                      429 if error.code in {"rate_limited", "quota_exceeded"} else 503)
            headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
            raise HTTPException(status, error.code, headers=headers) from None

    @router.get("/state")
    def state():
        return call(lambda: {"version": 1, "playback": service.get().wire_state(),
                             "receiver": service.receiver.read()})

    @router.get("/devices")
    def devices():
        return call(lambda: {"devices": service.get().devices()})

    @router.get("/display/state")
    def display_state(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(lambda: service.get_display().state())

    @router.get("/display/artwork/{identity}")
    def display_artwork(identity: str):
        frame = call(lambda: service.get_display().artwork(identity))
        if frame is None:
            raise HTTPException(404, "artwork_unavailable")
        return Response(frame, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    @router.post("/search")
    def search(body: dict = Body(...)):
        if set(body) != {"query", "kind"}:
            raise HTTPException(422, "invalid_request")
        return call(lambda: {"items": service.get().search(body["query"], body["kind"])})

    @router.post("/commands")
    def execute(body: dict = Body(...)):
        return call(lambda: service.get().execute(body))

    @router.post("/receiver/session")
    def register(body: dict = Body(...)):
        if set(body) != {"session_id"}:
            raise HTTPException(422, "invalid_request")
        call(lambda: service.receiver.register(body["session_id"]))
        return {"registered": True}

    @router.post("/receiver/state")
    def receiver(body: dict = Body(...)):
        call(lambda: service.receiver.accept(body))
        return {"accepted": True}

    return router
