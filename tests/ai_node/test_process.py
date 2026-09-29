"""Offline /process boundaries: mocked HTTP only, with no LLM or CUDA inference."""
import asyncio
from copy import deepcopy
import json
import traceback
import unittest
from unittest.mock import Mock, patch

import httpx
from jsonschema import Draft202012Validator

from ai_node.app import create_app
from ai_node.config import LLMConfig
from ai_node.process import (
    DRAFT202012_DIALECT, InvalidModelResponse, ProcessError, Processor,
    argument_validator, decode_result, strict_json,
)
from ai_node.schemas import ProcessRequest


BASE = {"transcript": "Turn the lights on", "client_id": "cube"}
TOOL = {
    "name": "lights.set_power", "description": "Set power",
    "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                   "required": ["enabled"], "additionalProperties": False},
}
INTENT = {"type": "tool", "tool": "lights.set_power", "arguments": {"enabled": True}}
CONVERSATION = {"type": "conversation", "response": "Hello."}
CONFIG = LLMConfig("http://llm.invalid:4321/v1", "mock-model")


def envelope(content=None):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps(CONVERSATION) if content is None else content,
    }}]}


def respond_request(options=None):
    return {**BASE, "phase": "respond",
            "tool_result": {"tool": "lights.set_power", "success": False,
                            "error": "Device unavailable", "data": {}},
            "response_options": ["Failed."] if options is None else options}


