"""Open-Meteo handling is tested without live HTTP or a renderer socket."""
import asyncio
import json
import math
import os
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from display.weather import (
    Condition, MAX_RESPONSE_BYTES, WeatherConfig, WeatherProvider,
    encode_message, parse_response,
)


def payload(*, temperature=18.45, code=61, day=1, probability=65):
    return {
        "timezone": "Europe/Berlin",
        "current": {"time": 3600 * 5 + 900, "temperature_2m": temperature,
                    "weather_code": code, "is_day": day},
        "hourly": {"time": [3600 * 4, 3600 * 5, 3600 * 6],
                   "precipitation_probability": [3, probability, 91]},
    }


class WeatherDataTests(unittest.TestCase):
    def setUp(self):
        self.config = WeatherConfig()

    def test_default_configuration_and_request(self):
        with patch.dict(os.environ, {}, clear=True):
            config = WeatherConfig.from_environment()
        self.assertEqual((config.latitude, config.longitude, config.timezone,
                          config.refresh_seconds), (52.2772, 10.5244, "Europe/Berlin", 600))
        self.assertEqual(config.request_params(), {
            "latitude": 52.2772, "longitude": 10.5244,
            "timezone": "Europe/Berlin", "temperature_unit": "celsius",
            "current": "temperature_2m,weather_code,is_day",
            "hourly": "precipitation_probability", "timeformat": "unixtime",
            "forecast_hours": 2, "past_hours": 1,
        })
        with patch.dict(os.environ, {
                "CUBE_WEATHER_LATITUDE": "40", "CUBE_WEATHER_LONGITUDE": "-74",
                "CUBE_WEATHER_TIMEZONE": "America/New_York",
                "CUBE_WEATHER_REFRESH_SECONDS": "900"}):
            override = WeatherConfig.from_environment()
        self.assertEqual((override.latitude, override.longitude, override.timezone,
                          override.refresh_seconds), (40, -74, "America/New_York", 900))
        for name, value in (("CUBE_WEATHER_LATITUDE", "nan"),
                            ("CUBE_WEATHER_LONGITUDE", "181"),
                            ("CUBE_WEATHER_TIMEZONE", "Invalid/Zone"),
                            ("CUBE_WEATHER_REFRESH_SECONDS", "0")):
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                with self.assertRaises(ValueError):
                    WeatherConfig.from_environment()

    def test_parsing_timestamp_and_temperature(self):
        snapshot = parse_response(payload(), self.config, 100)
        self.assertEqual((snapshot.temperature_c10, snapshot.precipitation_probability,
                          snapshot.condition, snapshot.is_day, snapshot.updated_at),
                         (185, 65, Condition.RAIN, True, 100))
        self.assertEqual(parse_response(payload(temperature=-3.55, day=0),
                                        self.config, 110).temperature_c10, -36)
        self.assertEqual(parse_response(payload(probability=0), self.config, 0)
                         .precipitation_probability, 0)
        self.assertEqual(parse_response(payload(probability=100), self.config, 0)
                         .precipitation_probability, 100)
        # Unix timestamps choose the right interval even around local DST changes.
        dst = payload()
        dst["current"]["time"] = 1792891800
        dst["hourly"]["time"] = [1792888200, 1792891800, 1792895400]
        self.assertEqual(parse_response(dst, self.config, 0).precipitation_probability, 65)

    def test_condition_groups_and_unknown_code(self):
        groups = {
            Condition.CLEAR: [0], Condition.PARTLY_CLOUDY: [1, 2],
            Condition.CLOUDY: [3, 999], Condition.FOG: [45, 48],
            Condition.RAIN: [51, 53, 55, 56, 57, 61, 63, 80, 81],
            Condition.HEAVY_RAIN: [65, 66, 67, 82],
            Condition.SNOW: [71, 73, 75, 77, 85, 86],
            Condition.THUNDERSTORM: [95, 96, 97, 99],
        }
        for condition, codes in groups.items():
            for code in codes:
                with self.subTest(code=code):
                    self.assertEqual(parse_response(payload(code=code), self.config, 0)
                                     .condition, condition)

    def test_malformed_and_invalid_payloads(self):
        cases = [None, [], {}, {**payload(), "timezone": "GMT"},
                 {**payload(), "current": {}}, {**payload(), "hourly": {}},
                 payload(temperature="18"), payload(temperature=float("nan")),
                 payload(temperature=101), payload(temperature=True),
                 payload(temperature=10**1000),
                 payload(code=1.5), payload(code=True), payload(day=2),
                 payload(probability=-1), payload(probability=101),
                 payload(probability=float("inf")), payload(probability=True)]
        missing = payload()
        missing["hourly"]["time"] = [0, 3600, 7200]
        cases.append(missing)
        duplicate = payload()
        duplicate["hourly"]["time"] = [18000, 18000, 21600]
        cases.append(duplicate)
        unequal = payload()
        unequal["hourly"]["precipitation_probability"] = [1]
        cases.append(unequal)
        for case in cases:
            with self.subTest(case=case), self.assertRaises((ValueError, KeyError, TypeError)):
                parse_response(case, self.config, 0)

    def test_message_and_shrinking_lease(self):
        snapshot = parse_response(payload(temperature=-3.55), self.config, 100)
        self.assertEqual(encode_message(snapshot, 100), "weather v1 -36 65 3 1 1800")
        self.assertEqual(encode_message(snapshot, 100 + 3500), "weather v1 -36 65 3 1 100")
        self.assertIsNone(encode_message(snapshot, 3700))
        self.assertLess(len(encode_message(snapshot, 100).encode("ascii")), 128)

    def test_publish_retains_snapshot_on_socket_error(self):
        provider = WeatherProvider(self.config, clock=lambda: 100, send=Mock(side_effect=OSError))
        provider.snapshot = parse_response(payload(), self.config, 100)
        self.assertFalse(provider.publish(Mock(), 100))
        self.assertIsNotNone(provider.snapshot)


class WeatherHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_request_and_failures(self):
        requests = []

        def reply(request):
            requests.append(request)
            return httpx.Response(200, json=payload())

        provider = WeatherProvider(WeatherConfig(), clock=lambda: 100)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            snapshot = await provider.fetch(client)
        self.assertEqual(snapshot.temperature_c10, 185)
        self.assertEqual(requests[0].url.host, "api.open-meteo.com")
        self.assertEqual(requests[0].url.params["timezone"], "Europe/Berlin")
        self.assertEqual(requests[0].url.params["forecast_hours"], "2")
        for response in (httpx.Response(503), httpx.Response(200, content=b"invalid"),
                         httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
                with self.assertRaises((httpx.HTTPError, ValueError, json.JSONDecodeError)):
                    await provider.fetch(client)

        def timeout(request):
            raise httpx.ConnectTimeout("timeout", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(httpx.ConnectTimeout):
                await provider.fetch(client)

    async def test_retry_and_last_known_good(self):
        now = [100.0]
        provider = WeatherProvider(WeatherConfig(), clock=lambda: now[0], send=Mock())
        good = parse_response(payload(), provider.config, now[0])
        calls = []

        async def fetch(_):
            calls.append(now[0])
            if len(calls) == 1:
                return good
            raise httpx.ConnectTimeout("timeout")

        async def sleep(delay):
            if len(calls) >= 2:
                provider.stopped.set()
            now[0] += delay

        with patch.object(provider, "fetch", side_effect=fetch), \
             patch("display.weather.asyncio.sleep", side_effect=sleep):
            await provider._run()
        self.assertEqual(calls, [100.0, 700.0])
        self.assertIs(provider.snapshot, good)
        self.assertEqual(now[0], 760.0)


class WeatherShutdownTests(unittest.TestCase):
    def test_close_cancels_pending_request(self):
        started = Event()

        async def blocked(_provider, _client):
            started.set()
            await asyncio.Future()

        with patch.object(WeatherProvider, "fetch", blocked):
            provider = WeatherProvider(WeatherConfig())
            provider.start()
            self.assertTrue(started.wait(2))
            began = time.monotonic()
            provider.close()
            self.assertFalse(provider.thread.is_alive())
            self.assertLess(time.monotonic() - began, 2)
            provider.close()
