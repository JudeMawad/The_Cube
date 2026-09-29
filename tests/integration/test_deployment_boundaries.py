"""Runtime imports remain service-local; cache sharing is explicit configuration."""
import ast
from pathlib import Path
import unittest

from ai_node.config import KokoroConfig as AIKokoro, STTConfig
from core.config import KokoroConfig as BackendKokoro, BackendConfig
from tests.paths import ROOT


class DeploymentBoundaryTests(unittest.TestCase):
    def test_services_have_no_cross_service_runtime_imports(self):
        for directory, forbidden in (
            ("ai_node", {"server", "client", "core", "features", "integrations"}),
            ("server", {"ai_node", "client"}),
            ("client/app", {"ai_node", "server", "core", "features", "integrations"}),
        ):
            for path in (ROOT / directory).rglob("*.py"):
                if any(part in {".venv", "assets", "build", "__pycache__"} for part in path.parts):
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        names = [node.module]
                    for name in names:
                        self.assertNotIn(name.split(".")[0], forbidden, str(path))

    def test_colocated_services_share_only_explicitly_configured_paths(self):
        self.assertNotEqual(AIKokoro.from_env({}).model_path, BackendKokoro.from_env({}).model_path)
        shared = {prefix + suffix: value for prefix in ("CUBE", "CUBE_AI")
                  for suffix, value in (("_KOKORO_MODEL_PATH", "/shared/kokoro/model.onnx"),
                                        ("_KOKORO_VOICES_PATH", "/shared/kokoro/voices.bin"),
                                        ("_WHISPER_CACHE_DIR", "/shared/whisper"))}
        ai, backend = AIKokoro.from_env(shared), BackendKokoro.from_env(shared)
        self.assertEqual(ai.model_path, backend.model_path)
        self.assertEqual(ai.voices_path, backend.voices_path)
        self.assertEqual(STTConfig.from_env(shared).cache_dir,
                         BackendConfig.from_env(shared).whisper_cache_dir)
