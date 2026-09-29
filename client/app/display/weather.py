"""Cached Open-Meteo data for the native display; never called by rendering."""

import asyncio
from dataclasses import dataclass
from enum import IntEnum
import json
import math
import os
import socket
from threading import Event, Thread
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import transport

URL = "https://api.open-meteo.com/v1/forecast"
MAX_RESPONSE_BYTES = 32768
MAX_AGE_SECONDS = 3600
MAX_LEASE_SECONDS = 1800
RETRY_SECONDS = 60
PUBLISH_SECONDS = 60


class Condition(IntEnum):
    CLEAR = 0
    PARTLY_CLOUDY = 1
    CLOUDY = 2
    RAIN = 3
    HEAVY_RAIN = 4
    THUNDERSTORM = 5
    SNOW = 6
    FOG = 7


_CONDITIONS = {
    **dict.fromkeys((0,), Condition.CLEAR),
    **dict.fromkeys((1, 2), Condition.PARTLY_CLOUDY),
    **dict.fromkeys((3,), Condition.CLOUDY),
    **dict.fromkeys((45, 48), Condition.FOG),
    **dict.fromkeys((51, 53, 55, 56, 57, 61, 63, 80, 81), Condition.RAIN),
    **dict.fromkeys((65, 66, 67, 82), Condition.HEAVY_RAIN),
    **dict.fromkeys((71, 73, 75, 77, 85, 86), Condition.SNOW),
    **dict.fromkeys((95, 96, 97, 99), Condition.THUNDERSTORM),
}


@dataclass(frozen=True)
class WeatherConfig:
    latitude: float = 52.2772
    longitude: float = 10.5244
    timezone: str = "Europe/Berlin"
    refresh_seconds: int = 600

    @classmethod
    def from_environment(cls):
        try:
            config = cls(
                float(os.environ.get("CUBE_WEATHER_LATITUDE", "52.2772")),
                float(os.environ.get("CUBE_WEATHER_LONGITUDE", "10.5244")),
                os.environ.get("CUBE_WEATHER_TIMEZONE", "Europe/Berlin"),
                int(os.environ.get("CUBE_WEATHER_REFRESH_SECONDS", "600")),
            )
            if (not math.isfinite(config.latitude) or not -90 <= config.latitude <= 90
                    or not math.isfinite(config.longitude) or not -180 <= config.longitude <= 180
                    or not 120 <= config.refresh_seconds <= 1800):
                raise ValueError("weather configuration outside allowed bounds")
            ZoneInfo(config.timezone)
            return config
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("invalid weather configuration") from error

    def request_params(self):
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "temperature_unit": "celsius",
            "current": "temperature_2m,weather_code,is_day",
            "hourly": "precipitation_probability",
            "timeformat": "unixtime",
            "forecast_hours": 2,
            "past_hours": 1,
        }


@dataclass(frozen=True)
class WeatherSnapshot:
    temperature_c10: int
    precipitation_probability: int
    condition: Condition
    is_day: bool
    updated_at: float


def _number(value, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError("invalid weather number")
    if not math.isfinite(value):
        raise ValueError("invalid weather number")
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid weather integer")
    return value


def parse_response(data, config, now):
    if type(data) is not dict or data.get("timezone") != config.timezone:
        raise ValueError("invalid weather timezone")
    current, hourly = data["current"], data["hourly"]
    if type(current) is not dict or type(hourly) is not dict:
        raise ValueError("invalid weather structure")
    current_time = _integer(current["time"], 0, 4_102_444_800)
    temperature = _number(current["temperature_2m"], -100, 100)
    code = _integer(current["weather_code"], 0, 999)
    daylight = _integer(current["is_day"], 0, 1)
    timestamps, probabilities = hourly["time"], hourly["precipitation_probability"]
    if (type(timestamps) is not list or type(probabilities) is not list
            or len(timestamps) != len(probabilities) or not 1 <= len(timestamps) <= 4):
        raise ValueError("invalid hourly data")
    matches = [index for index, timestamp in enumerate(timestamps)
               if _integer(timestamp, 0, 4_102_444_800) <= current_time < timestamp + 3600]
    if len(matches) != 1:
        raise ValueError("current hour missing or ambiguous")
    probability = _number(probabilities[matches[0]], 0, 100)
    # Round halves away from zero, matching the native integer display contract.
    temperature_c10 = math.floor(temperature * 10 + .5) if temperature >= 0 else -math.floor(-temperature * 10 + .5)
    return WeatherSnapshot(temperature_c10, math.floor(probability + .5),
                           _CONDITIONS.get(code, Condition.CLOUDY), bool(daylight), now)


def encode_message(snapshot, now):
    age = max(0, now - snapshot.updated_at)
    lease = min(MAX_LEASE_SECONDS, math.floor(MAX_AGE_SECONDS - age))
    if lease <= 0:
        return None
    message = (f"weather v1 {snapshot.temperature_c10} "
               f"{snapshot.precipitation_probability} {int(snapshot.condition)} "
               f"{int(snapshot.is_day)} {lease}")
    assert len(message) < 128
    return message


class WeatherProvider:
    def __init__(self, config=None, *, clock=time.monotonic, send=transport.send_state):
        self.config = config if config is not None else WeatherConfig.from_environment()
        self.clock = clock
        self.send = send
        self.snapshot = None  # worker-owned; never read from the audio thread
        self.stopped = Event()
        self.loop = None
        self.task = None
        self.thread = None
        self._wake_handle = None

    def _pulse(self):
        # Keep cross-thread cancellation prompt on event loops whose selector
        # does not wake immediately for call_soon_threadsafe.
        if not self.stopped.is_set():
            self._wake_handle = self.loop.call_later(.5, self._pulse)

    async def fetch(self, client):
        async with client.stream("GET", URL, params=self.config.request_params()) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("weather response too large")
        return parse_response(json.loads(body), self.config, self.clock())

    def publish(self, channel, now):
        if self.snapshot is None:
            return False
        message = encode_message(self.snapshot, now)
        if message is None:
            return False
        try:
            self.send(channel, message)
        except OSError:
            return False
        return True

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        self._wake_handle = self.loop.call_later(.5, self._pulse)
        next_fetch = next_publish = 0.0
        timeout = httpx.Timeout(5, connect=3)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                             trust_env=False) as client:
                    while not self.stopped.is_set():
                        now = self.clock()
                        if now >= next_fetch:
                            try:
                                async with asyncio.timeout(10):
                                    self.snapshot = await self.fetch(client)
                                next_fetch = self.clock() + self.config.refresh_seconds
                                next_publish = 0
                            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, OSError):
                                next_fetch = self.clock() + RETRY_SECONDS
                        now = self.clock()
                        if now >= next_publish:
                            self.publish(channel, now)
                            next_publish = now + PUBLISH_SECONDS
                        await asyncio.sleep(max(0, min(next_fetch, next_publish) - self.clock()))
        except asyncio.CancelledError:
            pass
        finally:
            self._wake_handle.cancel()

    def start(self):
        if self.thread is not None:
            return
        self.thread = Thread(target=lambda: asyncio.run(self._run()), name="cube-weather", daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        if self.loop is not None and self.task is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread is not None:
            self.thread.join(timeout=12)
