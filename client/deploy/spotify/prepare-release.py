#!/usr/bin/env python3
"""Prepare an immutable candidate from an official archive; never install or start it."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

from soloist_release import SOURCE_URL, ReleaseError, age_status, parse_version, sha256, validate_elf


def prepare(archive, output):
    if platform.machine() != "aarch64":
        raise ReleaseError("Prepare and validate this ARM64 candidate on the Pi.")
    if output.exists() or output.is_symlink():
        raise ReleaseError("Candidate directory already exists; choose a new directory.")
    # Extract only named regular members; never trust archive paths or links.
    with tarfile.open(archive, "r:gz") as bundle:
        wanted = {}
        for member in bundle:
            if member.name in ("soloist", "CHANGELOG.md", "THIRD_PARTY_LICENSES.txt"):
                if member.name in wanted or not member.isfile() or not 0 < member.size <= 128 * 1024 * 1024:
                    raise ReleaseError("Unexpected archive member.")
                wanted[member.name] = member
        if "soloist" not in wanted or "THIRD_PARTY_LICENSES.txt" not in wanted:
            raise ReleaseError("Archive is missing the binary or third-party notices.")
        output.mkdir(mode=0o700)
        try:
            for name, member in wanted.items():
                with bundle.extractfile(member) as source, (output / name).open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                (output / name).chmod(0o755 if name == "soloist" else 0o644)
            binary = output / "soloist"
            validate_elf(binary)
            result = subprocess.run([str(binary.resolve()), "--version"], capture_output=True,
                                    text=True, timeout=10, check=False)
            if result.returncode:
                raise ReleaseError("Candidate version check failed.")
            version = result.stdout.strip()
            built = parse_version(version)
            status = age_status(built)
            if status["status"] != "ok":
                raise ReleaseError("New candidate must be less than 60 days old.")
            record = {"schema_version": 1, "source_url": SOURCE_URL,
                      "archive_sha256": sha256(archive), "binary_sha256": sha256(binary),
                      "version_output": version, "built_at": built.isoformat(),
                      "prepared_at": datetime.now(timezone.utc).isoformat()}
            (output / "release.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            (output / "release.json").chmod(0o644)
        except BaseException:
            shutil.rmtree(output)
            raise
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True,
                        help="Archive downloaded directly from Spotify; executing --version trusts its code")
    parser.add_argument("--output", type=Path, required=True, help="New candidate directory outside git")
    args = parser.parse_args(argv)
    try:
        record = prepare(args.archive, args.output)
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        print("Candidate preparation failed; check archive, architecture, age, and destination.", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
