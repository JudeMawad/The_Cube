"""Offline receiver deployment tests; no account, audio server, or receiver daemon."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import stat
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[2] / "client/deploy/spotify"
spec = importlib.util.spec_from_file_location("soloist_release", TOOLS / "soloist_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
with patch.dict(sys.modules, {"soloist_release": release}):
    prepare = runpy.run_path(str(TOOLS / "prepare-release.py"))
    check = runpy.run_path(str(TOOLS / "check-build-age.py"))
    launch = runpy.run_path(str(TOOLS / "soloist-launch"))

VERSION = "soloist 1.3.8.84 build 1790488860 (20260927) (g5c3a2053ac) (linux/aarch64)"
BUILT = datetime(2026, 9, 27, 6, 1, tzinfo=timezone.utc)
ELF = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00" + b"test fixture"


class BuildAgeTests(unittest.TestCase):
    def test_observed_version_output(self):
        self.assertEqual(release.parse_version(VERSION + "\n"), BUILT)

    def test_reject_unknown_format_architecture_and_inconsistent_date(self):
        for value in ("1.3.8", VERSION.replace("aarch64", "x86_64"),
                      VERSION.replace("20260927", "20260928"), VERSION + "\nextra"):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.parse_version(value)

    def test_exact_age_boundaries(self):
        for day, before, after in ((60, "ok", "warning"), (75, "warning", "urgent"),
                                   (83, "urgent", "critical"), (90, "critical", "expired")):
            boundary = BUILT + timedelta(days=day)
            self.assertEqual(release.age_status(BUILT, boundary - timedelta(seconds=1))["status"], before)
            self.assertEqual(release.age_status(BUILT, boundary)["status"], after)

    def test_future_clock_is_invalid_not_fresh(self):
        with self.assertRaises(release.ReleaseError):
            release.age_status(BUILT, BUILT - timedelta(minutes=6))
        self.assertEqual(release.age_status(BUILT, BUILT - timedelta(seconds=10))["age_days"], 0)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        self.binary = self.candidate / "soloist"
        self.binary.write_bytes(ELF)
        self.binary.chmod(0o755)
        self.record = {"schema_version": 1, "version_output": VERSION,
                       "built_at": BUILT.isoformat(), "source_url": release.SOURCE_URL,
                       "binary_sha256": release.sha256(self.binary), "archive_sha256": "a" * 64}
        self.manifest = self.candidate / "release.json"
        self.write_record()

    def write_record(self):
        self.manifest.write_text(json.dumps(self.record))

    def test_current_symlink_resolves_consistent_release(self):
        current = self.root / "current"
        current.symlink_to(self.candidate)
        directory, record, status = release.load_release(current, BUILT)
        self.assertEqual(directory, self.candidate)
        self.assertEqual(record, self.record)
        self.assertEqual(status["status"], "ok")

    def test_changed_binary_fails_checksum(self):
        self.binary.write_bytes(ELF + b"changed")
        with self.assertRaises(release.ReleaseError):
            release.load_release(self.candidate, BUILT)

    def test_non_executable_rejected(self):
        self.binary.chmod(0o644)
        with self.assertRaises(release.ReleaseError):
            release.load_release(self.candidate, BUILT)

    def test_symlink_binary_rejected(self):
        target = self.root / "elsewhere"
        self.binary.rename(target)
        self.binary.symlink_to(target)
        with self.assertRaises(release.ReleaseError):
            release.load_release(self.candidate, BUILT)

    def test_bad_manifest(self):
        for content in ("not json", "[]", "{}", "null", "x" * 20000):
            self.manifest.write_text(content)
            with self.subTest(content=content[:10]), self.assertRaises(release.ReleaseError):
                release.load_release(self.candidate, BUILT)

    def test_timestamp_cannot_override_observed_build(self):
        self.record["built_at"] = (BUILT + timedelta(days=30)).isoformat()
        self.write_record()
        with self.assertRaises(release.ReleaseError):
            release.load_release(self.candidate, BUILT)

    def test_expired_rollback_is_detected(self):
        _, _, status = release.load_release(self.candidate, BUILT + timedelta(days=91))
        self.assertEqual(status["status"], "expired")

    def test_check_exit_codes_and_no_raw_manifest_output_on_failure(self):
        for level, code in (("ok", 0), ("warning", 0), ("expired", 10)):
            with patch.dict(check["main"].__globals__, load_release=lambda _: (None, self.record, {"status": level})):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(check["main"]([]), code)
        self.manifest.write_text("secret-test-marker")
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(check["main"](["--release", str(self.candidate)]), 2)
        self.assertNotIn("secret-test-marker", output.getvalue())


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "download.tar.gz"
        self.output = self.root / "candidate"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(prepare["platform"], "machine", return_value="aarch64"))
        self.run = self.stack.enter_context(patch.object(prepare["subprocess"], "run",
                                                        return_value=SimpleNamespace(returncode=0, stdout=VERSION)))
        self.stack.enter_context(patch.dict(prepare["prepare"].__globals__,
                                           age_status=lambda built: release.age_status(built, BUILT)))

    def archive_with(self, extra=None):
        with tarfile.open(self.archive, "w:gz") as bundle:
            for name, data in (("soloist", ELF), ("THIRD_PARTY_LICENSES.txt", b"notices"),
                               ("../../outside", b"never extracted")):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
            if extra:
                bundle.addfile(extra)

    def test_candidate_records_actual_build_and_preserves_notices(self):
        self.archive_with()
        record = prepare["prepare"](self.archive, self.output)
        self.assertEqual(record["built_at"], BUILT.isoformat())
        self.assertEqual(record["archive_sha256"], release.sha256(self.archive))
        self.assertEqual((self.output / "THIRD_PARTY_LICENSES.txt").read_text(), "notices")
        self.assertEqual(set(p.name for p in self.output.iterdir()),
                         {"soloist", "THIRD_PARTY_LICENSES.txt", "release.json"})
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0][-1], "--version")

    def test_existing_candidate_is_never_overwritten(self):
        self.output.mkdir()
        with self.assertRaises(release.ReleaseError):
            prepare["prepare"](self.archive, self.output)
        self.run.assert_not_called()

    def test_duplicate_or_link_member_rejected_before_execution(self):
        extra = tarfile.TarInfo("soloist")
        extra.type = tarfile.SYMTYPE
        extra.linkname = "/etc/passwd"
        self.archive_with(extra)
        with self.assertRaises(release.ReleaseError):
            prepare["prepare"](self.archive, self.output)
        self.assertFalse(self.output.exists())
        self.run.assert_not_called()

    def test_unrecognized_binary_output_cleans_candidate(self):
        self.archive_with()
        self.run.return_value.stdout = "unexpected output"
        with self.assertRaises(release.ReleaseError):
            prepare["prepare"](self.archive, self.output)
        self.assertFalse(self.output.exists())

    def test_old_candidate_not_promoted_by_new_install_date(self):
        self.archive_with()
        with patch.dict(prepare["prepare"].__globals__, age_status=lambda _: {"status": "warning"}):
            with self.assertRaises(release.ReleaseError):
                prepare["prepare"](self.archive, self.output)
        self.assertFalse(self.output.exists())


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.root / "soloist.key"
        self.key.write_text("private-test-value\n")
        self.key.chmod(0o440)
        # Reproduce root-owned systemd credentials without chown or root tests.
        # File reads and mode/type/size checks still use real temporary files.
        real_fstat = os.fstat

        def credential_stat(descriptor):
            info = real_fstat(descriptor)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_size=info.st_size)

        self.credential_stat = patch.object(os, "fstat", side_effect=credential_stat)
        self.credential_stat.start()
        self.addCleanup(self.credential_stat.stop)

    def test_systemd_root_owned_read_only_credentials_loaded_without_printing(self):
        self.key.chmod(0o600)
        self.key.write_text("x" * 37 + "\n")
        self.assertEqual(self.key.stat().st_size, 38)
        for mode in (0o400, 0o440):
            self.key.chmod(mode)
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(mode=oct(mode)), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(launch["read_key"](self.key), "x" * 37)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

    def test_systemd_unsafe_modes_rejected(self):
        for mode in (0o000, 0o040, 0o600, 0o640, 0o644, 0o444, 0o420,
                     0o402, 0o450, 0o540, 0o1440, 0o2440, 0o4440):
            info = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=0, st_size=38)
            with self.subTest(mode=oct(mode)), self.assertRaises(release.ReleaseError):
                launch["systemd_credential_file"](info)

    def test_systemd_non_root_owner_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_uid=1000, st_size=38)
        with self.assertRaises(release.ReleaseError):
            launch["systemd_credential_file"](info)

    def test_systemd_unsafe_types_and_sizes_rejected(self):
        for kind in (stat.S_IFDIR, stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK,
                     stat.S_IFCHR, stat.S_IFBLK):
            info = SimpleNamespace(st_mode=kind | 0o440, st_uid=0, st_size=38)
            with self.subTest(kind=kind), self.assertRaises(release.ReleaseError):
                launch["systemd_credential_file"](info)
        for size in (0, 8193):
            info = SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_uid=0, st_size=size)
            with self.subTest(size=size), self.assertRaises(release.ReleaseError):
                launch["systemd_credential_file"](info)
        launch["systemd_credential_file"](
            SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_uid=0, st_size=8192))

    def test_original_source_requires_0600_and_trusted_owner(self):
        for owner in (0, os.getuid()):
            info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=owner, st_size=38)
            launch["private_key_file"](info)
        for mode in (0o400, 0o440, 0o640, 0o644, 0o660, 0o606, 0o700, 0o4600):
            for owner in (0, os.getuid()):
                info = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=owner, st_size=38)
                with self.subTest(mode=oct(mode), owner=owner), self.assertRaises(release.ReleaseError):
                    launch["private_key_file"](info)
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1, st_size=38)
        with self.assertRaises(release.ReleaseError):
            launch["private_key_file"](info)

    def test_original_source_types_and_size_remain_strict(self):
        for kind, size in ((stat.S_IFDIR, 38), (stat.S_IFLNK, 38), (stat.S_IFIFO, 38),
                           (stat.S_IFREG, 0), (stat.S_IFREG, 8193)):
            info = SimpleNamespace(st_mode=kind | 0o600, st_uid=os.getuid(), st_size=size)
            with self.subTest(kind=kind, size=size), self.assertRaises(release.ReleaseError):
                launch["private_key_file"](info)

    def test_unsafe_key_permissions_and_symlink_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaises(release.ReleaseError):
            launch["read_key"](self.key)
        link = self.root / "link"
        link.symlink_to(self.key)
        with self.assertRaises(OSError):
            launch["read_key"](link)

    def test_bad_key_contents(self):
        for value in ("", "a\nb", "x" * 8193, "abc\x00def", " padded-key "):
            self.key.chmod(0o600)
            self.key.write_text(value)
            self.key.chmod(0o440)
            with self.assertRaises(release.ReleaseError):
                launch["read_key"](self.key)

    def test_storage_is_private_and_unsafe_existing_directory_rejected(self):
        directory = self.root / "state"
        launch["private_directory"](directory)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        directory.chmod(0o755)
        with self.assertRaises(release.ReleaseError):
            launch["private_directory"](directory)

    def test_sink_requires_exact_unique_name_and_class(self):
        node = {"type": "PipeWire:Interface:Node", "info": {"props": {
            "node.name": "cube.spotify", "media.class": "Audio/Sink"}}}
        for nodes, expected in (([node], True), ([], False), ([node, node], False),
                                ([{"type": "PipeWire:Interface:Node", "info": {"props": {
                                    "node.name": "Other Spotify", "media.class": "Audio/Sink"}}}], False)):
            with patch.object(launch["subprocess"], "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(nodes))):
                self.assertEqual(launch["music_sink_ready"](), expected)

    def test_launch_preserves_exec_ownership_and_uses_only_scoped_output(self):
        globals_ = launch["main"].__globals__
        source = self.root / ".config/cube/spotify/soloist.key"
        source.parent.mkdir(parents=True)
        source.write_text("private-test-value")
        source.chmod(0o600)
        with patch.dict(globals_, load_release=lambda _: (self.root, {}, {"status": "ok"}),
                        managed_release=lambda _: None, private_directory=lambda _: None,
                        music_sink_ready=lambda: True), \
                patch.dict(os.environ, CREDENTIALS_DIRECTORY=str(self.root)), \
                patch.object(Path, "home", return_value=self.root), \
                patch.object(os, "execv") as execute, patch.object(os, "umask"):
            launch["main"]()
        args = execute.call_args.args[1]
        self.assertEqual(args[args.index("--pipewire-device") + 1], "cube.spotify")
        self.assertEqual(args[args.index("--ws") + 1], "127.0.0.1:0")
        self.assertNotIn("--initial-volume", args)

    def test_expired_release_never_executes(self):
        globals_ = launch["main"].__globals__
        with patch.dict(globals_, load_release=lambda _: (self.root, {}, {"status": "expired"}),
                        managed_release=lambda _: None), patch.object(os, "execv") as execute, \
                patch.object(os, "umask"), redirect_stderr(io.StringIO()):
            self.assertEqual(launch["main"](), 10)
            execute.assert_not_called()

    def test_missing_sink_never_falls_back_and_diagnostics_hide_key(self):
        globals_ = launch["main"].__globals__
        source = self.root / ".config/cube/spotify/soloist.key"
        source.parent.mkdir(parents=True)
        source.write_text("private-test-value")
        source.chmod(0o600)
        output = io.StringIO()
        with patch.dict(globals_, load_release=lambda _: (self.root, {}, {"status": "ok"}),
                        managed_release=lambda _: None, private_directory=lambda _: None,
                        music_sink_ready=lambda: False), \
                patch.dict(os.environ, CREDENTIALS_DIRECTORY=str(self.root)), \
                patch.object(Path, "home", return_value=self.root), \
                patch.object(os, "execv") as execute, patch.object(os, "umask"), redirect_stderr(output):
            self.assertEqual(launch["main"](), 75)
            execute.assert_not_called()
        self.assertNotIn("private-test-value", output.getvalue())

    def test_unsafe_original_key_rejected_even_with_private_credential_copy(self):
        globals_ = launch["main"].__globals__
        source = self.root / ".config/cube/spotify/soloist.key"
        source.parent.mkdir(parents=True)
        source.write_text("private-test-value")
        with patch.dict(globals_, load_release=lambda _: (self.root, {}, {"status": "ok"}),
                        managed_release=lambda _: None), \
                patch.dict(os.environ, CREDENTIALS_DIRECTORY=str(self.root)), \
                patch.object(Path, "home", return_value=self.root), \
                patch.object(os, "execv") as execute, patch.object(os, "umask"), redirect_stderr(io.StringIO()):
            for mode in (0o440, 0o644):
                source.chmod(mode)
                self.assertEqual(launch["main"](), 78)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
