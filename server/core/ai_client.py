"""Async AI HTTP boundary; backend transcription selection owns fallback policy."""
import asyncio

import httpx
from pydantic import ValidationError

from .config import AINodeConfig
from .cancellation import check, headers
from .schemas import HealthResult, ProcessRequest, ProcessResult, TranscriptionResult, process_result_adapter


class AINodeError(Exception):
    """Safe failure at the remote boundary; no upstream bodies or credentials."""

    def __init__(self, message, *, reason="request_failed", status_code=None):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


class AIClient:
    HEALTH_TIMEOUT = 1.0

    def __init__(self, config: AINodeConfig | None = None, *, transport=None):
        self.config = config if config is not None else AINodeConfig.from_env()
        self.transport = transport

    async def _request(self, method, path, *, timeout, connect_timeout=None, **kwargs):
        check()
        kwargs["headers"] = {**kwargs.get("headers", {}), **headers()}
        if self.config.url is None:
            raise AINodeError("AI node is disabled.", reason="disabled")
        phase_timeout = httpx.Timeout(
            timeout, connect=timeout if connect_timeout is None else connect_timeout,
        )
        try:
            # The outer deadline bounds all async HTTP phases together. Cancellation
            # unwinds the request and closes the client before fallback can start.
            # No blocking HTTP worker, detached task, retry, probe, or redirect.
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=phase_timeout,
                    follow_redirects=False, trust_env=False,
                ) as client:
                    response = await client.request(method, self.config.url + path, **kwargs)
                    check()
                    response.raise_for_status()
                    return response.json()
        except (httpx.TimeoutException, TimeoutError):
            raise AINodeError("AI node request timed out.", reason="timeout") from None
        except httpx.HTTPStatusError as error:
            raise AINodeError(
                "AI node returned an unsuccessful HTTP status.",
                reason="http_status", status_code=error.response.status_code,
            ) from None
        except httpx.HTTPError:
            raise AINodeError("AI node transport failed.", reason="transport") from None
        except ValueError:
            raise AINodeError("AI node returned invalid JSON.", reason="invalid_response") from None

    async def ai_node_available(self, capability: str | None = None) -> bool:
        """Liveness by default; request 'transcribe' or 'process' for readiness."""
        if capability not in {None, "transcribe", "process"}:
            raise ValueError("Unknown AI capability")
        try:
            data = await self._request("GET", "/health", timeout=self.HEALTH_TIMEOUT)
            health = HealthResult.model_validate(data)
            return capability is None or getattr(health.capabilities, capability)
        except (AINodeError, ValidationError):
            return False

    async def transcribe_remote(self, audio: bytes, *, filename="command.wav") -> TranscriptionResult:
        timeout = self.config.transcription_timeout
        data = await self._request(
            "POST", "/transcribe", timeout=timeout, connect_timeout=min(0.25, timeout),
            files={"file": (filename, audio, "audio/wav")},
        )
        try:
            return TranscriptionResult.model_validate(data)
        except ValidationError:
            raise AINodeError(
                "AI node returned invalid transcription.", reason="invalid_response",
            ) from None

    async def process_remote(self, request: ProcessRequest) -> ProcessResult:
        timeout = self.config.process_timeout
        data = await self._request(
            "POST", "/process", timeout=timeout, connect_timeout=min(0.25, timeout),
            json=request.model_dump(mode="json"),
        )
        try:
            return process_result_adapter.validate_python(data)
        except ValidationError:
            raise AINodeError(
                "AI node returned an invalid process result.", reason="invalid_response",
            ) from None
