"""No microphone, models, speaker, renderer or real HTTP required."""
import io
from threading import Event
import unittest
from unittest.mock import Mock, patch

import requests

from notifications import Notification, Notifications


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.worker = Notifications("http://cube.invalid", "test-pi")
        self.payload = {"id": 1, "response": "Dune is ready to watch.", "ack_token": "test-receipt"}

    def test_queue_waits_for_playback_and_retries_ack_without_replay(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=lambda: self.payload)
        posted = Event()
        attempts = []

        def post(*args, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise requests.ConnectionError("private")
            posted.set()
            self.worker.close()
            return Mock(status_code=200)

        session.post.side_effect = post
        with patch("notifications.worker.requests.Session") as factory, patch.object(self.worker, "_headers", return_value={}), patch.object(self.worker.stopped, "wait", return_value=False):
            factory.return_value.__enter__.return_value = session
            self.worker.start()
            item = self.worker.queue.get(timeout=1)
            session.post.assert_not_called()
            self.assertEqual(session.get.call_count, 1)
            self.worker.complete(item, True)
            self.assertTrue(posted.wait(1))
            self.worker.thread.join(1)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["json"], {"ack_token": "test-receipt"})

    def test_failed_speech_is_not_acknowledged(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=lambda: self.payload)
        with patch("notifications.worker.requests.Session") as factory, patch.object(self.worker, "_headers", return_value={}):
            factory.return_value.__enter__.return_value = session
            self.worker.start()
            item = self.worker.queue.get(timeout=1)
            self.worker.close()
            self.worker.complete(item, False)
            self.worker.thread.join(1)
        session.post.assert_not_called()

    def test_validation_checks_identity_and_discards_obsolete_event(self):
        item = Notification(self.payload)
        session = Mock()
        session.post.return_value = Mock(status_code=200, json=lambda: {"valid": False})
        with patch("notifications.worker.requests.Session") as factory, patch.object(self.worker, "_headers", return_value={"X-Cube-Client-ID": "test-pi"}):
            factory.return_value.__enter__.return_value = session
            self.assertFalse(self.worker.validate(item))
        self.assertTrue(item.discarded)
        session.post.assert_called_once_with("http://cube.invalid/events/1/validate",
            json={"ack_token": "test-receipt"}, headers={"X-Cube-Client-ID": "test-pi"},
            timeout=(3, 5), allow_redirects=False)
        self.assertFalse(session.trust_env)

    def test_obsolete_ack_is_dropped_without_replaying(self):
        session = Mock()
        calls = []
        def get(*args, **kwargs):
            calls.append("get")
            if len(calls) > 1:
                self.worker.close()
                return Mock(status_code=204)
            return Mock(status_code=200, json=lambda: self.payload)
        session.get.side_effect = get
        session.post.return_value = Mock(status_code=404)
        with patch("notifications.worker.requests.Session") as factory, patch.object(self.worker, "_headers", return_value={}):
            factory.return_value.__enter__.return_value = session
            self.worker.start()
            item = self.worker.queue.get(timeout=1)
            self.worker.complete(item, True)
            self.worker.thread.join(1)
        self.assertFalse(self.worker.thread.is_alive())
        session.post.assert_called_once()
        self.assertEqual(len(calls), 2)

    def test_disconnect_backoff_is_bounded_and_logs_no_exception(self):
        delays = []

        def wait(delay):
            delays.append(delay)
            if len(delays) == 6:
                self.worker.close()
            return False

        output = io.StringIO()
        with patch.object(self.worker, "_headers", side_effect=OSError("PRIVATE_KEY_SENTINEL")), patch.object(self.worker.stopped, "wait", side_effect=wait), patch("sys.stdout", output):
            self.worker._run()
        self.assertEqual(delays, [5, 10, 20, 40, 60, 60])
        self.assertNotIn("PRIVATE_KEY_SENTINEL", output.getvalue())
