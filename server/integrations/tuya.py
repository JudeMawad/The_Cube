"""Backend-only Tuya Cloud adapter; imports and construction perform no I/O.

Run from the repository root for read-only diagnostics::

    PYTHONPATH=server server/.venv/bin/python -m integrations.tuya status all

Configuration is loaded once per client. Recreate the client after changing it.
"""

import argparse
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import re
from threading import RLock
import time
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlencode, urlsplit

import httpx


CONFIG_PATH = Path.home() / ".config/cube/tuya.env"
DEVICE_VARIABLES = MappingProxyType({
    "lamp": "TUYA_LAMP_PLUG_ID",
    "mirror": "TUYA_MIRROR_PLUG_ID",
    "mushroom": "TUYA_MUSHROOM_PLUG_ID",
})
TOKEN_ERRORS = frozenset({1010, 1011, 1012})
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z", re.ASCII)


class TuyaError(Exception):
    """Safe diagnostic, without upstream bodies, headers, or credentials."""


class TuyaConfigError(TuyaError):
    pass


class TuyaDeviceNotConfigured(TuyaConfigError):
    """A known logical alias has no configured Device ID."""


class TuyaDeviceError(TuyaError):
    pass


class TuyaCapabilityError(TuyaDeviceError):
    pass


class TuyaTransportError(TuyaError):
    """A transport or HTTP failure; status_code is absent for network failures."""

    def __init__(self, status_code=None):
        self.status_code = status_code
        super().__init__("Tuya HTTP request failed." if status_code is not None
                         else "Could not reach Tuya.")


class TuyaAPIError(TuyaError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Tuya rejected the request (code {code}).")


class TuyaAuthError(TuyaAPIError):
    pass


class TuyaResponseError(TuyaError):
    pass


class TuyaCommandUncertain(TuyaError):
    """The command may have reached the device; read its state before retrying."""


def _device_id(value):
    # Validate syntax, not a particular vendor's ID length or hex-only format.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", value):
        raise TuyaDeviceError("Invalid Tuya Device ID.")
    return value


def _header_value(value):
    return (isinstance(value, str) and bool(value)
            and all(33 <= ord(char) <= 126 for char in value))


@dataclass(frozen=True)
class Settings:
    access_id: str = field(repr=False)
    access_secret: str = field(repr=False)
    endpoint: str = field(repr=False)
    device_ids: Mapping[str, str] = field(repr=False)

    @classmethod
    def load(cls, path=None):
        path = CONFIG_PATH if path is None else Path(path)
        required = {"CUBE_TUYA_ACCESS_ID", "CUBE_TUYA_ACCESS_SECRET", "CUBE_TUYA_ENDPOINT"}
        allowed = required | set(DEVICE_VARIABLES.values())
        try:
            values = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                key = key.strip()
                if not sep or key not in allowed:
                    continue
                value = value.strip()
                if value[:1] in {"'", '"'}:
                    if len(value) < 2 or value[-1] != value[0]:
                        raise TuyaConfigError(f"Invalid Tuya configuration value: {key}.")
                    value = value[1:-1]
                if key in values:
                    raise TuyaConfigError(f"Duplicate Tuya configuration variable: {key}.")
                values[key] = value
        except (OSError, UnicodeError, ValueError):
            raise TuyaConfigError("Cannot read ~/.config/cube/tuya.env.") from None
        for key in sorted(required):
            if not _header_value(values.get(key)):
                raise TuyaConfigError(f"Missing or invalid Tuya configuration variable: {key}.")
        endpoint = values["CUBE_TUYA_ENDPOINT"].rstrip("/")
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment
                    or "?" in endpoint or "#" in endpoint or "\\" in endpoint):
                raise ValueError()
            # Validate the port as well, without ever echoing the URL.
            parsed.port
            httpx.URL(endpoint)
        except (ValueError, httpx.InvalidURL):
            raise TuyaConfigError("Invalid Tuya configuration variable: CUBE_TUYA_ENDPOINT.") from None
        devices = {}
        for alias, key in DEVICE_VARIABLES.items():
            value = values.get(key)
            if value:
                try:
                    devices[alias] = _device_id(value)
                except TuyaDeviceError:
                    raise TuyaConfigError(f"Invalid Tuya configuration variable: {key}.") from None
        return cls(values["CUBE_TUYA_ACCESS_ID"], values["CUBE_TUYA_ACCESS_SECRET"],
                   endpoint, MappingProxyType(devices))


@dataclass(frozen=True)
class DeviceInfo:
    id: str = field(repr=False)
    name: str
    category: str
    online: bool


