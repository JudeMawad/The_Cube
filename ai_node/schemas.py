"""AI HTTP contracts. Keep aligned with server/core/schemas.py via contract tests.

These are intentionally separate from the unchanged production /voice dictionaries.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Capabilities(WireModel):
    transcribe: bool
    process: bool


class HealthResult(WireModel):
    status: Literal["ok"]
    capabilities: Capabilities


class TranscriptionResult(WireModel):
    text: str
    language: str
    processing_time: float = Field(ge=0, allow_inf_nan=False)


class ToolDefinition(WireModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, JsonValue]


class ConversationResult(WireModel):
    type: Literal["conversation"]
    response: str = Field(min_length=1, max_length=500, pattern=r"\S")


class ToolIntent(WireModel):
    type: Literal["tool"]
    tool: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


ProcessResult = Annotated[ConversationResult | ToolIntent, Field(discriminator="type")]
process_result_adapter = TypeAdapter(ProcessResult)


class ToolResult(WireModel):
    """Facts returned by backend feature code, never proof supplied by an LLM."""
    tool: str = Field(min_length=1)
    success: bool
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None

# Shared process envelopes only; history/state management lives in the backend.
ConversationText = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
PendingDecision = Literal["confirm", "reject", "select", "title"]


class ChatMessage(WireModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def check_content(self):
        if not self.content.strip() or (self.role == "assistant" and len(self.content) > 500):
            raise ValueError("Invalid history message length or blank content")
        return self


class MediaMovie(WireModel):
    media_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    year: str | None = None


class PendingMedia(WireModel):
    pending_id: str = Field(min_length=1)
    intent: Literal["request", "availability", "status", "cancel_download"]
    state: Literal["title", "choose", "offer", "cancel_download"]
    candidates: list[MediaMovie] = Field(default_factory=list, max_length=3)
    permitted_decisions: list[PendingDecision] = Field(default_factory=list)
    expires_in: float = Field(ge=0, allow_inf_nan=False)


class MediaContext(WireModel):
    pending: PendingMedia | None = None
    remembered_movie: MediaMovie | None = None
    last_requested_movie: MediaMovie | None = None


class ProcessRequest(WireModel):
    transcript: str = Field(min_length=1, pattern=r"\S")
    client_id: str = Field(min_length=1)
    tools: list[ToolDefinition] = Field(default_factory=list)
    phase: Literal["interpret", "respond"] = "interpret"
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)
    context: MediaContext | None = None
    tool_result: ToolResult | None = None
    response_options: list[ConversationText] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_phase(self):
        if self.phase == "interpret":
            if self.tool_result is not None or self.response_options:
                raise ValueError("Interpretation cannot include tool results or response options")
        elif self.tool_result is None or not self.response_options or self.tools:
            raise ValueError("Response generation requires a tool result and options, and no tools")
        return self
