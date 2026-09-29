"""Render portable deployment files without installing or running services."""
from pathlib import Path
import tempfile
import unittest
from scripts.render_deployment import render


class DeploymentTemplatesTests(unittest.TestCase):
    def test_client_account_paths_and_socket_authority_agree(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'units'
            count = render('client', 'cube', '/home/cube', '/srv/cube', 1200, output)
            self.assertGreater(count, 5)
            voice = (output / 'cube-voice.service').read_text()
            display = (output / 'cube-display.service').read_text()
            self.assertIn('User=cube', voice)
            self.assertIn('user@1200.service', voice)
            self.assertIn('/run/user/1200', voice)
            self.assertIn('/srv/cube/client/app/.venv/bin/python', voice)
            self.assertIn('Environment=CUBE_DISPLAY_USER=cube', display)
            self.assertIn('subject.user === "cube"', (output / '60-cube-power.rules').read_text())
            for path in output.rglob('*'):
                if path.is_file():
                    self.assertNotIn('@CUBE_', path.read_text())
            with self.assertRaises(FileExistsError):
                render('client', 'cube', '/home/cube', '/srv/cube', 1200, output)

    def test_rejects_unsafe_values_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'units'
            for override in ({'uid': 0}, {'user': 'root'}, {'component': '../client'},
                             {'repo': '/tmp/a b'}, {'repo': '/tmp/../srv'}, {'home': 'relative'}):
                args = dict(component='client', user='cube', home='/home/cube', repo='/srv/cube', uid=1200, output=output)
                args.update(override)
                with self.subTest(override=override), self.assertRaises(ValueError):
                    render(**args)
                self.assertFalse(output.exists())