class TuyaClient:
    def __init__(self, *, config_path=None, transport=None):
        self._config_path = config_path
        self._transport = transport
        self._settings = None
        self._http = None
        self._token = None
        self._expires_at = 0.0
        self._switch_codes = {}
        # Serialize requests/renewals so concurrent users never renew the same
        # rejected token twice. This client controls a small set of plugs.
        self._lock = RLock()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with self._lock:
            if self._http is not None:
                self._http.close()
                self._http = None
            self._token = None
            self._expires_at = 0.0
            self._switch_codes.clear()

    def _config(self):
        with self._lock:
            if self._settings is None:
                self._settings = Settings.load(self._config_path)
            return self._settings

    def resolve_device(self, device: str | None = None, *, device_id: str | None = None) -> str:
        if (device is None) == (device_id is None):
            raise TuyaDeviceError("Provide exactly one Tuya alias or explicit Device ID.")
        if device_id is not None:
            return _device_id(device_id)
        if not isinstance(device, str) or device not in DEVICE_VARIABLES:
            raise TuyaDeviceError("Unknown Tuya device alias; use lamp, mirror, or mushroom.")
        identity = self._config().device_ids.get(device)
        if identity is None:
            raise TuyaDeviceNotConfigured(f"Tuya device is not configured: {DEVICE_VARIABLES[device]}.")
        return identity

    def _send(self, method, path, *, token=None, params=None, body=None):
        """One signing/transport/envelope path for token and business requests.

        https://developer.tuya.com/en/docs/iot/new-singnature?id=Kbw0q34cs2e5g
        Sign the prepared URL and exact UTF-8 bytes passed to HTTPX.
        """
        settings = self._config()
        query = urlencode(sorted((params or {}).items()))
        url = httpx.URL(settings.endpoint + path + ("?" + query if query else ""))
        content = b"" if body is None else json.dumps(
            body, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        timestamp = str(int(time.time() * 1000))
        canonical = "\n".join((method, hashlib.sha256(content).hexdigest(),
                               "", url.raw_path.decode("ascii")))
        signature = hmac.new(
            settings.access_secret.encode("utf-8"),
            (settings.access_id + (token or "") + timestamp + canonical).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()
        headers = {"client_id": settings.access_id, "t": timestamp,
                   "sign_method": "HMAC-SHA256", "sign": signature,
                   "Accept": "application/json", "Content-Type": "application/json"}
        if token is not None:
            headers["access_token"] = token
        try:
            if self._http is None:
                self._http = httpx.Client(timeout=10, follow_redirects=False,
                                          trust_env=False, transport=self._transport)
            response = self._http.request(method, url, headers=headers, content=content)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            raise TuyaTransportError() from None
        if not 200 <= response.status_code < 300:
            raise TuyaTransportError(response.status_code)
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise TuyaResponseError("Tuya returned invalid JSON.") from None
        if not isinstance(data, dict) or type(data.get("success")) is not bool:
            raise TuyaResponseError("Tuya returned an invalid response envelope.")
        if not data["success"]:
            code = data.get("code")
            if isinstance(code, str) and re.fullmatch(r"[0-9]{1,10}", code):
                code = int(code)
            if type(code) is not int or not 0 <= code <= 2147483647:
                raise TuyaResponseError("Tuya returned an invalid error code.")
            error = TuyaAuthError if token is None or code in TOKEN_ERRORS | {1004, 1005} else TuyaAPIError
            raise error(code)
        if "result" not in data:
            raise TuyaResponseError("Tuya returned no result.")
        return data["result"]

    def _access_token(self):
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token
        self._token = None
        self._expires_at = 0.0
        started = time.monotonic()
        result = self._send("GET", "/v1.0/token", params={"grant_type": "1"})
        if (not isinstance(result, dict) or not _header_value(result.get("access_token"))
                or type(result.get("expire_time")) is not int or result["expire_time"] <= 0):
            raise TuyaResponseError("Tuya returned an invalid access token or lifetime.")
        lifetime = result["expire_time"]
        # Tuya lifetimes are seconds, not absolute timestamps. Account for the
        # request duration and renew early, including for short-lived tokens.
        try:
            self._expires_at = started + lifetime - min(30, lifetime / 10)
        except OverflowError:
            raise TuyaResponseError("Tuya returned an invalid token lifetime.") from None
        if time.monotonic() >= self._expires_at:
            raise TuyaResponseError("Tuya returned a token with no usable lifetime.")
        self._token = result["access_token"]
        return self._token

    def _request(self, method, path, *, body=None):
        with self._lock:
            for attempt in range(2):
                token = self._access_token()
                try:
                    return self._send(method, path, token=token, body=body)
                except TuyaAPIError as error:
                    if error.code in TOKEN_ERRORS:
                        self._token = None
                        self._expires_at = 0.0
                        if attempt == 0:
                            continue
                    raise
                except (TuyaTransportError, TuyaResponseError):
                    if method == "POST":
                        raise TuyaCommandUncertain(
                            "Tuya command outcome is unknown; read device state before retrying."
                        ) from None
                    raise

    def get_device_info(self, device: str | None = None, *, device_id: str | None = None) -> DeviceInfo:
        identity = self.resolve_device(device, device_id=device_id)
        data = self._request("GET", f"/v1.1/iot-03/devices/{identity}")
        if (not isinstance(data, dict) or data.get("id") != identity
                or not isinstance(data.get("name"), str)
                or not isinstance(data.get("category"), str)
                or type(data.get("online")) is not bool):
            raise TuyaResponseError("Tuya returned invalid device information.")
        # Do not expose raw device data, which can contain local keys or IPs.
        return DeviceInfo(identity, data["name"], data["category"], data["online"])

    def get_status(self, device: str | None = None, *, device_id: str | None = None) -> dict[str, object]:
        identity = self.resolve_device(device, device_id=device_id)
        data = self._request("GET", f"/v1.0/iot-03/devices/{identity}/status")
        if not isinstance(data, list):
            raise TuyaResponseError("Tuya returned an invalid device status.")
        status = {}
        for item in data:
            if (not isinstance(item, dict) or not isinstance(item.get("code"), str)
                    or not _CODE.fullmatch(item["code"]) or "value" not in item
                    or item["code"] in status):
                raise TuyaResponseError("Tuya returned an invalid or duplicate status entry.")
            status[item["code"]] = item["value"]
        return status

    def _switch_code(self, identity):
        with self._lock:
            if identity in self._switch_codes:
                return self._switch_codes[identity]
            data = self._request("GET", f"/v1.0/iot-03/devices/{identity}/functions")
            if not isinstance(data, dict) or not isinstance(data.get("functions"), list):
                raise TuyaResponseError("Tuya returned invalid device functions.")
            seen, candidates = set(), []
            for item in data["functions"]:
                if (not isinstance(item, dict) or not isinstance(item.get("code"), str)
                        or not _CODE.fullmatch(item["code"])
                        or not isinstance(item.get("type"), str) or item["code"] in seen):
                    raise TuyaResponseError("Tuya returned an invalid or duplicate device function.")
                code = item["code"]
                seen.add(code)
                if item["type"].lower() == "boolean" and (code == "switch" or code.startswith("switch_")):
                    candidates.append(code)
            if len(candidates) > 1:
                raise TuyaCapabilityError("Tuya device has multiple ambiguous writable switches.")
            if len(candidates) != 1 or candidates[0] not in {"switch", "switch_1"}:
                raise TuyaCapabilityError("Tuya device has no supported writable power switch.")
            self._switch_codes[identity] = candidates[0]
            return candidates[0]

    def is_on(self, device: str | None = None, *, device_id: str | None = None) -> bool:
        identity = self.resolve_device(device, device_id=device_id)
        code = self._switch_code(identity)
        value = self.get_status(device_id=identity).get(code)
        if type(value) is not bool:
            raise TuyaResponseError("Tuya power status is missing or is not a Boolean.")
        return value

    def set_power(self, device: str | None = None, power: bool | None = None,
                  *, device_id: str | None = None) -> None:
        """Set power; success confirms cloud acceptance, not physical completion."""
        if type(power) is not bool:
            raise TuyaDeviceError("Tuya power must be True or False.")
        identity = self.resolve_device(device, device_id=device_id)
        code = self._switch_code(identity)
        result = self._request("POST", f"/v1.0/iot-03/devices/{identity}/commands",
                               body={"commands": [{"code": code, "value": power}]})
        if result is False:
            raise TuyaError("Tuya did not accept the power command.")
        if result is not True:
            raise TuyaCommandUncertain("Tuya command outcome is unknown; read device state before retrying.")

    def turn_on(self, device: str | None = None, *, device_id: str | None = None) -> None:
        self.set_power(device, True, device_id=device_id)

    def turn_off(self, device: str | None = None, *, device_id: str | None = None) -> None:
        self.set_power(device, False, device_id=device_id)


def main(argv=None):
    """Read-only CLI: all HTTP operations, including token acquisition, are GETs."""
    parser = argparse.ArgumentParser(description="Read Tuya plug visibility and reported power state.")
    parser.add_argument("command", nargs="?", choices=["status"], default="status")
    parser.add_argument("device", nargs="?", choices=["all", *DEVICE_VARIABLES], default="all")
    args = parser.parse_args(argv)
    aliases = list(DEVICE_VARIABLES) if args.device == "all" else [args.device]
    failed = False
    with TuyaClient() as client:
        for alias in aliases:
            try:
                info = client.get_device_info(alias)
                power = "on" if client.is_on(alias) else "off"
                connection = "online" if info.online else "offline (last reported state)"
                print(f"{alias}: visible, {connection}, power={power}")
            except TuyaError as error:
                failed = True
                print(f"{alias}: error: {error}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
