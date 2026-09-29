#!/usr/bin/env python3
"""Download the Kokoro v1.0 model files used by Cube."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from ai_node.config import KokoroConfig


def download(url: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 0:
        print(f"Skipping existing file: {destination}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} -> {destination}")

    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=destination.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with urlopen(url, timeout=60) as response, tmp_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        expected = response.headers.get("Content-Length")
        size = tmp_path.stat().st_size
        if size == 0 or (expected is not None and size != int(expected)):
            raise RuntimeError(f"Incomplete download: {url}")
        tmp_path.replace(destination)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    try:
        config = KokoroConfig.from_env()
        for url, destination in ((MODEL_URL, config.model_path), (VOICES_URL, config.voices_path)):
            download(url, destination)
    except Exception as error:
        print(f"Kokoro setup failed: {error}", file=sys.stderr)
        return 1

    print(f"Kokoro files are ready: {config.model_path}, {config.voices_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
