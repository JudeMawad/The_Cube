"""Confirmed, owned movie cancellation with durable progress and read-back checks."""

from datetime import datetime

from integrations.arr_client import ArrError
from integrations.overseerr_client import OverseerrError, parse_movie
from integrations.radarr_client import RadarrClient
from core.response_options import approved_response_options


class CancellationRefused(Exception):
    """A safe, spoken explanation; no upstream response text."""


class AlreadyCancelled(Exception):
    pass


def result(action, response, success=True, *, alternatives=()):
    return {"action": action, "response": response, "success": success,
            "response_options": approved_response_options(response, alternatives)}


class MovieCancellation:
    def __init__(self, client, store, radarr=None):
        self.client = client
        self.store = store
        self.radarr = radarr if radarr is not None else RadarrClient()

    def _owned(self, movie_id, client_id):
        row = self.store.owned_request(client_id, movie_id) if self.store is not None else None
        if not row:
            raise CancellationRefused("I can only cancel movies requested through this Cube.")
        if self.store.has_other_owner(client_id, movie_id):
            raise CancellationRefused("Another Cube also requested that movie. Please manage it in Overseerr.")
        return row

    def _requests(self, details):
        info = details.get("mediaInfo") or {}
        references = info.get("requests") or []
        if not isinstance(references, list):
            raise CancellationRefused("I couldn't verify the movie requests.")
        requests = []
        for item in references:
            if not isinstance(item, dict):
                raise CancellationRefused("I couldn't verify the movie requests.")
            if item.get("is4k") or item.get("status") not in {1, 2, 4}:
                continue
            identity = item.get("id")
            if type(identity) is not int or identity <= 0:
                raise CancellationRefused("I couldn't verify the movie request identity.")
            request = self.client.get_request(identity)
            requests.append(request if request is not None else {"id": identity, "missing": True})
        return requests

    def _request_matches(self, request, movie_id, user_id):
        return (request.get("type") == "movie" and request.get("is4k") is False
                and (request.get("media") or {}).get("tmdbId") == movie_id
                and (request.get("requestedBy") or {}).get("id") == user_id)

    @staticmethod
    def _near_submission(request, tracked):
        try:
            timestamp = datetime.fromisoformat(request["createdAt"].replace("Z", "+00:00"))
            return timestamp.tzinfo is not None and abs(timestamp.timestamp() - tracked["requested_at"]) <= 120
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    @staticmethod
    def _queue_identity(item):
        return {"id": item["id"], "downloadId": item.get("downloadId")}

    def preview(self, movie, client_id):
        tracked = self._owned(movie.media_id, client_id)
        details = self.client.movie_details(movie.media_id)
        if parse_movie(details).state == "available":
            raise CancellationRefused(f"{movie.label} is already available. There's no unfinished download to cancel.")
        operation = self.store.cancellation(client_id, movie.media_id)
        if operation and operation["generation"] == tracked["generation"]:
            _, radarr_movie, queue = self._inspect(operation["target"], client_id, allow_queue_change=True)
            if tracked["cancellation_state"] == "cancelled" and radarr_movie is None:
                raise AlreadyCancelled(f"{movie.label} is already cancelled.")
            # Older cancellations kept an unmonitored entry. Removing it needs a
            # fresh confirmation; status/restart reconciliation never broadens scope.
            return {**operation["target"], "remove_radarr": True,
                    "queue": [self._queue_identity(item) for item in queue]}
        requests = self._requests(details)
        user_id = self.client.requesting_user_id()
        if tracked["overseerr_request_id"] is not None:
            request = self.client.get_request(tracked["overseerr_request_id"])
            matches = [request] if request and self._request_matches(request, movie.media_id, user_id) else []
        else:
            matches = [r for r in requests if self._request_matches(r, movie.media_id, user_id)
                       and self._near_submission(r, tracked)]
        if len(matches) != 1:
            raise CancellationRefused("I can't safely match that older request. Please cancel it in Overseerr and Radarr.")
        request = matches[0]
        if any(r["id"] != request["id"] for r in requests):
            raise CancellationRefused("Another active request shares that movie. Please manage it in Overseerr.")
        server_id = request.get("serverId")
        if server_id is None:
            server_id = (details.get("mediaInfo") or {}).get("serviceId")
        self.radarr.verify_server(self.client.radarr_servers(), server_id)
        radarr_movie = self.radarr.find_movie(movie.media_id)
        if radarr_movie and radarr_movie["hasFile"]:
            raise CancellationRefused(f"{movie.label} has already been imported. I'll keep the library file.")
        external_id = (details.get("mediaInfo") or {}).get("externalServiceId")
        if radarr_movie and external_id is not None and external_id != radarr_movie["id"]:
            raise CancellationRefused("The movie's service identity has changed. Please check it manually.")
        queue = self.radarr.queue_for_movie(radarr_movie["id"]) if radarr_movie else []
        return {"request_id": tracked["id"], "generation": tracked["generation"],
                "media_id": movie.media_id, "label": movie.label, "user_id": user_id,
                "overseerr_id": request["id"], "server_id": server_id,
                "radarr_id": radarr_movie["id"] if radarr_movie else None, "remove_radarr": True,
                "queue": [self._queue_identity(item) for item in queue]}

    def _inspect(self, target, client_id, allow_queue_change=False, prepared_rerequest=False):
        if prepared_rerequest:
            tracked = self.store.request_record(client_id, target["media_id"])
            if not tracked or tracked["confirmed"] or self.store.has_other_owner(client_id, target["media_id"]):
                raise CancellationRefused("The request ownership changed. Please check Overseerr.")
        else:
            tracked = self._owned(target["media_id"], client_id)
        expected_generation = target["generation"] + int(prepared_rerequest)
        if tracked["id"] != target["request_id"] or tracked["generation"] != expected_generation:
            raise CancellationRefused("That request has changed. Please ask me to cancel it again.")
        details = self.client.movie_details(target["media_id"])
        if parse_movie(details).state == "available":
            raise CancellationRefused(f"{target['label']} is now available. I'll keep the library file.")
        user_id = self.client.requesting_user_id()
        request = self.client.get_request(target["overseerr_id"])
        if user_id != target["user_id"] or (request and not self._request_matches(request, target["media_id"], user_id)):
            raise CancellationRefused("The request ownership changed. Please check Overseerr.")
        references = self._requests(details)
        if any(r["id"] != target["overseerr_id"] for r in references):
            raise CancellationRefused("Another active request shares that movie. Please check Overseerr.")
        if request is None and references:
            raise OverseerrError("The request removal is not yet verified.")
        service_id = request.get("serverId") if request else None
        info = details.get("mediaInfo") or {}
        if service_id is None:
            service_id = info.get("serviceId")
        if service_id is not None and service_id != target["server_id"]:
            raise CancellationRefused("The movie's Radarr server changed. Please check it manually.")
        if (info.get("externalServiceId") is not None and target["radarr_id"] is not None
                and info["externalServiceId"] != target["radarr_id"]):
            raise CancellationRefused("The movie's service identity changed. Please check it manually.")
        self.radarr.verify_server(self.client.radarr_servers(), target["server_id"])
        movie = self.radarr.find_movie(target["media_id"])
        if movie and movie["hasFile"]:
            raise CancellationRefused(f"{target['label']} has now been imported. I'll keep the library file.")
        if movie and movie["id"] != target["radarr_id"]:
            raise CancellationRefused("The download changed since confirmation. Please ask me to cancel it again.")
        queue = self.radarr.queue_for_movie(movie["id"]) if movie else []
        if not allow_queue_change and any(self._queue_identity(item) not in target["queue"] for item in queue):
            raise CancellationRefused("The download queue changed. Please ask me to cancel it again.")
        return request, movie, queue

    def execute(self, target, client_id):
        # Confirmed identities are immutable. Retries never broaden the operation.
        self._inspect(target, client_id)
        operation_id = self.store.begin_cancellation(client_id, target)
        stage = "prepared"
        try:
            request, movie, queue = self._inspect(target, client_id)
            if movie and movie["monitored"]:
                self.radarr.set_monitored(movie["id"], False)
            request, movie, queue = self._inspect(target, client_id)
            if movie and movie["monitored"]:
                raise ArrError("Monitoring change is not verified")
            stage = "unmonitored"
            self.store.cancellation_progress(operation_id, stage, "pending")
            for item in target["queue"]:
                _, _, current_queue = self._inspect(target, client_id)
                if any(self._queue_identity(current) == item for current in current_queue):
                    self.radarr.remove_queue_item(item["id"])
            request, _, queue = self._inspect(target, client_id)
            if queue:
                raise ArrError("Download removal is not verified")
            stage = "queue_removed"
            self.store.cancellation_progress(operation_id, stage, "pending")
            if request is not None:
                self.client.delete_request(target["overseerr_id"])
            stage = "request_removed"
            self.store.cancellation_progress(operation_id, stage, "pending")
            request, movie, queue = self._inspect(target, client_id)
            if request is not None or queue:
                raise ArrError("Request and download removal are not verified")
            if target.get("remove_radarr") and movie is not None:
                self.radarr.delete_movie(movie["id"])
            stage = "radarr_removed" if target.get("remove_radarr") else "request_removed"
            self.store.cancellation_progress(operation_id, stage, "pending")
            if self.reconcile(target, client_id, operation_id):
                return result("movie_download_cancelled", f"Alright I cancelled it.",
                              alternatives=[f"I've cancelled {target['label']}."])
        except (ArrError, OverseerrError, CancellationRefused):
            # Read-back can resolve a lost response, but never issues another write.
            try:
                if self.reconcile(target, client_id, operation_id):
                    return result("movie_download_cancelled", f"Cancelled {target['label']}.",
                                  alternatives=[f"I've cancelled {target['label']}."])
            except (ArrError, OverseerrError, CancellationRefused):
                pass
        outcome = "uncertain" if stage == "prepared" else "partial"
        self.store.cancellation_progress(operation_id, stage, outcome)
        message = (f"The download for {target['label']} stopped, but I couldn't verify removal from Overseerr and Radarr."
                   if stage in {"queue_removed", "request_removed", "radarr_removed"} else
                   f"I couldn't verify the full cancellation of {target['label']}.")
        return result(f"movie_cancellation_{outcome}", message + " Ask me to try cancelling it again.", False,
                      alternatives=[message + " You can ask me to try cancelling it again."])

    def reconcile(self, target, client_id, operation_id):
        request, movie, queue = self._inspect(target, client_id)
        radarr_done = movie is None or (not target.get("remove_radarr") and not movie["monitored"])
        if request is None and not queue and radarr_done:
            self.store.cancellation_progress(operation_id, "complete", "complete")
            return True
        return False

    def status(self, movie, client_id):
        tracked = self.store.owned_request(client_id, movie.media_id) if self.store is not None else None
        if not tracked or tracked["cancellation_state"] == "active":
            return None
        operation = self.store.cancellation(client_id, movie.media_id)
        if tracked["cancellation_state"] != "cancelled" and operation:
            try:
                self.reconcile(operation["target"], client_id, operation["id"])
            except (ArrError, OverseerrError, CancellationRefused):
                pass
            tracked = self.store.owned_request(client_id, movie.media_id)
        if tracked["cancellation_state"] == "cancelled":
            return result("movie_status", f"The request for {movie.label} was cancelled.",
                          alternatives=[f"The movie request for {movie.label} has been cancelled."])
        return result("movie_status", f"Cancellation of {movie.label} is still unverified. Ask me to try cancelling it again.",
                      alternatives=[f"I still can't verify the cancellation of {movie.label}. You can ask me to try cancelling it again."])

    def before_rerequest(self, movie, client_id):
        """Only a verified cancelled attempt may bypass stale Overseerr status."""
        tracked = self.store.request_record(client_id, movie.media_id)
        if not tracked:
            return False
        operation = self.store.cancellation(client_id, movie.media_id)
        prepared = (not tracked["confirmed"] and operation and operation["outcome"] == "complete"
                    and tracked["generation"] == operation["generation"] + 1)
        if tracked["cancellation_state"] == "active" and not prepared:
            return False
        if tracked["cancellation_state"] != "cancelled" and not prepared:
            raise CancellationRefused("Please resolve the outstanding cancellation before requesting that movie again.")
        request, radarr_movie, queue = self._inspect(operation["target"], client_id, prepared_rerequest=bool(prepared))
        if request is not None or queue or (operation["target"].get("remove_radarr") and radarr_movie is not None):
            raise CancellationRefused("The previous cancellation is no longer verified. Please check Overseerr and Radarr.")
        return True

    def enable_rerequest(self, movie):
        radarr_movie = self.radarr.find_movie(movie.media_id)
        if radarr_movie and not radarr_movie["monitored"]:
            self.radarr.set_monitored(radarr_movie["id"], True)
            if not self.radarr.find_movie(movie.media_id)["monitored"]:
                raise ArrError("I couldn't enable the new movie request in Radarr.")
