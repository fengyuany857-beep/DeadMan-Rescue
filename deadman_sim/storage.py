import errno
import os
from pathlib import Path
import stat
import tempfile

from .crash import CrashInjector
from .params import TOTAL_LIMIT


class StorageExhausted(OSError):
    def __init__(self, message="storage exhausted"):
        super().__init__(errno.ENOSPC, message)


class Storage:
    def __init__(self, root, quota=TOTAL_LIMIT, crash=None):
        os.umask(0o077)
        self.root = Path(root).absolute()
        self.quota = quota
        self.crash = crash or CrashInjector()
        self.failures = {}
        self.peak_usage = 0
        self._check(self.root)
        if self.root.exists() and (not self.root.is_dir() or
                                   self.root.stat().st_mode & 0o077):
            raise PermissionError("state directory must be private")

    def _check(self, path):
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError("symlink in state path")
            if part.exists():
                info = part.stat()
                if info.st_uid not in (0, os.geteuid()):
                    raise PermissionError("unexpected owner")
                if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                    raise PermissionError("writable state ancestor")

    def path(self, name):
        p = self.root / name
        if p.parent != self.root or p.name in ("", ".", ".."):
            raise ValueError("flat state paths required")
        self._check(p)
        if p.exists() and not p.is_file():
            raise ValueError("not a regular file")
        if p.exists() and p.stat().st_mode & 0o077:
            raise PermissionError("state file must be private")
        return p

    def initialize(self):
        self._check(self.root)
        self.root.mkdir(mode=0o700, exist_ok=False)
        self.sync_dir(self.root.parent)

    def fail_next(self, operation, count=1):
        self.failures[operation] = count

    def _fault(self, operation):
        if self.failures.get(operation, 0):
            self.failures[operation] -= 1
            code = errno.ENOSPC if operation == "ENOSPC" else errno.EIO
            raise OSError(code, operation)

    def usage(self):
        self._check(self.root)
        total = 0
        for p in self.root.iterdir():
            self.path(p.name)
            total += p.stat().st_size
        return total

    def ensure_space(self, size):
        self._fault("ENOSPC")
        self._fault("EIO")
        anticipated = self.usage() + size
        if anticipated > self.quota:
            raise StorageExhausted()
        self.peak_usage = max(self.peak_usage, anticipated)

    def _fsync(self, fd):
        self._fault("fsync")
        os.fsync(fd)

    def sync_dir(self, directory=None):
        self._fault("dir_fsync")
        fd = os.open(directory or self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._fsync(fd)
        finally:
            os.close(fd)

    def read(self, name):
        fd = os.open(self.path(name), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            return stream.read()

    def atomic_write(self, name, data):
        p = self.path(name)
        self.ensure_space(len(data))
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                self._fsync(stream.fileno())
            self.crash.hit(name + ":file_fsynced")
            self._fault("rename")
            self.crash.hit(name + ":before_rename")
            os.replace(tmp, p)
            self.crash.hit(name + ":renamed")
            self.sync_dir()
            self.crash.hit(name + ":dir_fsynced")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def append_record(self, name, data):
        p = self.path(name)
        self.ensure_space(len(data))
        created = not p.exists()
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(data)
            stream.flush()
            self._fsync(stream.fileno())
        self.crash.hit(name + ":appended_fsynced")
        if created:
            self.sync_dir()

    def truncate(self, name, offset):
        fd = os.open(self.path(name), os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.ftruncate(fd, offset)
            self._fsync(fd)
        finally:
            os.close(fd)

    def delete(self, name):
        self.path(name).unlink()
        self.sync_dir()

    def remove_directory(self):
        for path in tuple(self.root.iterdir()):
            self.delete(path.name)
        self.root.rmdir()
        self.sync_dir(self.root.parent)
