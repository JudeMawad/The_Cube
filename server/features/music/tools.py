"""Validated local music intents; names are resolved by the backend."""
from typing import Annotated, Literal
from pydantic import Field, model_validator

from core.schemas import ToolDefinition, ToolResult
from core.tool_registry import ToolArguments


class EmptyArguments(ToolArguments):
    pass


class VolumeArguments(ToolArguments):
    percent: int = Field(ge=0, le=100)


class PlayArguments(ToolArguments):
    query: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]
    kind: Literal["track", "artist", "playlist", "auto"]
    artist: Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S")] | None = None
    personal: bool = False
    selection: Literal["exact", "top"] = "exact"

    @model_validator(mode="after")
    def scoped_fields(self):
        if self.artist is not None and self.kind not in {"track", "auto"}:
            raise ValueError("Artist qualifier requires a track")
        if self.personal and self.kind not in {"playlist", "auto"}:
            raise ValueError("Personal lookup requires a playlist")
        if self.selection == "top" and (self.kind != "track" or self.artist is not None or self.personal):
            raise ValueError("Top selection requires an unqualified track")
        return self


BINDINGS = [
    ("play_music", "Resume Spotify on Cube or transfer the existing session to Cube.", EmptyArguments),
    ("resume", "Resume Spotify music on Cube.", EmptyArguments),
    ("pause", "Pause or stop Spotify music on Cube without clearing its queue.", EmptyArguments),
    ("next", "Skip to the next Spotify song on Cube.", EmptyArguments),
    ("previous", "Play the previous Spotify song on Cube.", EmptyArguments),
    ("set_volume", "Set Spotify MUSIC user volume only. Unqualified volume belongs to Cube assistant volume.", VolumeArguments),
    ("play_request", "Resolve a deterministic named music request and play its validated URI.", PlayArguments),
]


def register_tools(registry, *, commands):
    for operation, description, model in BINDINGS:
        name = "music." + operation
        def execute(arguments, context, operation=operation, name=name):
            result = commands.execute(context, operation, arguments.model_dump())
            return ToolResult(tool=name, success=result["success"], error=result.get("error"), data={"reply": result})
        registry.register(ToolDefinition(name=name, description=description, parameters=model.model_json_schema()),
                          arguments_model=model, handler=execute, ai_visible=False)
