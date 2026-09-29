"""Exercise client deployment failures without touching systemd or Pi hardware."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.restart_client import (
    CORE, DISPLAY, OPTIONAL, START_ORDER, STOP_ORDER, VOICE,
    ClientRestart, RestartError, restart_lock,
)


class FakeHost:
    def __init__(self, root):
        self.root = root
        self.native = root / 'client/native/cube-display'
        self.native.mkdir(parents=True)
        (self.native / 'renderer-control.py').write_text('# test helper\n')
        self.write_binary(self.native / 'cube-display', b'old')
        submodule = root / 'client/third_party/rpi-rgb-led-matrix'
        submodule.mkdir(parents=True)
        (submodule / 'Makefile').touch()
        self.calls = []
        self.fail = lambda args: False
        self.pid = 100
        self.slept = 0
        self.on_sleep = lambda: None
        app = root / 'client/app'
        self.states = {}
        for unit in CORE + OPTIONAL:
            executable = self.native / 'cube-display' if unit == DISPLAY else app / '.venv/bin/python'
            self.states[unit] = {
                'LoadState': 'loaded', 'UnitFileState': 'enabled',
                'ActiveState': 'active', 'SubState': 'waiting' if unit.endswith('.timer') else 'running',
                'MainPID': '0' if unit.endswith('.timer') else '10', 'NRestarts': '0',
                'WorkingDirectory': str(self.native if unit == DISPLAY else app),
                'ExecStart': f'{{ path={executable} ; argv[]={executable} {app / "cube.py"} ; }}',
                'User': 'root' if unit == DISPLAY else 'cubeuser',
                'Environment': 'CUBE_DISPLAY_USER=cubeuser' if unit == DISPLAY else '',
                'EnvironmentFiles': '', 'UnsetEnvironment': '', 'NeedDaemonReload': 'no',
            }

    @staticmethod
    def write_binary(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x7fELF' + body)
        path.chmod(0o755)

    def run(self, args, **kwargs):
        self.calls.append(args)
        if self.fail(args):
            return subprocess.CompletedProcess(args, 1, '', '')
        output = ''
        if args[:2] == ['systemctl', 'show']:
            output = '\n'.join(f'{k}={v}' for k, v in self.states[args[2]].items())
        elif args[0] == 'make':
            self.write_binary(self.native / 'build/cube-display', b'new')
        elif args[:2] == ['sudo', 'systemctl']:
            action, unit = args[2:]
            state = self.states[unit]
            if action == 'stop':
                state.update(ActiveState='inactive', SubState='dead', MainPID='0')
            else:
                # systemctl start does not restart an already active unit.
                if state['ActiveState'] != 'active':
                    self.pid += 1
                    state.update(ActiveState='active', SubState='waiting' if unit.endswith('.timer') else 'running',
                                 MainPID='0' if unit.endswith('.timer') else str(self.pid))
        elif args[:2] == ['sudo', 'install']:
            shutil.copyfile(args[-2], args[-1])
            Path(args[-1]).chmod(0o755)
        elif args[:2] == ['sudo', 'mv']:
            Path(args[-2]).replace(args[-1])
        elif args[:2] == ['sudo', 'rm']:
            Path(args[-1]).unlink(missing_ok=True)
        elif args[-2:] == ['control', 'get_status']:
            output = '{"display_enabled": true, "master_brightness_percent": 50}'
        else:
            assert args == ['sudo', '-v'], args
        return subprocess.CompletedProcess(args, 0, output, '')

    def sleep(self, seconds):
        self.slept += seconds
        self.on_sleep()

    def actions(self, action):
        return [args[-1] for args in self.calls if args[:3] == ['sudo', 'systemctl', action]]


class RestartClientTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.host = FakeHost(self.root)
        self.stack.enter_context(patch('scripts.restart_client.os.geteuid', return_value=1000))
        self.stack.enter_context(patch('scripts.restart_client.pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1000)))
        self.stack.enter_context(patch('scripts.restart_client.shutil.which', side_effect=lambda tool: '/usr/bin/' + tool))
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.errors = self.stack.enter_context(redirect_stderr(io.StringIO()))

    def client(self, **options):
        return ClientRestart(self.root, run=self.host.run, sleep=self.host.sleep, **options)

    def test_rebuild_backup_atomic_install_service_order_and_verification(self):
        client = self.client()
        self.assertEqual(client.execute(), 0)
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFnew')
        self.assertEqual(client.backup.read_bytes(), b'\x7fELFold')
        self.assertEqual(self.host.actions('stop'), list(STOP_ORDER))
        self.assertEqual(self.host.actions('start'), list(START_ORDER))
        build_index = next(i for i, args in enumerate(self.host.calls) if args[0] == 'make')
        stop_index = next(i for i, args in enumerate(self.host.calls) if args[:3] == ['sudo', 'systemctl', 'stop'])
        install_index = next(i for i, args in enumerate(self.host.calls) if args[:2] == ['sudo', 'install'])
        display_stop = self.host.calls.index(['sudo', 'systemctl', 'stop', DISPLAY])
        self.assertLess(build_index, stop_index)
        self.assertLess(display_stop, install_index)
        self.assertEqual(self.host.slept, 10)
        self.assertFalse(client.temporary.exists())
        self.assertIn('brightness: 50%', self.output.getvalue())

    def test_dry_run_has_only_read_commands_and_no_files_changed(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.client(dry_run=True).execute(), 0)
        self.assertTrue(all(args[:2] == ['systemctl', 'show'] for args in self.host.calls))
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.host.slept, 0)

    def test_restart_only_preserves_installed_binary_and_needs_no_build_tools(self):
        with patch('scripts.restart_client.shutil.which', side_effect=lambda tool: None if tool == 'make' else '/usr/bin/' + tool):
            client = self.client(restart_only=True)
            self.assertEqual(client.execute(), 0)
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')
        self.assertFalse(client.backup.exists())
        self.assertFalse(any(args[0] == 'make' or args[:2] == ['sudo', 'install'] for args in self.host.calls))

    def test_optional_selection_skips_disabled_missing_and_masked_but_keeps_active(self):
        for unit in OPTIONAL:
            self.host.states[unit].update(UnitFileState='disabled', ActiveState='inactive', MainPID='0')
        self.host.states[OPTIONAL[0]].update(ActiveState='active', MainPID='10')
        self.host.states[OPTIONAL[1]].update(LoadState='not-found')
        self.host.states[OPTIONAL[2]].update(UnitFileState='masked', ActiveState='active')
        client = self.client(dry_run=True)
        client.execute()
        self.assertEqual(set(client.selected), set(CORE) | {OPTIONAL[0]})
        self.assertNotIn('cube-spotify-age-check.service', client.selected)

    def test_preflight_failures_never_build_stop_or_use_sudo(self):
        cases = [
            (DISPLAY, 'Environment', '', 'CUBE_DISPLAY_USER'),
            (DISPLAY, 'Environment', 'CUBE_DISPLAY_USER=someone_else', 'CUBE_DISPLAY_USER'),
            (DISPLAY, 'UnsetEnvironment', 'CUBE_DISPLAY_USER', 'CUBE_DISPLAY_USER'),
            (DISPLAY, 'WorkingDirectory', '/another/checkout', 'another checkout'),
            (VOICE, 'ExecStart', '{ path=/another/python ; }', 'unexpected executable'),
            (VOICE, 'UnitFileState', 'masked', 'missing, masked'),
            (VOICE, 'NeedDaemonReload', 'yes', 'daemon-reload'),
        ]
        for unit, key, value, message in cases:
            with self.subTest(unit=unit, key=key), patch.dict(self.host.states[unit], {key: value}):
                self.host.calls.clear()
                with self.assertRaisesRegex(RestartError, message):
                    self.client().execute()
                self.assertTrue(all(args[:2] == ['systemctl', 'show'] for args in self.host.calls))

    def test_root_or_wrong_account_is_rejected(self):
        for uid in (0, 2000):
            with self.subTest(uid=uid), patch('scripts.restart_client.os.geteuid', return_value=uid):
                with self.assertRaises(RestartError):
                    self.client().execute()
        self.assertFalse(self.host.actions('stop'))

    def test_failed_build_leaves_services_and_installed_binary_untouched(self):
        self.host.fail = lambda args: args[0] == 'make'
        client = self.client()
        with self.assertRaises(RestartError):
            client.execute()
        self.assertFalse(self.host.actions('stop'))
        self.assertFalse(self.host.actions('start'))
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')

    def test_failed_stop_does_not_install_and_recovers_previously_running_units(self):
        self.host.states[OPTIONAL[-1]].update(ActiveState='inactive', MainPID='0')
        self.host.fail = lambda args: args == ['sudo', 'systemctl', 'stop', DISPLAY]
        client = self.client()
        with self.assertRaises(RestartError):
            client.execute()
        self.assertFalse(client.backup.exists())
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')
        self.assertNotIn(OPTIONAL[-1], self.host.actions('start'))
        self.assertIn(VOICE, self.host.actions('start'))

    def test_candidate_install_failure_restores_backup(self):
        client = self.client()
        self.host.fail = lambda args: args[:2] == ['sudo', 'install'] and args[-2] == str(client.candidate)
        with self.assertRaises(RestartError):
            client.execute()
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')
        self.assertIn('Restored the previous renderer', self.output.getvalue())
        self.assertEqual(self.host.states[VOICE]['ActiveState'], 'active')

    def test_display_start_failure_rolls_back_before_restarting_voice(self):
        client = self.client()
        self.host.fail = lambda args: (args == ['sudo', 'systemctl', 'start', DISPLAY]
                                      and client.installed.read_bytes() == b'\x7fELFnew')
        with self.assertRaises(RestartError):
            client.execute()
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')
        self.assertEqual(self.host.states[DISPLAY]['ActiveState'], 'active')
        self.assertEqual(self.host.actions('start').count(VOICE), 1)

    def test_non_display_failure_reports_error_without_rollback_or_restarting_healthy_units(self):
        failed = 'cube-spotify-status.service'
        self.host.fail = lambda args: args == ['sudo', 'systemctl', 'start', failed]
        client = self.client()
        self.assertEqual(client.execute(), 1)
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFnew')
        self.assertEqual(self.host.actions('start').count(DISPLAY), 1)
        self.assertEqual(self.host.actions('start').count(VOICE), 1)
        self.assertIn(f'journalctl -u {failed}', self.errors.getvalue())

    def test_restart_during_stability_check_is_failure_even_when_active_again(self):
        changed = False
        def restart_once():
            nonlocal changed
            if not changed:
                self.host.states[DISPLAY].update(MainPID='999', NRestarts='1')
                changed = True
        self.host.on_sleep = restart_once
        client = self.client()
        with self.assertRaises(RestartError):
            client.execute()
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')

    def test_socket_failure_rolls_back_renderer(self):
        self.host.fail = lambda args: args[-2:] == ['control', 'get_status']
        client = self.client()
        with self.assertRaises(RestartError):
            client.execute()
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')

    def test_install_rename_failure_restores_backup_and_removes_temporary_file(self):
        failed_once = False
        def fail_first_rename(args):
            nonlocal failed_once
            if args[:2] == ['sudo', 'mv'] and not failed_once:
                failed_once = True
                return True
            return False
        self.host.fail = fail_first_rename
        client = self.client()
        with self.assertRaises(RestartError):
            client.execute()
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')
        self.assertFalse(client.temporary.exists())

    def test_failed_rollback_does_not_start_display_or_voice_with_bad_candidate(self):
        client = self.client()
        self.host.fail = lambda args: (args == ['sudo', 'systemctl', 'start', DISPLAY]
                                      or (args[:2] == ['sudo', 'install'] and args[-2] == str(client.backup)))
        with self.assertRaises(RestartError):
            client.execute()
        self.assertIn('Renderer rollback failed', self.errors.getvalue())
        self.assertEqual(self.host.actions('start').count(DISPLAY), 1)
        self.assertNotIn(VOICE, self.host.actions('start'))

    def test_backup_directory_or_symlink_is_rejected_before_stopping(self):
        client = self.client()
        client.backup.mkdir()
        with self.assertRaisesRegex(RestartError, 'Unsafe backup'):
            client.execute()
        client.backup.rmdir()
        client.backup.symlink_to(client.installed)
        with self.assertRaisesRegex(RestartError, 'Unsafe backup'):
            client.execute()
        self.assertFalse(self.host.actions('stop'))

    def test_renderer_still_running_after_stop_prevents_replacement(self):
        client = self.client()
        original_run = self.host.run
        def run(args, **kwargs):
            result = original_run(args, **kwargs)
            if args == ['sudo', 'systemctl', 'stop', DISPLAY]:
                self.host.states[DISPLAY].update(ActiveState='deactivating', MainPID='123')
            return result
        client.run = run
        with self.assertRaisesRegex(RestartError, 'has not stopped'):
            client.execute()
        self.assertFalse(client.backup.exists())
        self.assertEqual(client.installed.read_bytes(), b'\x7fELFold')

    def test_refuses_overlapping_runs(self):
        with restart_lock(self.root / 'lock'):
            with self.assertRaisesRegex(RestartError, 'already running'):
                with restart_lock(self.root / 'lock'):
                    self.fail('Second restart acquired the lock')


if __name__ == '__main__':
    unittest.main()
