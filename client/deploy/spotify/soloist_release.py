"""Offline Soloist release validation. No credentials, downloads, or activation."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import stat

SOURCE_URL = "https://soloist-builds.spotifycdn.com/soloist_release_arm64.tar.gz"
DEFAULT_RELEASE = Path("/opt/cube/soloist/current")
LIFETIME = timedelta(days=90)
VERSION = re.compile(
    r"soloist (?P<version>[0-9]+(?:\.[0-9]+)+) build (?P<epoch>[0-9]{10}) "
    r"\((?P<date>[0-9]{8})\) \((?P<revision>[A-Za-z0-9_-]+)\) \(linux/aarch64\)"
)


class ReleaseError(ValueError):
    """Safe, fixed diagnostics; never include subprocess output or secret values."""


def parse_version(output):
    match = VERSION.fullmatch(output.strip())
    if not match:
        raise ReleaseError("Unrecognized Soloist version or architecture; review upstream before updating.")
    built = datetime.fromtimestamp(int(match["epoch"]), timezone.utc)
    if built.strftime("%Y%m%d") != match["date"]:
        raise ReleaseError("Build timestamp and build date disagree.")
    return built


def age_status(built, now=None):
    now = now or datetime.now(timezone.utc)
    if built > now + timedelta(minutes=5):
        raise ReleaseError("Build timestamp is in the future; check the system clock and release.")
    age = max(timedelta(), now - built)
    level = "ok"
    for days, label in ((60, "warning"), (75, "urgent"), (83, "critical"), (90, "expired")):
        if age >= timedelta(days=days):
            level = label
    return {"status": level, "age_days": age.days,
            "expires_at": (built + LIFETIME).isoformat()}


def regular_file(path, maximum):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
        raise ReleaseError("Release file is missing, oversized, empty, or not a regular file.")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_elf(path):
    regular_file(path, 128 * 1024 * 1024)
    with path.open("rb") as stream:
        header = stream.read(20)
    # ELF64, little endian, AArch64 (EM_AARCH64 = 183).
    if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
        raise ReleaseError("Candidate is not an ARM64 Linux executable.")


def load_release(directory, now=None):
    # Resolve current once, so promotion cannot mix two release directories.
    directory = Path(directory).resolve(strict=True)
    manifest = directory / "release.json"
    regular_file(manifest, 16 * 1024)
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            raise ValueError
        if record["source_url"] != SOURCE_URL:
            raise ValueError
        for key in ("binary_sha256", "archive_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", record[key]):
                raise ValueError
        built = parse_version(record["version_output"])
        if record["built_at"] != built.isoformat():
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ReleaseError("Invalid release manifest; prepare the candidate again.") from None
    binary = directory / "soloist"
    validate_elf(binary)
    if not binary.stat().st_mode & stat.S_IXUSR or sha256(binary) != record["binary_sha256"]:
        raise ReleaseError("Soloist executable permission or recorded checksum does not match.")
    return directory, record, age_status(built, now)
