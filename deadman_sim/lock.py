import fcntl
import os
from pathlib import Path

from .storage import Storage


class IdentityChanged(RuntimeError):
    pass


class SharedLock:
    def __init__(self, root, state_name="deadman-rescue"):
        self.root = Path(root).absolute()
        self.state_dir = self.root / state_name
        self.path = self.root / ".deadman-rescue.lock"
        Storage(self.state_dir)
        if self.path.is_symlink():
            raise ValueError("symlink lock")
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(self.fd)
            raise PermissionError("lock must be private and owned by caller")
        self.inode = self._identity(os.fstat(self.fd))
        self.directory = self._dir_identity()
        self.held = False
        self.invalid = False

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def _dir_identity(self):
        try:
            if self.state_dir.is_symlink():
                raise IdentityChanged("state symlink")
            return self._identity(self.state_dir.stat())
        except FileNotFoundError:
            return None

    def check(self):
        try:
            changed = (self.path.is_symlink() or
                       self._identity(self.path.stat()) != self.inode or
                       self._dir_identity() != self.directory)
        except FileNotFoundError:
            changed = True
        if self.invalid or changed:
            self.invalid = True
            raise IdentityChanged("lock or state identity changed")

    def acquire(self):
        self.check()
        if self.held:
            raise RuntimeError("already held")
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held = True
        try:
            self.check()
        except BaseException:
            self.release()
            raise
        return self

    def require(self):
        if not self.held:
            raise RuntimeError("shared lock required")
        self.check()

    def release(self):
        if self.held:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.held = False

    def close(self):
        self.release()
        os.close(self.fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
