"""Exercise the Linux launcher without CUDA libraries, inference, or a server."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


SOURCE = Path(__file__).resolve().parents[2] / "ai_node"
STUB_UVICORN = """
import json
import os
import sys
from ai_node.config import STTConfig
import nvidia.cublas.lib
import nvidia.cudnn.lib

print(json.dumps({
    "args": sys.argv[1:],
    "cwd": os.getcwd(),
    "prefix": sys.prefix,
    "executable": sys.executable,
    "library_path": os.environ["LD_LIBRARY_PATH"],
    "settings": vars(STTConfig.from_env()),
    "package_files": [nvidia.cublas.lib.__file__, nvidia.cudnn.lib.__file__],
}))
"""


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cube launcher ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repository with spaces"
        self.node = self.root / "ai_node"
        self.node.mkdir(parents=True)
        self.launcher = self.node / "start.sh"
        shutil.copy2(SOURCE / "start.sh", self.launcher)
        shutil.copy2(SOURCE / "config.py", self.node / "config.py")
        self.environment = self.node / ".venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.environment)
        self.python = self.environment / "bin" / "python"
        self.site = self.environment / "lib" / (
            f"python{sys.version_info.major}.{sys.version_info.minor}"
        ) / "site-packages"
        self.libraries = []
        for package, filename in (
            ("cublas", "libcublas.so.12"), ("cudnn", "libcudnn.so.9"),
        ):
            library = self.site / "nvidia" / package / "lib" / filename
            library.parent.mkdir(parents=True)
            library.touch()  # Discovery validates files; no shared library is loaded.
            self.libraries.append(library)
        (self.site / "uvicorn.py").write_text(STUB_UVICORN)
        self.env = {
            key: value for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH"}
            and not key.startswith("CUBE_AI_WHISPER_")
        }

    def launch(self, **overrides):
        return subprocess.run(
            [str(self.launcher)], cwd=self.root.parent,
            env={**self.env, **overrides}, capture_output=True, text=True, timeout=15,
        )

    def result(self, **overrides):
        result = self.launch(**overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_runtime_failure(self, library):
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")  # Stub Uvicorn must never start.
        self.assertIn(library, result.stderr)
        self.assertIn("-m pip install", result.stderr)

    def test_namespace_packages_and_server_invocation(self):
        result = self.result()
        self.assertEqual(result["package_files"], [None, None])
        self.assertEqual(result["args"], [
            "ai_node.app:app", "--host", "0.0.0.0", "--port", "8766", "--workers", "1",
        ])
        self.assertEqual(Path(result["cwd"]), self.root)
        self.assertEqual(Path(result["prefix"]), self.environment)
        self.assertEqual(Path(result["executable"]), self.python)
        self.assertEqual(result["settings"], {
            "model": "base.en", "device": "cuda", "compute_type": "float16",
            "cache_dir": str(self.node / "assets/models/whisper"),
        })

    def test_library_path_unset_empty_and_inherited(self):
        expected = ":".join(str(library.parent) for library in self.libraries)
        for previous in (None, "", "/existing/one:/existing/two"):
            with self.subTest(previous=previous):
                overrides = {} if previous is None else {"LD_LIBRARY_PATH": previous}
                result = self.result(**overrides)
                self.assertEqual(
                    result["library_path"],
                    expected + (":" + previous if previous else ""),
                )

    def test_explicit_model_settings_are_preserved(self):
        result = self.result(
            CUBE_AI_WHISPER_MODEL="/models/custom",
            CUBE_AI_WHISPER_DEVICE="cpu",
            CUBE_AI_WHISPER_COMPUTE_TYPE="int8",
        )
        self.assertEqual(result["settings"], {
            "model": "/models/custom", "device": "cpu", "compute_type": "int8",
            "cache_dir": str(self.node / "assets/models/whisper"),
        })

    def test_searches_all_namespace_locations(self):
        extra_site = self.root.parent / "extra packages"
        for library in self.libraries:
            (extra_site / library.parent.relative_to(self.site)).mkdir(parents=True)
        # Earlier package directories exist but contain no required library files.
        result = self.result(PYTHONPATH=str(extra_site))
        self.assertEqual(result["package_files"], [None, None])
        self.assertEqual(
            result["library_path"], ":".join(str(p.parent) for p in self.libraries),
        )

    def test_regular_packages(self):
        for library in self.libraries:
            for directory in (library.parent, library.parent.parent, self.site / "nvidia"):
                (directory / "__init__.py").touch()
        result = self.result()
        self.assertTrue(all(result["package_files"]))

    def test_missing_package(self):
        for library in self.libraries:
            with self.subTest(library=library.name):
                package = library.parent.parent
                hidden = package.with_name(package.name + "_hidden")
                package.rename(hidden)
                try:
                    self.assert_runtime_failure(library.name)
                finally:
                    hidden.rename(package)

    def test_directory_without_expected_library(self):
        for library in self.libraries:
            with self.subTest(library=library.name):
                library.unlink()
                try:
                    self.assert_runtime_failure(library.name)
                finally:
                    library.touch()

    def test_valid_and_broken_library_symlinks(self):
        targets = []
        for library in self.libraries:
            target = library.with_name(library.name + ".actual")
            library.rename(target)
            library.symlink_to(target.name)
            targets.append(target)
        self.result()
        for library, target in zip(self.libraries, targets):
            with self.subTest(library=library.name):
                target.unlink()
                try:
                    self.assert_runtime_failure(library.name)
                finally:
                    target.touch()

    def test_missing_virtual_environment_python(self):
        self.python.unlink()
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("AI-node Python not found", result.stderr)
        self.assertIn("ai_node/requirements.txt", result.stderr)
