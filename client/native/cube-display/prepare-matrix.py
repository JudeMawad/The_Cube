#!/usr/bin/env python3
"""Prepare a patched Git snapshot without changing the dependency checkout."""

import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile


def prepare():
    app = Path(__file__).resolve().parent
    repo = app.parents[1] / "third_party/rpi-rgb-led-matrix"
    patch = app.parents[1] / "patches/rp1-pio-stop-dma-before-sm.patch"
    build = app / "build"
    destination = build / "rgbmatrix"
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    # Do not silently ignore local edits when building a committed snapshot.
    subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "HEAD", "--"], check=True
    )
    fingerprint = commit + "\n" + hashlib.sha256(
        patch.read_bytes() + Path(__file__).read_bytes()
    ).hexdigest() + "\n"
    stamp = destination / ".cube-source"
    if stamp.exists() and stamp.read_text() == fingerprint:
        return

    build.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="rgbmatrix-source-", dir=build))
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "HEAD"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(staging, filter="data")
    subprocess.run(
        ["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(patch)],
        check=True, cwd=staging
    )
    # Verify the patch is present before stamping the generated tree.
    subprocess.run(
        ["patch", "--batch", "--reverse", "--dry-run", "--fuzz=0",
         "-p1", "-i", str(patch)], check=True, cwd=staging
    )
    (staging / ".cube-source").write_text(fingerprint)
    if destination.exists():
        # Preserve previous generated sources/artifacts when inputs change.
        destination.rename(build / (staging.name + "-previous"))
    staging.rename(destination)


if __name__ == "__main__":
    prepare()
