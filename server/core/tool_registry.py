"""Explicit tool metadata and validated bindings; execution is a separate stage."""
from dataclasses import dataclass
import inspect
from typing import Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from .schemas import ToolDefinition, ToolIntent, ToolResult


class ToolArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


@dataclass(frozen=True)
class ToolContext:
    """Trusted backend context, never populated from model arguments."""
    client_id: str
    control_session: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class ValidatedTool:
    name: str
    arguments: ToolArguments
    handler: Callable[[ToolArguments, ToolContext], ToolResult]


class ToolValidationError(Exception):
    def __init__(self, reason):
        super().__init__("The requested tool could not be validated.")
        self.reason = reason


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._local_only: set[str] = set()
        self._bindings: dict[str, tuple[type[ToolArguments], Callable]] = {}

    def register(self, tool: ToolDefinition, *, arguments_model=None, handler=None, ai_visible=True) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        definition = tool.model_copy(deep=True)
        if arguments_model is not None or handler is not None:
            if (not isinstance(arguments_model, type)
                    or not issubclass(arguments_model, ToolArguments)
                    or arguments_model.model_config.get("extra") != "forbid"
                    or arguments_model.model_config.get("strict") is not True):
                raise ValueError("Executable tools require a strict ToolArguments model")
            if (not callable(handler) or inspect.iscoroutinefunction(handler)
                    or inspect.iscoroutinefunction(getattr(handler, "__call__", None))):
                raise ValueError("Executable tools require a synchronous handler")
            if "client_id" in arguments_model.model_fields:
                raise ValueError("Client identity belongs to backend ToolContext")
            # One source of truth for both advertised and validated arguments.
            definition.parameters = arguments_model.model_json_schema(mode="validation")
            self._bindings[tool.name] = (arguments_model, handler)
        self._tools[tool.name] = definition
        if not ai_visible:
            self._local_only.add(tool.name)

    def get(self, name: str) -> ToolDefinition:
        return self._tools[name].model_copy(deep=True)

    def list_tools(self) -> list[ToolDefinition]:
        return [self.get(name) for name in self._tools]

    def list_executable_tools(self) -> list[ToolDefinition]:
        return [self.get(name) for name in self._bindings if name not in self._local_only]

    def validate_intent(self, intent: ToolIntent) -> ValidatedTool:
        if intent.tool not in self._tools:
            raise ToolValidationError("unknown_tool")
        if intent.tool not in self._bindings:
            raise ToolValidationError("not_executable")
        model, handler = self._bindings[intent.tool]
        try:
            arguments = model.model_validate(intent.arguments, strict=True, extra="forbid")
        except ValidationError:
            # Do not expose rejected argument values or validator exception text.
            raise ToolValidationError("invalid_arguments") from None
        return ValidatedTool(intent.tool, arguments, handler)