class ConfigTests(unittest.TestCase):
    def test_timeout_default_and_positive_float_overrides(self):
        with self.assertNoLogs("ai_node.config", level="WARNING"):
            self.assertEqual(LLMConfig.from_env({}).timeout, 20.0)
            for value, expected in ((" 0.25 ", 0.25), ("1", 1.0), ("30.5", 30.5), ("1e2", 100.0)):
                with self.subTest(value=value):
                    config = LLMConfig.from_env({"CUBE_AI_LLM_TIMEOUT": value})
                    self.assertEqual(config.timeout, expected)
                    self.assertIsInstance(config.timeout, float)

    def test_blank_and_invalid_timeouts_warn_and_use_default(self):
        for value in ("", " \t", "0", "-0", "-1", "nan", "NaN", "inf", "Infinity",
                      "-Infinity", "1e999", "1e-999", "PRIVATE_INVALID_TIMEOUT"):
            with self.subTest(value=value), self.assertLogs("ai_node.config", level="WARNING") as logs:
                config = LLMConfig.from_env({"CUBE_AI_LLM_BASE_URL": CONFIG.base_url,
                                             "CUBE_AI_LLM_MODEL": CONFIG.model,
                                             "CUBE_AI_LLM_TIMEOUT": value})
                self.assertEqual(config.timeout, 20.0)
                self.assertTrue(config.configured)
            self.assertEqual([record.getMessage() for record in logs.records], [
                "Invalid CUBE_AI_LLM_TIMEOUT; using 20.0 seconds",
            ])

    def test_missing_blank_and_partial_configuration(self):
        for values in ({}, {"CUBE_AI_LLM_BASE_URL": " ", "CUBE_AI_LLM_MODEL": "\t"},
                       {"CUBE_AI_LLM_BASE_URL": CONFIG.base_url},
                       {"CUBE_AI_LLM_MODEL": "mock-model"},
                       {"OPENAI_BASE_URL": CONFIG.base_url, "OPENAI_MODEL": "mock-model"}):
            with self.subTest(values=values):
                self.assertFalse(LLMConfig.from_env(values).configured)

    def test_trimmed_independent_configuration(self):
        config = LLMConfig.from_env({"CUBE_AI_LLM_BASE_URL": " http://llm.invalid:4321/v1/// ",
                                     "CUBE_AI_LLM_MODEL": " mock-model "})
        self.assertEqual(config, CONFIG)
        self.assertTrue(config.configured)
        self.assertTrue(LLMConfig("https://llm.invalid/api/v1", "different-model").configured)

    def test_invalid_url_disables_processing(self):
        for url in ("", "llm.invalid/v1", "/v1", "ftp://llm.invalid", "http://",
                    "http://user:password@llm.invalid", "http://user@llm.invalid",
                    "http://llm.invalid?key=secret", "http://llm.invalid#fragment",
                    "http://llm.invalid?", "http://llm.invalid#", "http://llm.invalid:0",
                    "http://llm.invalid:65536", "http://llm.invalid:bad", "http://[broken",
                    "http://llm.invalid/a b", "http://llm.invalid/a\nb", "http://llm.invalid\\v1"):
            with self.subTest(url=url):
                self.assertFalse(LLMConfig(url, "mock-model").configured)

    def test_decoder_rejects_duplicates_constants_and_trailing_content(self):
        for raw in ('{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', 'NaN', 'Infinity',
                    '-Infinity', '{"n":1e999}', '{} {}', '```json\n{}\n```', 'result: {}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json(raw)
        self.assertEqual(strict_json(' \n{"text":" café "}\t'), {"text": " café "})


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tts = patch("ai_node.app.SpeechSynthesizer.load")
        tts.start()
        self.addCleanup(tts.stop)
        self.requests = []
        self.upstream = httpx.Response(200, json=envelope())

        async def handle(request):
            self.requests.append(request)
            if isinstance(self.upstream, Exception):
                raise self.upstream
            return self.upstream

        self.transport = httpx.MockTransport(handle)
        self.app = create_app(llm_config=CONFIG, llm_transport=self.transport,
                              model_factory=Mock(side_effect=RuntimeError("mock STT unavailable")))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://ai.invalid")
        self.addAsyncCleanup(self.client.aclose)

    async def post(self, payload=None):
        return await self.client.post("/process", json=BASE if payload is None else payload)

    async def assert_bad_model(self, body, payload=None):
        self.requests.clear()
        self.upstream = httpx.Response(200, content=body if isinstance(body, str) else json.dumps(body))
        response = await self.post(payload)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json(), {"detail": "AI processing returned an invalid response."})
        self.assertEqual(len(self.requests), 1)

    def sent_output_schema(self, phase):
        sent = json.loads(self.requests[-1].content)
        self.assertEqual(set(sent), {"model", "messages", "stream", "response_format"})
        self.assertEqual(sent["model"], CONFIG.model)
        self.assertFalse(sent["stream"])
        self.assertEqual(set(self.requests[-1].extensions["timeout"].values()), {20.0})
        response_format = sent["response_format"]
        self.assertEqual(set(response_format), {"type", "json_schema"})
        self.assertEqual(response_format["type"], "json_schema")
        definition = response_format["json_schema"]
        self.assertEqual(set(definition), {"name", "strict", "schema"})
        self.assertEqual(definition["name"], "cube_process_" + phase)
        self.assertIs(definition["strict"], True)
        Draft202012Validator.check_schema(definition["schema"])
        return definition["schema"]

    async def test_interpret_output_schema_without_tools_allows_only_conversation(self):
        self.assertEqual((await self.post()).status_code, 200)
        self.assertEqual(len(self.requests), 1)
        schema = self.sent_output_schema("interpret")
        self.assertEqual(set(schema["properties"]), {"type", "response"})
        self.assertEqual(schema["properties"]["response"]["maxLength"], 300)
        validator = Draft202012Validator(schema)
        for text in ("Hi", "x" * 300):
            self.assertTrue(validator.is_valid({"type": "conversation", "response": text}))
        for result in (INTENT, {"type": "conversation", "response": ""},
                       {"type": "conversation", "response": "x" * 301},
                       {"type": "conversation", "response": 7}, {"type": "conversation"},
                       {"response": "Hi"}, {**CONVERSATION, "extra": True},
                       [CONVERSATION, CONVERSATION]):
            with self.subTest(result=result):
                self.assertFalse(validator.is_valid(result))

    async def test_interpret_output_schema_names_exact_tools_and_keeps_arguments_generic(self):
        tools = [TOOL, {"name": "media.select", "description": "Select media",
                        "parameters": {"type": "object", "properties": {
                            "media_id": {"type": "integer", "minimum": 1}}, "required": ["media_id"]}}]
        self.assertEqual((await self.post({**BASE, "tools": tools})).status_code, 200)
        self.assertEqual(len(self.requests), 1)
        schema = self.sent_output_schema("interpret")
        conversation, tool = schema["oneOf"]
        self.assertEqual(tool, {
            "type": "object",
            "properties": {
                "type": {"const": "tool"},
                "tool": {"type": "string", "enum": [item["name"] for item in tools]},
                "arguments": {"type": "object"},
            },
            "required": ["type", "tool", "arguments"],
            "additionalProperties": False,
        })
        self.assertEqual(set(conversation["properties"]), {"type", "response"})
        self.assertEqual(conversation["properties"]["response"]["maxLength"], 300)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(CONVERSATION))
        self.assertTrue(validator.is_valid({"type": "conversation", "response": "x" * 300}))
        for name in (item["name"] for item in tools):
            # The generation schema deliberately accepts generic argument objects.
            self.assertTrue(validator.is_valid({**INTENT, "tool": name, "arguments": {"any": [1, "x"]}}))
        for result in ({**INTENT, "tool": "unknown"}, {**INTENT, "tool": TOOL["name"] + " "},
                       {**INTENT, "arguments": []}, {**INTENT, "response": "Done"},
                       {"type": "tool", "tool": TOOL["name"]}, {**INTENT, "extra": True},
                       {"type": "conversation", "response": ""},
                       {"type": "conversation", "response": "x" * 301}):
            with self.subTest(result=result):
                self.assertFalse(validator.is_valid(result))

    async def test_respond_output_schema_preserves_exact_response_options(self):
        options = ["Failed.", "  Échec.\n", "Failed."]
        self.upstream = httpx.Response(200, json=envelope(json.dumps({
            "type": "conversation", "response": options[1],
        })))
        self.assertEqual((await self.post(respond_request(options))).status_code, 200)
        self.assertEqual(len(self.requests), 1)
        schema = self.sent_output_schema("respond")
        self.assertEqual(set(schema["properties"]), {"type", "response"})
        self.assertEqual(schema["properties"]["response"]["enum"], options)
        validator = Draft202012Validator(schema)
        for option in options:
            self.assertTrue(validator.is_valid({"type": "conversation", "response": option}))
        for result in (INTENT, {"type": "conversation", "response": options[1].strip()},
                       {"type": "conversation", "response": "Other wording"},
                       {"type": "conversation", "response": options[0], "extra": True}):
            with self.subTest(result=result):
                self.assertFalse(validator.is_valid(result))

    async def test_generation_limit_preserves_hard_validation_and_approved_wording(self):
        result = {"type": "conversation", "response": "x" * 500}
        for payload in (BASE, respond_request([result["response"]])):
            phase = payload.get("phase", "interpret")
            with self.subTest(phase=phase):
                self.requests.clear()
                self.upstream = httpx.Response(200, json=envelope(json.dumps(result)))
                response = await self.post(payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), result)
                self.assertEqual(len(self.requests), 1)
                schema = self.sent_output_schema(phase)
                self.assertEqual(Draft202012Validator(schema).is_valid(result), phase == "respond")

    async def test_generic_generation_arguments_still_require_per_tool_validation(self):
        result = {**INTENT, "arguments": {"enabled": "true"}}
        with self.assertLogs("ai_node.app", level="WARNING") as logs:
            await self.assert_bad_model(envelope(json.dumps(result)), {**BASE, "tools": [TOOL]})
        schema = self.sent_output_schema("interpret")
        self.assertTrue(Draft202012Validator(schema).is_valid(result))
        self.assertEqual(logs.records[0].getMessage(),
                         "AI-node processing failed status=502 reason=invalid_tool_arguments")

    async def test_invalid_model_reason_codes_do_not_expose_sensitive_data(self):
        secret = "PRIVATE_MODEL_VALUE_9e32"
        transcript = "PRIVATE_TRANSCRIPT_42c1"
        prompt_data = "PRIVATE_PROMPT_DATA_81d3"
        tool = {**TOOL, "description": prompt_data}
        interpretation = {**BASE, "transcript": transcript, "tools": [tool],
                          "history": [{"role": "user", "content": prompt_data}]}
        responding = {**respond_request(), "transcript": transcript,
                      "history": interpretation["history"]}
        responding["tool_result"]["error"] = secret
        incomplete = envelope(secret)
        incomplete["choices"][0]["finish_reason"] = secret
        invalid_message = envelope(secret)
        invalid_message["choices"][0]["message"]["role"] = secret
        native_call = envelope(secret)
        native_call["choices"][0]["message"]["tool_calls"] = [{
            "type": "function", "function": {"name": secret, "arguments": secret},
        }]
        cases = [
            ("invalid_envelope_json", '{"private":"' + secret, interpretation),
            ("invalid_envelope", [secret], interpretation),
            ("invalid_choices", {"choices": secret}, interpretation),
            ("incomplete_response", incomplete, interpretation),
            ("invalid_assistant_message", invalid_message, interpretation),
            ("native_tool_call", native_call, interpretation),
            ("invalid_result_json", envelope(secret), interpretation),
            ("invalid_result_schema", envelope(json.dumps({
                "type": "conversation", "response": {"private": secret},
            })), interpretation),
            ("unapproved_response", envelope(json.dumps({
                "type": "conversation", "response": secret,
            })), responding),
            ("unadvertised_tool", envelope(json.dumps({**INTENT, "tool": secret})), interpretation),
            ("invalid_tool_arguments", envelope(json.dumps({
                **INTENT, "arguments": {"enabled": secret},
            })), interpretation),
        ]
        validators = {TOOL["name"]: argument_validator(TOOL["parameters"])}
        for reason, body, payload in cases:
            with self.subTest(reason=reason):
                body = body if isinstance(body, str) else json.dumps(body)
                request = ProcessRequest.model_validate(payload)
                with self.assertRaises(InvalidModelResponse) as decoded:
                    decode_result(body, request, validators)
                self.assertEqual(decoded.exception.reason, reason)
                self.assertEqual(str(decoded.exception), reason)

                self.upstream = httpx.Response(200, content=body)
                with self.assertRaises(ProcessError) as processed:
                    await Processor(CONFIG, transport=self.transport).process(request)
                error = processed.exception
                self.assertEqual(error.status_code, 502)
                self.assertEqual(error.reason, reason)
                self.assertEqual(str(error), "AI processing returned an invalid response.")

                self.requests.clear()
                with self.assertLogs("ai_node.app", level="WARNING") as logs:
                    response = await self.post(payload)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"detail": "AI processing returned an invalid response."})
                self.assertEqual(len(self.requests), 1)
                self.assertEqual([record.getMessage() for record in logs.records], [
                    f"AI-node processing failed status=502 reason={reason}",
                ])
                self.assertIsNone(logs.records[0].exc_info)
                exposed = (response.text + repr(vars(error)) + repr(vars(decoded.exception))
                           + "".join(traceback.format_exception(decoded.exception))
                           + "".join(traceback.format_exception(error)) + repr(logs.output)
                           + repr(logs.records[0].args))
                for sensitive in (secret, transcript, prompt_data):
                    self.assertNotIn(sensitive, exposed)

    async def test_unexpected_decoder_failure_keeps_safe_generic_reason(self):
        with patch("ai_node.process.decode_result", side_effect=RuntimeError("PRIVATE_DECODER_DETAIL")):
            with self.assertLogs("ai_node.app", level="WARNING") as logs:
                response = await self.post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "AI processing returned an invalid response."})
        self.assertEqual([record.getMessage() for record in logs.records], [
            "AI-node processing failed status=502 reason=invalid_response",
        ])
        self.assertIsNone(logs.records[0].exc_info)
        self.assertNotIn("PRIVATE_DECODER_DETAIL", response.text + repr(logs.output))

    async def test_interpret_conversation_and_full_input(self):
        payload = {**BASE, "tools": [deepcopy(TOOL)], "history": [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier reply"},
        ], "context": {"pending": {"pending_id": "opaque", "intent": "request", "state": "choose",
                                     "candidates": [{"media_id": 1, "title": "Dune", "year": "2021"}],
                                     "permitted_decisions": ["select", "reject"], "expires_in": 30.0},
                        "remembered_movie": {"media_id": 1, "title": "Dune", "year": "2021"},
                        "last_requested_movie": None}}
        response = await self.post(payload)
        self.assertEqual(response.json(), CONVERSATION)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), CONFIG.base_url + "/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(set(request.extensions["timeout"].values()), {20.0})
        sent = json.loads(request.content)
        self.assertEqual(set(sent), {"model", "messages", "stream", "response_format"})
        self.assertEqual(sent["model"], CONFIG.model)
        self.assertFalse(sent["stream"])
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertEqual(sent["messages"][1:-1], payload["history"])
        data = json.loads(sent["messages"][-1]["content"])
        self.assertEqual(data, {key: payload[key] for key in ("transcript", "context", "tools")})
        self.assertNotIn("client_id", data)

    async def test_interpret_intent_preserves_arguments_and_dotted_name(self):
        self.upstream = httpx.Response(200, json=envelope(json.dumps(INTENT)))
        response = await self.post({**BASE, "tools": [TOOL]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), INTENT)
        self.assertEqual(len(self.requests), 1)


    async def test_respond_has_no_tools_and_preserves_exact_selected_option(self):
        selected = "  Échec.\n"
        payload = respond_request(["Failed.", selected])
        # JSON escaping is decoded before equality; the actual string is preserved.
        self.upstream = httpx.Response(200, json=envelope(json.dumps({"type": "conversation", "response": selected})))
        response = await self.post(payload)
        self.assertEqual(response.json(), {"type": "conversation", "response": selected})
        sent = json.loads(self.requests[0].content)
        self.assertNotIn("tools", sent)
        data = json.loads(sent["messages"][-1]["content"])
        self.assertNotIn("tools", data)
        self.assertEqual(data["tool_result"], payload["tool_result"])
        self.assertEqual(data["response_options"], payload["response_options"])
        self.assertEqual(len(self.requests), 1)

    async def test_respond_rejects_intent_and_all_nonexact_variants(self):
        for result in (INTENT, {"type": "conversation", "response": "failed."},
                       {"type": "conversation", "response": "Failed"},
                       {"type": "conversation", "response": " Failed."},
                       {"type": "conversation", "response": "Failed.\n"}):
            with self.subTest(result=result):
                await self.assert_bad_model(envelope(json.dumps(result)), respond_request())
        await self.assert_bad_model(envelope(json.dumps({"type": "conversation", "response": "Failed."})),
                                    respond_request([" Failed. "]))

    async def test_invalid_completion_envelopes(self):
        invalid = [None, [], "text", {}, {"choices": []}, {"choices": {}},
                   {"choices": envelope()["choices"] * 2}, {"choices": [None]},
                   {"choices": [{"message": envelope()["choices"][0]["message"]}]}]
        for reason in (None, "length", "tool_calls", "function_call", "content_filter", "error", 1):
            item = envelope()
            item["choices"][0]["finish_reason"] = reason
            invalid.append(item)
        for message in (None, [], {}, {"role": "user", "content": "{}"},
                        {"role": "assistant"}, {"role": "assistant", "content": None},
                        {"role": "assistant", "content": []}, {"role": "assistant", "content": {}}):
            invalid.append({"choices": [{"finish_reason": "stop", "message": message}]})
        for field in ("function_call", "refusal"):
            for value in ([], {}, "", False, "payload"):
                item = envelope()
                item["choices"][0]["message"][field] = value
                invalid.append(item)
        for item in invalid:
            with self.subTest(envelope=item):
                await self.assert_bad_model(item)

    async def test_null_optional_envelope_fields_and_standard_metadata_are_allowed(self):
        body = envelope()
        body.update(id="mock", usage={"total_tokens": 12})
        body["choices"][0]["message"].update(tool_calls=None, function_call=None, refusal=None)
        self.upstream = httpx.Response(200, json=body)
        self.assertEqual((await self.post()).json(), CONVERSATION)

    async def test_missing_null_and_empty_tool_calls_are_allowed(self):
        for payload in (BASE, respond_request([CONVERSATION["response"]])):
            for fields in ({}, {"tool_calls": None}, {"tool_calls": []}):
                with self.subTest(phase=payload.get("phase", "interpret"), fields=fields):
                    self.requests.clear()
                    body = envelope()
                    body["choices"][0]["message"].update(fields)
                    self.upstream = httpx.Response(200, json=body)
                    response = await self.post(payload)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), CONVERSATION)
                    self.assertEqual(len(self.requests), 1)

    async def test_nonempty_and_malformed_tool_calls_are_rejected(self):
        native_call = {"id": "call_mock", "type": "function", "function": {
            "name": TOOL["name"], "arguments": json.dumps(INTENT["arguments"]),
        }}
        for payload in ({**BASE, "tools": [TOOL]}, respond_request([CONVERSATION["response"]])):
            for value in ([native_call], [{}], [None], {}, "", "[]", False, True, 0, 1):
                with self.subTest(phase=payload.get("phase", "interpret"), tool_calls=value):
                    body = envelope()
                    body["choices"][0]["message"]["tool_calls"] = value
                    await self.assert_bad_model(body, payload)

    async def test_strict_decoding_of_envelope_and_assistant_content(self):
        raw_content = ['{"type":"conversation","response":"a","response":"b"}',
                       '{"type":"tool","tool":"lights.set_power","arguments":{"enabled":true,"enabled":false}}',
                       'NaN', 'Infinity', '-Infinity', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
                       '{"x":1e999}', 'prefix ' + json.dumps(CONVERSATION),
                       '```json\n' + json.dumps(CONVERSATION) + '\n```',
                       json.dumps(CONVERSATION) + ' {}', '', 'null', '[]']
        for content in raw_content:
            with self.subTest(content=content):
                await self.assert_bad_model(envelope(content))
        for body in ('{"choices":[],"choices":[]}', '{"choices":NaN}', '{"choices":Infinity}',
                     '{"choices":-Infinity}', '{"choices":', json.dumps(envelope()) + '{}'):
            with self.subTest(body=body):
                await self.assert_bad_model(body)

    async def test_result_contract_and_tool_validation(self):
        invalid = [
            {"type": "conversation", "response": ""}, {"type": "conversation", "response": " \n"},
            {"type": "conversation", "response": "x" * 501}, {"type": "conversation", "response": 1},
            {**CONVERSATION, "success": True}, {"type": "unknown", "response": "x"},
            {"response": "x"}, {**INTENT, "success": True}, {**INTENT, "tool": "Lights.set_power"},
            {**INTENT, "tool": "lights.set_power "}, {**INTENT, "tool": "other"},
            {**INTENT, "arguments": {}}, {**INTENT, "arguments": {"enabled": "true"}},
            {**INTENT, "arguments": {"enabled": 1}}, {**INTENT, "arguments": {"enabled": True, "extra": 1}},
            {**INTENT, "arguments": []},
        ]
        for result in invalid:
            with self.subTest(result=result):
                await self.assert_bad_model(envelope(json.dumps(result)), {**BASE, "tools": [TOOL]})
        await self.assert_bad_model(envelope(json.dumps(INTENT)))

    async def test_local_refs_and_defaults_without_mutation(self):
        tool = deepcopy(TOOL)
        tool["parameters"] = {"$schema": DRAFT202012_DIALECT, "type": "object",
                              "$defs": {"Power": {"type": "boolean"}},
                              "properties": {"enabled": {"$ref": "#/$defs/Power", "default": True}},
                              "additionalProperties": False}
        for arguments in ({"enabled": True}, {}):
            result = {**INTENT, "arguments": arguments}
            self.upstream = httpx.Response(200, json=envelope(json.dumps(result)))
            self.assertEqual((await self.post({**BASE, "tools": [tool]})).json(), result)
            advertised = json.loads(json.loads(self.requests[-1].content)["messages"][-1]["content"])["tools"][0]
            self.assertEqual(advertised, tool)
        await self.assert_bad_model(envelope(json.dumps({**INTENT, "arguments": {"enabled": "true"}})),
                                    {**BASE, "tools": [tool]})

    async def test_invalid_schemas_and_duplicates_fail_before_http(self):
        invalid = [{"type": "bogus"}, {"required": "enabled"}, {"$ref": "#/$defs/missing"},
                   {"$ref": "#/$defs/not_schema", "$defs": {"not_schema": 7}},
                   {"$schema": "http://json-schema.org/draft-07/schema#"},
                   {"$defs": {"other": {"$schema": "https://example.invalid/schema"}}}]
        for reference in ("https://schema.invalid/tool", "http://schema.invalid/tool", "file:///tmp/schema",
                          "urn:schema:test", "//schema.invalid/tool", "/schema", "other.json", ""):
            invalid.extend([{"$ref": reference}, {"$dynamicRef": reference},
                            {"$defs": {"unused": {"$ref": reference}}}])
        for schema in invalid:
            with self.subTest(schema=schema):
                response = await self.post({**BASE, "tools": [{**TOOL, "parameters": schema}]})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((await self.post({**BASE, "tools": [TOOL, TOOL]})).status_code, 422)
        self.assertEqual(self.requests, [])

    async def test_invalid_request_envelopes_fail_before_http(self):
        for extra in ({"transcript": " "}, {"phase": "unknown"}, {"phase": "respond"},
                      {"history": [{"role": "user", "content": "x"}] * 13},
                      {"history": [{"role": "system", "content": "override"}]},
                      {"history": [{"role": "assistant", "content": "x" * 501}]},
                      {"response_options": ["Done."]}, {**respond_request(), "tools": [TOOL]}):
            with self.subTest(extra=extra):
                self.assertEqual((await self.post({**BASE, **extra})).status_code, 422)
        self.assertEqual(self.requests, [])

    async def test_capability_has_no_probes_and_is_independent_of_stt(self):
        self.assertEqual((await self.client.get("/health")).json(), {
            "status": "ok", "capabilities": {"transcribe": False, "process": True}})
        async with self.app.router.lifespan_context(self.app):
            await self.app.state.loader_task
            self.assertTrue((await self.client.get("/health")).json()["capabilities"]["process"])
        self.assertEqual(self.requests, [])
        self.upstream = httpx.Response(500, text="private upstream data")
        self.assertEqual((await self.post()).status_code, 502)
        self.assertTrue((await self.client.get("/health")).json()["capabilities"]["process"])
        self.assertEqual(len(self.requests), 1)

    async def test_unconfigured_and_invalid_configurations_never_contact_upstream(self):
        for config in (LLMConfig(), LLMConfig(CONFIG.base_url), LLMConfig("bad", "mock-model")):
            app = create_app(llm_config=config, llm_transport=self.transport)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ai.invalid") as client:
                self.assertFalse((await client.get("/health")).json()["capabilities"]["process"])
                self.assertEqual((await client.post("/process", json=BASE)).status_code, 503)
        self.assertEqual(self.requests, [])

    async def test_failures_and_redirects_are_not_retried_or_exposed(self):
        for failure, expected in [(httpx.Response(code, text="PRIVATE", headers={"location": "http://other.invalid"}), 502)
                                  for code in (301, 302, 401, 429, 500, 503)]:
            self.requests.clear()
            self.upstream = failure
            response = await self.post()
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("PRIVATE", response.text)
            self.assertEqual(len(self.requests), 1)
        for error, expected in ((httpx.ConnectError("PRIVATE"), 502), (httpx.ReadTimeout("PRIVATE"), 504)):
            self.requests.clear()
            self.upstream = error
            response = await self.post()
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("PRIVATE", response.text)
            self.assertEqual(len(self.requests), 1)

    async def test_client_disables_environment_proxies(self):
        real_client = httpx.AsyncClient
        with patch("ai_node.process.httpx.AsyncClient", wraps=real_client) as factory:
            self.assertEqual((await self.post()).status_code, 200)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])

    async def test_deadline_and_cancellation_close_request_without_retry(self):
        for cancel in (False, True):
            entered, unwound, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
            calls = []

            class WaitingTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    calls.append(request)
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        unwound.set()

                async def aclose(self):
                    closed.set()

            config = LLMConfig(CONFIG.base_url, CONFIG.model, timeout=5 if cancel else 0.02)
            app = create_app(llm_config=config, llm_transport=WaitingTransport())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ai.invalid") as client:
                task = asyncio.create_task(client.post("/process", json=BASE))
                await asyncio.wait_for(entered.wait(), 1)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertEqual((await asyncio.wait_for(task, 1)).status_code, 504)
            self.assertTrue(unwound.is_set())
            self.assertTrue(closed.is_set())
            self.assertEqual(len(calls), 1)

    async def test_configured_timeout_is_used_by_asyncio_and_httpx(self):
        for override, expected in (({}, 20.0), ({"CUBE_AI_LLM_TIMEOUT": " 7.25 "}, 7.25)):
            with self.subTest(override=override):
                self.requests.clear()
                config = LLMConfig.from_env({"CUBE_AI_LLM_BASE_URL": CONFIG.base_url,
                                             "CUBE_AI_LLM_MODEL": CONFIG.model, **override})
                app = create_app(llm_config=config, llm_transport=self.transport)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ai.invalid") as client:
                    with patch("ai_node.process.asyncio.timeout", wraps=asyncio.timeout) as deadline:
                        self.assertEqual((await client.post("/process", json=BASE)).status_code, 200)
                deadline.assert_called_once_with(expected)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(set(self.requests[0].extensions["timeout"].values()), {expected})
