"""AI-node deployment independence, without starting inference."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.paths import ROOT


class DeploymentTests(unittest.TestCase):
    def test_node_imports_without_backend_or_inference_packages(self):
        script = '''
import importlib.abc
import sys
class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'server', 'client', 'core', 'features', 'integrations', 'faster_whisper', 'ctranslate2', 'torch', 'kokoro_onnx'}:
            raise AssertionError('Unexpected runtime import: ' + fullname)
sys.meta_path.insert(0, BlockRuntime())
sys.path.insert(0, sys.argv[1])
from ai_node.app import app, create_app
assert create_app().state.loader_task is None
assert app.title == 'Cube AI node'
from pathlib import Path
assert app.state.tts.model_path == Path(sys.argv[1]) / 'ai_node/assets/models/kokoro/kokoro-v1.0.onnx'
assert app.state.stt.config.cache_dir == str(Path(sys.argv[1]) / 'ai_node/assets/models/whisper')
'''
        with tempfile.TemporaryDirectory(prefix="cube isolated ai ") as directory:
            shutil.copytree(ROOT / "ai_node", Path(directory) / "ai_node",
                            ignore=shutil.ignore_patterns("assets", ".venv", "__pycache__"))
            result = subprocess.run([sys.executable, "-I", "-c", script, directory],
                                    cwd=directory, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
