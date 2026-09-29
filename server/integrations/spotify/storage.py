"""Owner-only, bounded JSON storage and interprocess token refresh exclusion."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from .errors import SpotifyError

LIMIT = 32768


def private_directory(path, *, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SpotifyError("storage_invalid")
    return path


def read_private(path):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or not 0 < info.st_size <= LIMIT):
                raise SpotifyError("storage_invalid")
            data = source.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise SpotifyError("storage_invalid")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise SpotifyError("storage_invalid")
            return value
    except FileNotFoundError:
        raise SpotifyError("not_linked") from None
    except (OSError, ValueError, UnicodeError):
        raise SpotifyError("storage_invalid") from None


def write_private(path, value):
    path = Path(path)
    private_directory(path.parent)
    data = json.dumps(value, allow_nan=False).encode()
    if len(data) > LIMIT:
        raise SpotifyError("storage_invalid")
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".spotify-", dir=path.parent)
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class TokenStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "tokens.json"

    @contextmanager
    def locked(self):
        try:
            private_directory(self.directory)
            fd = os.open(self.directory / "tokens.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(fd, "r+") as lock:
                info = os.fstat(lock.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise SpotifyError("storage_invalid")
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
        except FileNotFoundError:
            raise SpotifyError("not_linked") from None
        except OSError:
            raise SpotifyError("storage_unavailable") from None

    def read(self):
        return read_private(self.path)

    def write(self, data):
        write_private(self.path, data)

    def delete(self):
        self.path.unlink(missing_ok=True)
