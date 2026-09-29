"""Private client HTTP contract, independent of the AI-node wire schemas."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Operation = Literal["set_display", "set_brightness", "get_status", "shutdown", "reboot", "get_volume", "set_volume", "adjust_volume", "mute", "unmute"]


class Message(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Session(Message):
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class Command(Session):
    command_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: Operation
    state: Literal["on", "off"] | None = None
    percent: int | None = Field(default=None, ge=0, le=100)
    delta: int | None = Field(default=None, ge=-20, le=20)

    @model_validator(mode="after")
    def scoped_arguments(self):
        if (self.state is not None) != (self.operation == "set_display"):
            raise ValueError("Invalid control arguments")
        if (self.percent is not None) != (self.operation in {"set_brightness", "set_volume"}):
            raise ValueError("Invalid control arguments")
        if (self.delta is not None) != (self.operation == "adjust_volume") or self.delta == 0:
            raise ValueError("Invalid control arguments")
        return self


class VolumeStatus(Message):
    sink: str = Field(min_length=1)
    channels: dict[str, int] = Field(min_length=1)
    muted: bool

    @model_validator(mode="after")
    def valid_channels(self):
        if any(not name or type(percent) is not int or percent < 0
               for name, percent in self.channels.items()):
            raise ValueError("Invalid audio channel levels")
        return self


class Status(Message):
    display_available: bool
    display_enabled: bool | None = None
    master_brightness_percent: int | None = Field(default=None, ge=0, le=100)
    uptime_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cpu_temperature_c: float | None = Field(default=None, ge=-40, le=150, allow_inf_nan=False)

    @model_validator(mode="after")
    def renderer_fields(self):
        fields = (self.display_enabled, self.master_brightness_percent)
        if self.display_available and any(value is None for value in fields):
            raise ValueError("Incomplete renderer status")
        if not self.display_available and any(value is not None for value in fields):
            raise ValueError("Unavailable renderer state")
        return self


class Result(Session):
    success: bool
    error: Literal["display_unavailable", "power_unavailable", "command_invalid", "command_expired", "control_failed", "audio_unavailable"] | None = None
    status: Status | None = None
    volume: VolumeStatus | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.success == (self.error is not None):
            raise ValueError("Inconsistent control result")
        if not self.success and (self.status is not None or self.volume is not None):
            raise ValueError("Failed control cannot claim a status")
        return self
