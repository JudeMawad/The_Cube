"""Strict AI contracts over public, structured backend movie operations."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from core.schemas import MediaContext, ToolDefinition, ToolResult
from core.tool_registry import ToolArguments


Title = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
Year = Annotated[str, Field(min_length=4, max_length=4, pattern=r"^(?:18|19|20)[0-9]{2}$")]
Token = Annotated[str, Field(min_length=1, max_length=64)]


class MovieArguments(ToolArguments):
    title: Title | None = None
    year: Year | None = None

    @model_validator(mode="after")
    def check_year(self):
        if self.year is not None and self.title is None:
            raise ValueError("Year requires a title")
        return self


class TargetArguments(MovieArguments):
    reference: Literal["remembered", "last_requested"] | None = None

    @model_validator(mode="after")
    def check_reference(self):
        # The semantic default is applied by the operation, after validation.
        # Explicit null is still an explicitly supplied reference field.
        if self.title is not None and "reference" in self.model_fields_set:
            raise ValueError("Specify a title or a reference, not both")
        return self


class PendingArguments(MovieArguments):
    pending_id: Token
    decision: Literal["confirm", "reject", "select", "title"]
    media_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_payload(self):
        if ((self.decision == "select") != (self.media_id is not None)
                or (self.decision == "title") != (self.title is not None)):
            raise ValueError("Decision payload does not match decision")
        return self


class CorrectionArguments(MovieArguments):
    title: Title
    pending_id: Token | None = None


class EndArguments(ToolArguments):
    forget: bool = False


BINDINGS = [
    ("media.request_movie", "Request a movie through backend title/year resolution; omit title to ask for it.", MovieArguments, "request_movie"),
    ("media.check_availability", "Check availability by optional title/year; an unavailable movie offers a request without submitting it.", MovieArguments, "check_availability"),
    ("media.movie_status", "Read status by title/year or remembered/last_requested reference; no target defaults to remembered.", TargetArguments, "movie_status"),
    ("media.cancel_movie", "Begin owned-download cancellation by title/year or remembered/last_requested reference; defaults to remembered and requires separate confirmation.", TargetArguments, "begin_movie_cancellation"),
    ("media.respond_to_pending", "Answer the current choice using its exact pending_id and permitted decision; select only a listed media_id, or provide title/year for a title reply.", PendingArguments, "respond_to_pending"),
    ("media.correct_movie", "Correct to a replacement title/year; include pending_id if present. Earlier requests remain unchanged and new requests require confirmation.", CorrectionArguments, "correct_movie"),
    ("media.end_conversation", "End dialogue without cancelling downloads; optionally forget the remembered movie.", EndArguments, "end_conversation"),
]


def tool_definitions():
    return [ToolDefinition(name=name, description=description, parameters=model.model_json_schema())
            for name, description, model, _ in BINDINGS]


class MediaTools:
    def __init__(self, commands):
        self.commands = commands

    def context(self, client_id):
        return MediaContext.model_validate(self.commands.media_context(client_id))

    def register(self, registry):
        for definition, (_, _, model, method) in zip(tool_definitions(), BINDINGS):
            def execute(arguments, context, name=definition.name, method=method):
                result = getattr(self.commands, method)(
                    context.client_id, **arguments.model_dump(exclude_unset=True))
                return ToolResult(tool=name, success=result["success"], data={"reply": result})
            registry.register(definition, arguments_model=model, handler=execute)
