from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import compare_transcription as compare


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "recording.wav"
        self.audio = b"recording\x00\x01same bytes"
        self.path.write_bytes(self.audio)
        self.args = [str(self.path), "--backend-url", "http://backend.invalid/",
                     "--ai-node-url", "http://ai.invalid"]
        self.requests = []

    def run_comparison(self, handler, extra=(), times=(10, 12, 20, 23)):
        output, errors = StringIO(), StringIO()
        def handle(request):
            self.requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/transcribe")
            self.assertIn(b'name="file"; filename="recording.wav"', request.content)
            self.assertIn(b"\r\n\r\n" + self.audio + b"\r\n", request.content)
            return handler(request)
        with redirect_stdout(output), redirect_stderr(errors), patch(
            "scripts.compare_transcription.perf_counter", side_effect=times,
        ) as timer:
            code = compare.main(self.args + list(extra), transport=httpx.MockTransport(handle))
        return code, output.getvalue(), errors.getvalue(), timer.call_count

    @staticmethod
    def response(text="same", language="en", processing_time=0.125):
        return httpx.Response(200, json={"text": text, "language": language, "processing_time": processing_time})

    def test_default_two_identical_uploads_reporting_and_timeout(self):
        code, output, errors, clocks = self.run_comparison(lambda r: self.response())
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual([r.url.host for r in self.requests], ["backend.invalid", "ai.invalid"])
        for request in self.requests:
            self.assertTrue(all(t == 60 for t in request.extensions["timeout"].values()))
        self.assertIn("AI-node warm-up requested: no", output)
        self.assertNotIn("cold", output.lower())
        self.assertEqual(output.count('transcript: "same"'), 2)
        self.assertEqual(output.count("language: en"), 2)
        self.assertEqual(output.count("processing time: 0.125s"), 2)
        self.assertIn("Backend wall-clock latency: 2.000s", output)
        self.assertIn("AI node wall-clock latency: 3.000s", output)
        self.assertIn("Exact transcript equality: true", output)
        self.assertEqual(clocks, 4)

    def test_exact_equality_does_not_normalize_text(self):
        code, output, _, _ = self.run_comparison(
            lambda r: self.response("same" if r.url.host == "backend.invalid" else "Same "))
        self.assertEqual(code, 0)
        self.assertIn('transcript: "Same "', output)
        self.assertIn("Exact transcript equality: false", output)

    def test_warmup_precedes_both_measurements_and_is_discarded(self):
        def handler(request):
            if len(self.requests) == 1:
                self.assertEqual(timer.call_count, 0)
                return self.response("DISPOSABLE", "xx", 9876.5)
            return self.response()
        # A separate timer makes the pre-measurement assertion visible to the handler.
        with patch("scripts.compare_transcription.perf_counter", side_effect=[1, 3, 10, 13]) as timer:
            output = StringIO()
            def record(request):
                self.requests.append(request)
                self.assertEqual(request.url.path, "/transcribe")
                self.assertIn(self.audio, request.content)
                return handler(request)
            with redirect_stdout(output):
                code = compare.main(self.args + ["--warmup"], transport=httpx.MockTransport(record))
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual([r.url.host for r in self.requests], ["ai.invalid", "backend.invalid", "ai.invalid"])
        self.assertIn("AI-node warm-up requested: yes", text)
        self.assertEqual(text.count("AI-node warm-up completed."), 1)
        for discarded in ("DISPOSABLE", "xx", "9876", "total"):
            self.assertNotIn(discarded, text)
        self.assertIn("Backend wall-clock latency: 2.000s", text)
        self.assertIn("AI node wall-clock latency: 3.000s", text)
        self.assertEqual(timer.call_count, 4)

    def test_warmup_failure_aborts_without_any_measurements(self):
        for failure in (httpx.Response(503), httpx.Response(200, json={"text": "invalid"})):
            with self.subTest(failure=failure):
                self.requests.clear()
                code, output, errors, clocks = self.run_comparison(lambda r: failure, ["--warmup"], times=())
                self.assertEqual(code, 1)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.requests[0].url.host, "ai.invalid")
                self.assertIn("warm-up failed", errors)
                self.assertNotIn("latency", output)
                self.assertNotIn("processing time", output)
                self.assertNotIn("equality", output)
                self.assertEqual(clocks, 0)

    def test_each_endpoint_failure_is_independent_no_retry(self):
        for failing_host in ("backend.invalid", "ai.invalid"):
            for failure in ("timeout", "status", "schema"):
                with self.subTest(host=failing_host, failure=failure):
                    self.requests.clear()
                    def handler(request):
                        self.assertTrue(all(t == 2.5 for t in request.extensions["timeout"].values()))
                        if request.url.host != failing_host:
                            return self.response("survivor")
                        if failure == "timeout":
                            raise httpx.ReadTimeout("private timeout", request=request)
                        if failure == "status":
                            return httpx.Response(500, text="private error")
                        return self.response(processing_time=-1)
                    code, output, errors, _ = self.run_comparison(handler, ["--timeout", "2.5"])
                    self.assertEqual(code, 1)
                    self.assertEqual(len(self.requests), 2)
                    self.assertIn('transcript: "survivor"', output)
                    self.assertIn("failed (", output)
                    self.assertIn("Exact transcript equality: unavailable", output)
                    self.assertNotIn("private", output + errors)

    def test_warmup_timeout_has_no_retry(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)
        code, output, errors, clocks = self.run_comparison(handler, ["--warmup"], times=())
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(clocks, 0)
        self.assertIn("warm-up failed", errors)

    def test_both_failures_reported(self):
        code, output, _, _ = self.run_comparison(lambda r: httpx.Response(503))
        self.assertEqual(code, 1)
        self.assertEqual(output.count("failed (HTTPStatusError)"), 2)

    def test_required_arguments_positive_timeout_and_missing_file(self):
        for args in ([], [str(self.path)], self.args[:-2],
                     *[self.args + ["--timeout", value] for value in ("0", "-1", "nan", "inf", "oops")]):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                compare.main(args)
            self.assertEqual(error.exception.code, 2)
        self.path.unlink()
        code, _, errors, clocks = self.run_comparison(lambda r: self.fail("Unexpected HTTP"), times=())
        self.assertEqual(code, 1)
        self.assertIn("Cannot read recording", errors)
        self.assertEqual(clocks, 0)
