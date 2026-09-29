#!/usr/bin/env python3
"""Rebuild and reload an installed Cube Pi client without resetting its data."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DISPLAY = 'cube-display.service'
VOICE = 'cube-voice.service'
CORE = ('cube-audio.service', 'cube-audio-control.service', DISPLAY, VOICE)
OPTIONAL = ('cube-spotify-bridge.service', 'cube-spotify.service',
            'cube-spotify-status.service', 'cube-spotify-age-check.timer')
START_ORDER = CORE[:3] + OPTIONAL + (VOICE,)
STOP_ORDER = (VOICE, 'cube-spotify-status.service', 'cube-spotify-age-check.timer',
              'cube-spotify.service', 'cube-spotify-bridge.service',
              'cube-audio-control.service', 'cube-audio.service', DISPLAY)
PROPERTIES = ('LoadState', 'UnitFileState', 'ActiveState', 'SubState', 'MainPID',
              'NRestarts', 'WorkingDirectory', 'ExecStart', 'User', 'Environment',
              'EnvironmentFiles', 'UnsetEnvironment', 'NeedDaemonReload')


class RestartError(Exception):
    pass


@contextmanager
def restart_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.client-restart.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RestartError('Another client restart is already running.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class ClientRestart:
    def __init__(self, root=ROOT, *, restart_only=False, dry_run=False,
                 run=subprocess.run, sleep=time.sleep):
        self.root = Path(root).resolve()
        self.native = self.root / 'client/native/cube-display'
        self.installed = self.native / 'cube-display'
        self.candidate = self.native / 'build/cube-display'
        self.backup = self.native / 'cube-display.backup'
        self.temporary = self.native / f'cube-display.{os.getpid()}.tmp'
        self.restart_only = restart_only
        self.dry_run = dry_run
        self.run = run
        self.sleep = sleep
        self.selected = []
        self.previously_active = set()
        self.backup_ready = False
        self.replacement_attempted = False

    def command(self, args, *, read=False, timeout=60):
        args = [str(arg) for arg in args]
        if not read:
            print('+ ' + shlex.join(args), flush=True)
        try:
            result = self.run(args, check=False, text=True, capture_output=read,
                              timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RestartError(f'{args[0]} failed: {error}') from error
        if result.returncode:
            # Do not echo systemctl Environment properties or private config.
            raise RestartError(f'Command failed ({result.returncode}): {shlex.join(args)}')
        return result.stdout if read else ''

    def show(self, unit):
        output = self.command(['systemctl', 'show', unit,
                               '--property=' + ','.join(PROPERTIES)], read=True)
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def systemctl(self, action, unit):
        self.command(['sudo', 'systemctl', action, unit])

    def check_path(self, state, directory, executable, unit):
        if Path(state.get('WorkingDirectory', '')).resolve() != directory.resolve():
            raise RestartError(f'{unit} uses another checkout. Run this script from the installed checkout.')
        match = re.search(r'\bpath=([^ ;]+)', state.get('ExecStart', ''))
        if not match or Path(match[1]).absolute() != executable.absolute():
            raise RestartError(f'{unit} has an unexpected executable; inspect its installed ExecStart.')

    @staticmethod
    def check_binary(path):
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise RestartError(f'Expected a regular executable: {path}')
        with path.open('rb') as source:
            if source.read(4) != b'\x7fELF':
                raise RestartError(f'Not an ELF renderer binary: {path}')

    def preflight(self):
        if os.geteuid() == 0:
            raise RestartError('Run as the Pi voice user, without sudo; privileged steps request sudo themselves.')
        tools = ['systemctl', 'sudo', 'install', 'mv', 'rm']
        if not self.restart_only:
            tools += ['make', 'g++', 'git', 'patch', 'python3']
        for tool in tools:
            if shutil.which(tool) is None:
                raise RestartError(f'Missing required tool: {tool}')
        states = {unit: self.show(unit) for unit in CORE + OPTIONAL}
        for unit in CORE:
            state = states[unit]
            if state.get('LoadState') != 'loaded' or state.get('UnitFileState', '').startswith('masked'):
                raise RestartError(f'Required unit is missing, masked or invalid: {unit}')
        self.selected = [unit for unit in START_ORDER if unit in CORE or (
            states[unit].get('LoadState') == 'loaded'
            and not states[unit].get('UnitFileState', '').startswith('masked')
            and (states[unit].get('ActiveState') in ('active', 'activating')
                 or states[unit].get('UnitFileState') in ('enabled', 'enabled-runtime')))]
        self.previously_active = {unit for unit in self.selected
                                  if states[unit].get('ActiveState') in ('active', 'activating')}
        for unit in self.selected:
            if states[unit].get('NeedDaemonReload') == 'yes':
                raise RestartError('Installed unit files changed. Run sudo systemctl daemon-reload, then retry.')
        voice = states[VOICE]
        user = voice.get('User', '')
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            raise RestartError('The voice service must name an existing non-root Pi account.') from None
        if uid == 0 or uid != os.geteuid():
            raise RestartError(f'Run this script as the installed voice account: {user}')
        display = states[DISPLAY]
        environment = dict(item.split('=', 1) for item in shlex.split(display.get('Environment', ''))
                           if '=' in item)
        if (environment.get('CUBE_DISPLAY_USER') != user
                or display.get('EnvironmentFiles')
                or any(item.split('=', 1)[0] == 'CUBE_DISPLAY_USER'
                       for item in shlex.split(display.get('UnsetEnvironment', '')))):
            raise RestartError(f'Set Environment=CUBE_DISPLAY_USER={user} directly in the display unit or a drop-in; '
                               'remove conflicting EnvironmentFile/UnsetEnvironment overrides and reload systemd.')
        self.check_path(display, self.native, self.installed, DISPLAY)
        app = self.root / 'client/app'
        self.check_path(voice, app, app / '.venv/bin/python', VOICE)
        if str(app / 'cube.py') not in voice.get('ExecStart', ''):
            raise RestartError('The installed voice service does not launch this checkout\'s cube.py.')
        for unit in ('cube-audio-control.service', 'cube-spotify-bridge.service', 'cube-spotify-status.service'):
            if unit in self.selected:
                self.check_path(states[unit], app, app / '.venv/bin/python', unit)
        self.check_binary(self.installed)
        if not (self.native / 'renderer-control.py').is_file():
            raise RestartError('Missing renderer-control.py in the installed checkout.')
        if not self.restart_only:
            if not (self.root / 'client/third_party/rpi-rgb-led-matrix/Makefile').is_file():
                raise RestartError('Initialize the pinned matrix submodule before rebuilding.')
            if (self.backup.is_symlink() or (self.backup.exists() and not self.backup.is_file())
                    or self.temporary.exists() or self.temporary.is_symlink()):
                raise RestartError('Unsafe backup or existing temporary renderer path; inspect before retrying.')

    def print_plan(self):
        print(f'Checkout: {self.root}')
        if not self.restart_only:
            print(f'Build: make -C {self.native} -j2 (as the current user)')
            print(f'Install: {self.candidate} -> {self.installed}; previous binary -> {self.backup}')
        print('Stop: ' + ', '.join(unit for unit in STOP_ORDER if unit in self.selected))
        print('Start: ' + ', '.join(self.selected))
        print('Check service stability for 10 seconds, then query the renderer socket.')
        print('Settings, credentials, installed units and animation files are preserved.')

    def assert_display_stopped(self):
        state = self.show(DISPLAY)
        if state.get('ActiveState') not in ('inactive', 'failed') or state.get('MainPID', '0') != '0':
            raise RestartError('Renderer has not stopped; refusing to replace its executable.')

    def atomic_install(self, source):
        self.command(['sudo', 'install', '-T', '-o', 'root', '-g', 'root', '-m', '0755',
                      '--', source, self.temporary])
        self.command(['sudo', 'mv', '-fT', '--', self.temporary, self.installed])

    def install_candidate(self):
        self.assert_display_stopped()
        self.command(['sudo', 'install', '-T', '-o', 'root', '-g', 'root', '-m', '0755',
                      '--', self.installed, self.backup])
        self.backup_ready = True
        self.replacement_attempted = True
        self.atomic_install(self.candidate)

    def verify(self):
        initial = {unit: self.show(unit) for unit in self.selected}
        problems = set()
        for tick in range(11):
            if tick:
                self.sleep(1)
            for unit in self.selected:
                state = self.show(unit) if tick else initial[unit]
                good = state.get('ActiveState') == 'active'
                if unit.endswith('.service'):
                    good = good and state.get('SubState') == 'running' and state.get('MainPID', '0') != '0'
                    good = good and state.get('MainPID') == initial[unit].get('MainPID')
                    good = good and state.get('NRestarts', '0') == initial[unit].get('NRestarts', '0')
                if not good:
                    problems.add(unit)
                if tick == 10:
                    print(f'{unit}: {state.get("ActiveState", "unknown")}/{state.get("SubState", "unknown")} '
                          f'(restarts: {state.get("NRestarts", "0")})')
        if DISPLAY not in problems:
            try:
                output = self.command([sys.executable, '-B', self.native / 'renderer-control.py',
                                       'control', 'get_status'], read=True)
                reply = json.loads(output)
                if (type(reply) is not dict or type(reply.get('display_enabled')) is not bool
                        or type(reply.get('master_brightness_percent')) is not int
                        or not 0 <= reply['master_brightness_percent'] <= 100):
                    raise ValueError('Invalid renderer response')
                print(f'Display enabled: {reply["display_enabled"]}; brightness: {reply["master_brightness_percent"]}%')
            except (RestartError, ValueError):
                problems.add(DISPLAY)
        return problems

    def recover(self):
        print('Attempting recovery of previously running services.', flush=True)
        renderer_safe = True
        if self.replacement_attempted and self.backup_ready:
            try:
                self.systemctl('stop', DISPLAY)
                self.assert_display_stopped()
                self.atomic_install(self.backup)
                print('Restored the previous renderer binary.')
            except RestartError as error:
                renderer_safe = False
                print(f'Renderer rollback failed: {error}', file=sys.stderr)
        for unit in self.selected:
            if unit not in self.previously_active:
                continue
            if not renderer_safe and unit in (DISPLAY, VOICE):
                continue  # Voice Wants=display would otherwise start the failed candidate.
            try:
                self.systemctl('start', unit)
            except RestartError as error:
                print(f'Recovery failed for {unit}: {error}', file=sys.stderr)

    @staticmethod
    def diagnostics(units):
        for unit in sorted(units):
            print(f'Inspect: journalctl -u {unit} -n 50 --no-pager', file=sys.stderr)

    def execute(self):
        self.preflight()
        self.print_plan()
        if self.dry_run:
            print('Dry run: no build, sudo, file changes or service operations performed.')
            return 0
        if not self.restart_only:
            self.command(['make', '-C', self.native, '-j2'], timeout=None)
            self.check_binary(self.candidate)
        # Authenticate after the build so a long compile cannot expire sudo
        # credentials before the first service is stopped.
        self.command(['sudo', '-v'])
        try:
            for unit in STOP_ORDER:
                if unit in self.selected:
                    self.systemctl('stop', unit)
            if not self.restart_only:
                self.install_candidate()
            startup_failures = set()
            for unit in self.selected:
                try:
                    self.systemctl('start', unit)
                except RestartError:
                    if unit == DISPLAY:
                        raise
                    startup_failures.add(unit)
            problems = self.verify() | startup_failures
            if DISPLAY in problems:
                self.diagnostics(problems)
                raise RestartError('Renderer startup or socket verification failed.')
            if problems:
                self.diagnostics(problems)
                print('Client restart finished with service failures; healthy services remain running.', file=sys.stderr)
                return 1
        except (RestartError, KeyboardInterrupt):
            self.recover()
            self.diagnostics(self.selected)
            raise
        finally:
            if self.temporary.exists() or self.temporary.is_symlink():
                self.command(['sudo', 'rm', '-f', '--', self.temporary])
        print('Client restart complete. Existing animations were reloaded; no GIF conversion was performed.')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart-only', action='store_true', help='restart services without building or installing')
    parser.add_argument('--dry-run', action='store_true', help='check the installation and print actions without changing anything')
    args = parser.parse_args(argv)
    client = ClientRestart(restart_only=args.restart_only, dry_run=args.dry_run)
    try:
        if os.geteuid() == 0:
            raise RestartError('Run as the Pi voice user, without sudo; privileged steps request sudo themselves.')
        if args.dry_run:
            return client.execute()
        with restart_lock(client.native / 'build'):
            return client.execute()
    except (RestartError, OSError, ValueError) as error:
        print(f'Client restart failed: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Client restart interrupted.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
