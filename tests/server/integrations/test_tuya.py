"""Offline Tuya tests. Every HTTP request uses an in-memory mock transport."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import traceback
import unittest
from unittest.mock import patch

import httpx

from integrations import tuya


CONFIG = """# Synthetic fixtures, never real credentials or IDs.
CUBE_TUYA_ACCESS_ID=ACCESS_TEST_ONLY
CUBE_TUYA_ACCESS_SECRET=SECRET_TEST_ONLY
CUBE_TUYA_ENDPOINT=https://tuya.invalid
TUYA_LAMP_PLUG_ID=lampDevice123
TUYA_MIRROR_PLUG_ID=mirrorDevice123
TUYA_MUSHROOM_PLUG_ID=mushroomDevice123
"""
IDS = {"lamp": "lampDevice123", "mirror": "mirrorDevice123", "mushroom": "mushroomDevice123"}
TOKEN = {"access_token": "TOKEN_TEST_ONLY", "expire_time": 7200,
         "refresh_token": "REFRESH_TEST_ONLY", "uid": "unused"}
FUNCTIONS = {"functions": [{"code": "switch_1", "type": "Boolean", "values": "{}"}]}
STATUS = [{"code": "switch_1", "value": True}, {"code": "cur_power", "value": 42}]
SENSITIVE = ("ACCESS_TEST_ONLY", "SECRET_TEST_ONLY", "TOKEN_TEST_ONLY", "REFRESH_TEST_ONLY")


def response(result):
    return httpx.Response(200, json={"success": True, "result": result})


class ConfigTests(unittest.TestCase):
    def load(self, content=CONFIG):
        with patch.object(Path, "read_text", return_value=content):
            return tuya.Settings.load()

    def test_path_and_file_authority(self):
        self.assertEqual(tuya.CONFIG_PATH, Path.home() / ".config/cube/tuya.env")
        with patch.dict("os.environ", {"CUBE_TUYA_ACCESS_ID": "WRONG"}):
            settings = self.load()
        self.assertEqual(settings.access_id, "ACCESS_TEST_ONLY")
        self.assertEqual(dict(settings.device_ids), IDS)
        for secret in SENSITIVE + tuple(IDS.values()):
            self.assertNotIn(secret, repr(settings))
        with self.assertRaises(TypeError):
            settings.device_ids["lamp"] = "changed"

    def test_blank_lines_comments_quotes_and_only_required_values(self):
        content = CONFIG.replace("ACCESS_TEST_ONLY", ' "ACCESS_TEST_ONLY" ').replace(
            "SECRET_TEST_ONLY", "'SECRET_TEST_ONLY'").replace(
            "https://tuya.invalid", "https://tuya.invalid/")
        settings = self.load("\n  # ignored\nUNRELATED='broken\nnot an assignment\n" + content + "\n")
        self.assertEqual(settings.access_secret, "SECRET_TEST_ONLY")
        self.assertEqual(settings.endpoint, "https://tuya.invalid")

    def test_assignments_are_data_not_shell_code(self):
        settings = self.load(CONFIG.replace("SECRET_TEST_ONLY", "'$(do-not-execute)'"))
        self.assertEqual(settings.access_secret, "$(do-not-execute)")

    def test_required_credentials_and_endpoint(self):
        for key in ("CUBE_TUYA_ACCESS_ID", "CUBE_TUYA_ACCESS_SECRET", "CUBE_TUYA_ENDPOINT"):
            for replacement in (None, "", "''", "\"\"", "'has space'"):
                with self.subTest(key=key, replacement=replacement):
                    lines = [line for line in CONFIG.splitlines() if not line.startswith(key + "=")]
                    if replacement is not None:
                        lines.append(key + "=" + replacement)
                    with self.assertRaises(tuya.TuyaConfigError) as caught:
                        self.load("\n".join(lines))
                    self.assertIn(key, str(caught.exception))

    def test_partial_device_configuration(self):
        content = "\n".join(line for line in CONFIG.splitlines() if not line.startswith("TUYA_"))
        self.assertEqual(dict(self.load(content).device_ids), {})
        self.assertEqual(dict(self.load(content + "\nTUYA_LAMP_PLUG_ID=lampDevice123\n").device_ids),
                         {"lamp": "lampDevice123"})
        self.assertNotIn("mirror", self.load(CONFIG.replace("mirrorDevice123", "")).device_ids)

    def test_invalid_values_and_duplicate_variables(self):
        for content in (CONFIG.replace("SECRET_TEST_ONLY", "'unclosed"),
                        CONFIG.replace("SECRET_TEST_ONLY", '"unclosed'),
                        CONFIG.replace("lampDevice123", "bad/id"),
                        CONFIG + "CUBE_TUYA_ACCESS_SECRET=duplicate\n",
                        CONFIG + "TUYA_LAMP_PLUG_ID=duplicate\n"):
            with self.subTest(content=content), self.assertRaises(tuya.TuyaConfigError):
                self.load(content)

    def test_invalid_endpoints(self):
        for endpoint in ("http://tuya.invalid", "no-url", "https://", "https://user:secret@tuya.invalid",
                         "https://tuya.invalid/path", "https://tuya.invalid?", "https://tuya.invalid?q=1",
                         "https://tuya.invalid#", "https://tuya.invalid#fragment",
                         "https://tuya.invalid:bad", "https://tuya.invalid:99999",
                         "https://tuya.invalid\\evil", "https://[bad"):
            with self.subTest(endpoint=endpoint), self.assertRaises(tuya.TuyaConfigError):
                self.load(CONFIG.replace("https://tuya.invalid", endpoint))

    def test_file_errors_are_safe(self):
        for error in (FileNotFoundError("SECRET_TEST_ONLY"), PermissionError("SECRET_TEST_ONLY"),
                      UnicodeError("SECRET_TEST_ONLY")):
            with patch.object(Path, "read_text", side_effect=error):
                with self.assertRaises(tuya.TuyaConfigError) as caught:
                    tuya.Settings.load()
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn("SECRET_TEST_ONLY", rendered)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.reader = patch.object(Path, "read_text", return_value=CONFIG).start()
        self.addCleanup(patch.stopall)
        clock = patch.object(tuya, "time").start()
        self.wall = clock.time
        self.wall.return_value = 1700000000.123
        self.monotonic = clock.monotonic
        self.monotonic.return_value = 100.0
        self.requests = []
        self.routes = {}
        self.tokens = []
        self.client = tuya.TuyaClient(transport=httpx.MockTransport(self.handle))
        self.addCleanup(self.client.close)

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/v1.0/token":
            result = self.tokens.pop(0) if self.tokens else response(TOKEN)
        else:
            route = self.routes.get(request.url.path)
            if isinstance(route, list):
                result = route.pop(0)
            elif route is not None:
                result = route
            elif request.url.path.endswith("/status"):
                result = response(STATUS)
            elif request.url.path.endswith("/functions"):
                result = response(FUNCTIONS)
            elif request.url.path.endswith("/commands"):
                result = response(True)
            else:
                result = response({"id": request.url.path.rsplit("/", 1)[-1],
                                   "name": "Test plug", "category": "cz", "online": True,
                                   "local_key": "DO_NOT_EXPOSE"})
        if isinstance(result, Exception):
            raise result
        return result

    def calls(self, suffix):
        return [req for req in self.requests if req.url.path.endswith(suffix)]

    def test_named_device_resolution_and_no_network(self):
        for alias, identity in IDS.items():
            self.assertEqual(self.client.resolve_device(alias), identity)
        self.assertEqual(self.requests, [])
        self.reader.assert_called_once()

    def test_unknown_aliases_never_become_ids(self):
        for alias in ("lmap", "lampDevice123", "Lamp", " lamp", "", "all", 1, []):
            with self.subTest(alias=alias), self.assertRaises(tuya.TuyaDeviceError):
                self.client.turn_on(alias)
        self.reader.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_missing_alias_id_and_explicit_id_operation(self):
        self.reader.return_value = CONFIG.replace("TUYA_MIRROR_PLUG_ID=mirrorDevice123", "")
        with self.assertRaises(tuya.TuyaConfigError) as caught:
            self.client.turn_on("mirror")
        self.assertIn("TUYA_MIRROR_PLUG_ID", str(caught.exception))
        self.assertEqual(self.requests, [])
        self.assertTrue(self.client.is_on("lamp"))
        self.assertTrue(self.client.is_on(device_id="explicitDevice456"))
        self.assertIn("explicitDevice456", self.requests[-1].url.path)

    def test_invalid_ids_and_target_combinations(self):
        for identity in ("", "a/b", "a?b", "a#b", "../x", " a", "a\n", "a%2Fb", "ü", "a" * 129, 123, []):
            with self.subTest(identity=identity), self.assertRaises(tuya.TuyaDeviceError):
                self.client.turn_on(device_id=identity)
        for kwargs in ({}, {"device": "lamp", "device_id": "explicitDevice456"}):
            with self.assertRaises(tuya.TuyaDeviceError):
                self.client.turn_on(**kwargs)
        self.assertEqual(self.requests, [])

    def test_token_and_get_signature_vectors(self):
        self.client.get_status("lamp")
        token, status = self.requests
        self.assertEqual(token.url.raw_path, b"/v1.0/token?grant_type=1")
        self.assertEqual(token.headers["sign"], "D5611F44EF775435D0E37BC5DD9FB10323AAB53DBCD81159C667017DDFCC774E")
        self.assertNotIn("access_token", token.headers)
        self.assertEqual(status.headers["sign"], "10EF43FC16687C2F097D0970B3D1DE14A31598CAAEB4D7FDABB2CB732894EF2C")
        self.assertEqual(status.headers["access_token"], "TOKEN_TEST_ONLY")
        for request in self.requests:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.content, b"")
            self.assertEqual(request.headers["client_id"], "ACCESS_TEST_ONLY")
            self.assertEqual(request.headers["t"], "1700000000123")
            self.assertEqual(request.headers["sign_method"], "HMAC-SHA256")
            self.assertEqual(request.extensions["timeout"], dict.fromkeys(("connect", "read", "write", "pool"), 10))

    def test_canonical_query_order_and_encoding(self):
        self.client._send("GET", "/v1.0/token", params={"z": "/", "a": "hello world"})
        request = self.requests[0]
        self.assertEqual(request.url.raw_path, b"/v1.0/token?a=hello+world&z=%2F")
        self.assertEqual(request.headers["sign"], "8CFDAEF8EAD22756C072EED4175D9904EEAA005EB7C9494740FF79E84F283B23")

    def test_transport_configuration(self):
        factory = httpx.Client
        with patch.object(tuya.httpx, "Client", wraps=factory) as create:
            self.client.get_status("lamp")
        self.assertEqual(create.call_args.kwargs["timeout"], 10)
        self.assertFalse(create.call_args.kwargs["follow_redirects"])
        self.assertFalse(create.call_args.kwargs["trust_env"])

    def test_token_reuse_across_devices_and_operations(self):
        self.client.get_status("lamp")
        self.client.is_on("mirror")
        self.client.get_device_info("mushroom")
        self.assertEqual(len(self.calls("/token")), 1)

    def test_expiry_and_early_renewal(self):
        self.client.get_status("lamp")
        self.monotonic.return_value = 7269.9
        self.client.get_status("mirror")
        self.assertEqual(len(self.calls("/token")), 1)
        self.monotonic.return_value = 7270.0
        self.tokens.append(response({**TOKEN, "access_token": "RENEWED_TEST_ONLY"}))
        self.client.get_status("mushroom")
        self.assertEqual(len(self.calls("/token")), 2)
        self.assertEqual(self.requests[-1].headers["access_token"], "RENEWED_TEST_ONLY")

    def test_short_token_lifetime(self):
        self.tokens.extend([response({**TOKEN, "expire_time": 10})] * 2)
        self.client.get_status("lamp")
        self.monotonic.return_value = 108.9
        self.client.get_status("lamp")
        self.assertEqual(len(self.calls("/token")), 1)
        self.monotonic.return_value = 109.0
        self.client.get_status("lamp")
        self.assertEqual(len(self.calls("/token")), 2)

    def test_invalid_tokens_and_lifetimes_are_not_cached(self):
        invalid = [None, [], {}, {"access_token": "x"}, {"expire_time": 7200}]
        invalid += [{**TOKEN, "access_token": token} for token in (None, "", "bad token", "bad\ntoken", 1)]
        invalid += [{**TOKEN, "expire_time": value} for value in (None, 0, -1, True, "7200", 1.5, 10 ** 400)]
        for result in invalid:
            with self.subTest(result=result):
                self.client.close()
                self.tokens = [response(result)]
                with self.assertRaises(tuya.TuyaResponseError):
                    self.client.get_status("lamp")
                self.assertIsNone(self.client._token)
        self.assertFalse(self.calls("/status"))

    def test_token_request_duration_counts_against_lifetime(self):
        self.monotonic.side_effect = [100, 8000]
        with self.assertRaises(tuya.TuyaResponseError):
            self.client.get_status("lamp")
        self.assertIsNone(self.client._token)
        self.assertFalse(self.calls("/status"))

    def test_each_token_error_renews_and_retries_once(self):
        path = "/v1.0/iot-03/devices/lampDevice123/status"
        for code in (1010, 1011, 1012, "1010", "1011", "1012"):
            with self.subTest(code=code):
                self.client.close()
                self.requests.clear()
                self.tokens = [response(TOKEN), response({**TOKEN, "access_token": "RENEWED_TEST_ONLY"})]
                self.routes[path] = [httpx.Response(200, json={"success": False, "code": code}), response(STATUS)]
                self.assertEqual(self.client.get_status("lamp"), {"switch_1": True, "cur_power": 42})
                self.assertEqual(len(self.calls("/token")), 2)
                self.assertEqual(len(self.calls("/status")), 2)
                self.assertEqual(self.requests[-1].headers["access_token"], "RENEWED_TEST_ONLY")
                self.assertNotEqual(self.requests[1].headers["sign"], self.requests[-1].headers["sign"])

    def test_failed_retry_never_retries_again(self):
        path = "/v1.0/iot-03/devices/lampDevice123/status"
        for code in (1010, 1011, 1012, 1004, 1106):
            with self.subTest(code=code):
                self.client.close()
                self.requests.clear()
                self.routes[path] = [httpx.Response(200, json={"success": False, "code": 1010}),
                                     httpx.Response(200, json={"success": False, "code": code})]
                with self.assertRaises(tuya.TuyaAPIError) as caught:
                    self.client.get_status("lamp")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.calls("/token")), 2)
                self.assertEqual(len(self.calls("/status")), 2)
                if code in (1010, 1011, 1012):
                    self.assertIsNone(self.client._token)

    def test_failed_token_acquisition_is_not_retried(self):
        self.tokens = [httpx.Response(200, json={"success": False, "code": 1012})]
        with self.assertRaises(tuya.TuyaAuthError):
            self.client.get_status("lamp")
        self.assertEqual(len(self.requests), 1)

    def test_failed_renewal_does_not_repeat_business_request(self):
        self.tokens = [response(TOKEN), httpx.Response(200, json={"success": False, "code": 1004})]
        self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = httpx.Response(
            200, json={"success": False, "code": 1010})
        with self.assertRaises(tuya.TuyaAuthError):
            self.client.get_status("lamp")
        self.assertEqual(len(self.calls("/token")), 2)
        self.assertEqual(len(self.calls("/status")), 1)
        self.assertIsNone(self.client._token)

    def test_concurrent_calls_share_initial_and_renewed_tokens(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.client.get_status, ["lamp"] * 8))
        self.assertEqual(len(self.calls("/token")), 1)
        self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = [
            httpx.Response(200, json={"success": False, "code": 1012}),
            *[response(STATUS) for _ in range(8)],
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.client.get_status, ["lamp"] * 8))
        self.assertEqual(len(self.calls("/token")), 2)

    def test_device_information_is_validated_and_filtered(self):
        info = self.client.get_device_info("lamp")
        self.assertEqual(info, tuya.DeviceInfo(IDS["lamp"], "Test plug", "cz", True))
        self.assertNotIn("DO_NOT_EXPOSE", repr(info))
        self.assertNotIn(IDS["lamp"], repr(info))
        for data in (None, [], {}, {"id": "wrong", "name": "x", "category": "cz", "online": True},
                     {"id": IDS["lamp"], "name": "x", "category": "cz", "online": 1}):
            self.routes["/v1.1/iot-03/devices/lampDevice123"] = response(data)
            with self.assertRaises(tuya.TuyaResponseError):
                self.client.get_device_info("lamp")

    def test_status_mapping_and_power_detection(self):
        self.assertEqual(self.client.get_status("lamp"), {"switch_1": True, "cur_power": 42})
        self.assertIs(self.client.is_on("lamp"), True)
        self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = response([{"code": "switch_1", "value": False}])
        self.assertIs(self.client.is_on("lamp"), False)

    def test_status_schema_and_duplicate_codes(self):
        for result in (None, {}, [None], [{}], [{"code": "switch_1"}], [{"code": [], "value": True}],
                       [{"code": "", "value": True}], STATUS + STATUS):
            self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = response(result)
            with self.subTest(result=result), self.assertRaises(tuya.TuyaResponseError):
                self.client.get_status("lamp")

    def test_power_requires_present_boolean_value(self):
        for value in (None, 0, 1, "true", "false", [], {}):
            self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = response([{"code": "switch_1", "value": value}])
            with self.subTest(value=value), self.assertRaises(tuya.TuyaResponseError):
                self.client.is_on("lamp")
        self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = response([])
        with self.assertRaises(tuya.TuyaResponseError):
            self.client.is_on("lamp")

    def test_switch_discovery_per_device_and_cache(self):
        self.routes["/v1.0/iot-03/devices/mirrorDevice123/functions"] = response({"functions": [
            {"code": "child_lock", "type": "Boolean"}, {"code": "switch", "type": "Boolean"}]})
        self.routes["/v1.0/iot-03/devices/mirrorDevice123/status"] = response([{"code": "switch", "value": False}])
        self.assertTrue(self.client.is_on("lamp"))
        self.assertFalse(self.client.is_on("mirror"))
        self.assertTrue(self.client.is_on("lamp"))
        self.assertEqual(len(self.calls("/functions")), 2)
        self.assertEqual(len(self.calls("/status")), 3)
        self.client.turn_on("mirror")
        self.assertEqual(json.loads(self.requests[-1].content), {"commands": [{"code": "switch", "value": True}]})

    def test_ambiguous_or_unsupported_switches_never_send_commands(self):
        for codes in ([], ["child_lock"], ["switch_2"], ["switch_led"], ["switch", "switch_1"],
                      ["switch_1", "switch_2"], ["switch_1", "switch_usb"]):
            self.routes["/v1.0/iot-03/devices/lampDevice123/functions"] = response({
                "functions": [{"code": code, "type": "Boolean"} for code in codes]})
            with self.subTest(codes=codes), self.assertRaises(tuya.TuyaCapabilityError):
                self.client.turn_on("lamp")
        self.assertFalse(self.calls("/commands"))
        self.assertEqual(self.client._switch_codes, {})

    def test_non_boolean_power_function_is_not_writable_power(self):
        self.routes["/v1.0/iot-03/devices/lampDevice123/functions"] = response({
            "functions": [{"code": "switch_1", "type": "Integer"}]})
        with self.assertRaises(tuya.TuyaCapabilityError):
            self.client.turn_on("lamp")
        self.assertFalse(self.calls("/commands"))

    def test_malformed_function_responses(self):
        for result in (None, [], {}, {"functions": {}}, {"functions": [None]},
                       {"functions": [{"code": "switch_1"}]},
                       {"functions": FUNCTIONS["functions"] * 2}):
            self.routes["/v1.0/iot-03/devices/lampDevice123/functions"] = response(result)
            with self.subTest(result=result), self.assertRaises(tuya.TuyaResponseError):
                self.client.turn_on("lamp")
        self.assertFalse(self.calls("/commands"))

    def test_discovery_api_error_does_not_fall_back_to_status(self):
        self.routes["/v1.0/iot-03/devices/lampDevice123/functions"] = httpx.Response(
            200, json={"success": False, "code": 1106})
        with self.assertRaises(tuya.TuyaAPIError):
            self.client.turn_on("lamp")
        self.assertFalse(self.calls("/status"))
        self.assertFalse(self.calls("/commands"))

    def test_turn_on_payload_and_signature(self):
        self.assertIsNone(self.client.turn_on("lamp"))
        request = self.requests[-1]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1.0/iot-03/devices/lampDevice123/commands")
        self.assertEqual(request.content, b'{"commands":[{"code":"switch_1","value":true}]}')
        self.assertEqual(request.headers["sign"], "A4DC081E6EEE2BE525A1A560724B1374C5C3B631240117BD4F60D23415448E43")

    def test_turn_off_and_explicit_set_power(self):
        self.assertIsNone(self.client.turn_off("lamp"))
        self.assertEqual(json.loads(self.requests[-1].content), {"commands": [{"code": "switch_1", "value": False}]})
        self.client.set_power(power=True, device_id="explicitDevice456")
        self.assertEqual(self.requests[-1].url.path, "/v1.0/iot-03/devices/explicitDevice456/commands")
        self.assertEqual(json.loads(self.requests[-1].content), {"commands": [{"code": "switch_1", "value": True}]})

    def test_power_rejects_non_booleans_before_io(self):
        for power in (None, 0, 1, "on", "off", "true", [], {}):
            with self.subTest(power=power), self.assertRaises(tuya.TuyaDeviceError):
                self.client.set_power("lamp", power)
        self.assertFalse(self.requests)
        self.reader.assert_not_called()

    def test_command_auth_retry_has_identical_payload(self):
        self.routes["/v1.0/iot-03/devices/lampDevice123/commands"] = [
            httpx.Response(200, json={"success": False, "code": 1012}), response(True)]
        self.client.turn_off("lamp")
        commands = self.calls("/commands")
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0].content, commands[1].content)
        self.assertEqual(len(self.calls("/token")), 2)

    def test_command_retry_failure_is_final(self):
        path = "/v1.0/iot-03/devices/lampDevice123/commands"
        for code in (1010, 1011, 1012):
            with self.subTest(code=code):
                self.client.close()
                self.requests.clear()
                self.routes[path] = [httpx.Response(200, json={"success": False, "code": code})
                                     for _ in range(2)]
                with self.assertRaises(tuya.TuyaAuthError) as caught:
                    self.client.turn_on("lamp")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.calls("/commands")), 2)
                self.assertEqual(len(self.calls("/token")), 2)
                self.assertIsNone(self.client._token)

    def test_command_renewal_failure_does_not_resend_command(self):
        self.tokens = [response(TOKEN), httpx.ReadTimeout("SECRET_TEST_ONLY")]
        self.routes["/v1.0/iot-03/devices/lampDevice123/commands"] = httpx.Response(
            200, json={"success": False, "code": 1012})
        with self.assertRaises(tuya.TuyaTransportError):
            self.client.turn_off("lamp")
        self.assertEqual(len(self.calls("/commands")), 1)
        self.assertEqual(len(self.calls("/token")), 2)
        self.assertIsNone(self.client._token)

    def test_retry_uses_fresh_timestamps_and_signature(self):
        self.wall.side_effect = [1700000000.0, 1700000001.0, 1700000002.0, 1700000003.0]
        self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = [
            httpx.Response(200, json={"success": False, "code": 1012}), response(STATUS)]
        self.client.get_status("lamp")
        self.assertEqual([req.headers["t"] for req in self.requests],
                         ["1700000000000", "1700000001000", "1700000002000", "1700000003000"])
        self.assertNotEqual(self.requests[1].headers["sign"], self.requests[3].headers["sign"])

    def test_command_failures_are_never_transport_retried(self):
        path = "/v1.0/iot-03/devices/lampDevice123/commands"
        for result in (httpx.ReadTimeout("PRIVATE"), httpx.ConnectError("PRIVATE"),
                       httpx.Response(500, text="PRIVATE"), httpx.Response(200, text="not-json"),
                       response(None), response(1), response("true")):
            with self.subTest(result=result):
                self.requests.clear()
                self.routes[path] = result
                with self.assertRaises(tuya.TuyaCommandUncertain):
                    self.client.turn_on("lamp")
                self.assertEqual(len(self.calls("/commands")), 1)
        self.routes[path] = response(False)
        with self.assertRaises(tuya.TuyaError) as caught:
            self.client.turn_off("lamp")
        self.assertNotIsInstance(caught.exception, tuya.TuyaCommandUncertain)

    def test_api_errors_and_non_refreshable_auth_errors(self):
        for code in (1004, 1005, 1106, 2015):
            with self.subTest(code=code):
                self.client.close()
                self.requests.clear()
                self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = httpx.Response(
                    200, json={"success": False, "code": code, "msg": "SECRET_TEST_ONLY"})
                with self.assertRaises(tuya.TuyaAPIError) as caught:
                    self.client.get_status("lamp")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.calls("/token")), 1)
                self.assertEqual(len(self.calls("/status")), 1)

    def test_http_errors_redirects_and_timeouts(self):
        path = "/v1.0/iot-03/devices/lampDevice123/status"
        for status in (301, 302, 401, 403, 404, 429, 500, 503):
            self.routes[path] = httpx.Response(status, text="SECRET_TEST_ONLY", headers={"Location": "https://other.invalid"})
            with self.subTest(status=status), self.assertRaises(tuya.TuyaTransportError) as caught:
                self.client.get_status("lamp")
            self.assertEqual(caught.exception.status_code, status)
        self.routes[path] = httpx.ReadTimeout("SECRET_TEST_ONLY")
        with self.assertRaises(tuya.TuyaTransportError) as caught:
            self.client.get_status("lamp")
        self.assertIsNone(caught.exception.status_code)
        self.assertEqual(len(self.calls("/token")), 1)
        self.assertEqual(len(self.calls("/status")), 9)

    def test_malformed_json_envelopes_and_error_codes(self):
        results = [httpx.Response(200, text="SECRET_TEST_ONLY")]
        results += [httpx.Response(200, json=data) for data in (
            None, [], {}, {"success": 1, "result": []}, {"success": True},
            {"success": False}, {"success": False, "code": "SECRET_TEST_ONLY"},
            {"success": False, "code": True}, {"success": False, "code": {}},
            {"success": False, "code": -1})]
        for result in results:
            self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = result
            with self.subTest(result=result), self.assertRaises(tuya.TuyaResponseError):
                self.client.get_status("lamp")

    def test_secrets_never_appear_in_errors_tracebacks_or_logs(self):
        message = " ".join(SENSITIVE)
        cases = [httpx.ReadTimeout(message), httpx.Response(500, text=message),
                 httpx.Response(200, text=message),
                 httpx.Response(200, json={"success": False, "code": 1106, "msg": message}),
                 httpx.Response(200, json={"success": False, "code": message})]
        with self.assertLogs("httpx", level="DEBUG") as logs:
            for result in cases:
                self.routes["/v1.0/iot-03/devices/lampDevice123/status"] = result
                with self.assertRaises(tuya.TuyaError) as caught:
                    self.client.get_status("lamp")
                rendered = repr(caught.exception) + "".join(traceback.format_exception(caught.exception))
                for value in SENSITIVE:
                    self.assertNotIn(value, rendered)
        logged = "\n".join(logs.output)
        for value in SENSITIVE + tuple(req.headers["sign"] for req in self.requests):
            self.assertNotIn(value, logged)

    def test_close_releases_pool_and_clears_token_and_switch_cache(self):
        self.client.is_on("lamp")
        pool = self.client._http
        self.client.close()
        self.assertTrue(pool.is_closed)
        self.assertIsNone(self.client._token)
        self.assertEqual(self.client._switch_codes, {})
        self.client.close()

    def test_cli_default_and_all_are_read_only(self):
        for argv in ([], ["status", "all"]):
            self.requests.clear()
            with patch.object(tuya, "TuyaClient", return_value=self.client), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(tuya.main(argv), 0)
            for alias in IDS:
                self.assertIn(f"{alias}: visible, online, power=on", output.getvalue())
            self.assertTrue(all(req.method == "GET" for req in self.requests))
            self.assertEqual(len(self.calls("/token")), 1)
            for value in SENSITIVE + tuple(IDS.values()):
                self.assertNotIn(value, output.getvalue())

    def test_cli_continues_after_missing_device_and_marks_offline_state(self):
        self.reader.return_value = CONFIG.replace("TUYA_MIRROR_PLUG_ID=mirrorDevice123", "")
        self.routes["/v1.1/iot-03/devices/mushroomDevice123"] = response({
            "id": IDS["mushroom"], "name": "Test", "category": "cz", "online": False})
        with patch.object(tuya, "TuyaClient", return_value=self.client), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tuya.main([]), 1)
        self.assertIn("mirror: error:", output.getvalue())
        self.assertIn("mushroom: visible, offline (last reported state), power=on", output.getvalue())
        self.assertTrue(all(req.method == "GET" for req in self.requests))

    def test_cli_rejects_mutating_commands_before_io(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                tuya.main(["on", "lamp"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.requests)


class ImportTests(unittest.TestCase):
    def test_import_and_construction_have_no_io(self):
        # Import into a separate module namespace so existing exception classes
        # and the test suite's module references are not replaced by reload().
        spec = importlib.util.spec_from_file_location("tuya_import_check", tuya.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {spec.name: module}), \
                patch.object(Path, "read_text", side_effect=AssertionError("configuration I/O")), \
                patch.object(httpx, "Client", side_effect=AssertionError("HTTP client construction")):
            spec.loader.exec_module(module)
            module.TuyaClient().close()


if __name__ == "__main__":
    unittest.main()
