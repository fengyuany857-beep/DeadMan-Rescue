import errno
import os
from pathlib import Path
import tempfile
import unittest

from deadman_sim.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(Path(self.tmp.name) / "state")
        self.storage.initialize()

    def test_atomic_append_permissions(self):
        self.storage.atomic_write("a", b"one")
        self.storage.append_record("a", b"two")
        self.assertEqual(self.storage.read("a"), b"onetwo")
        self.assertEqual(self.storage.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.storage.path("a").stat().st_mode & 0o777, 0o600)

    def test_faults_never_success(self):
        for fault in ("ENOSPC", "EIO", "fsync", "rename", "dir_fsync"):
            with self.subTest(fault=fault):
                self.storage.fail_next(fault)
                with self.assertRaises(OSError) as raised:
                    self.storage.atomic_write("a", b"new")
                self.assertEqual(raised.exception.errno,
                                 errno.ENOSPC if fault == "ENOSPC" else errno.EIO)

    def test_append_failure(self):
        self.storage.fail_next("fsync")
        with self.assertRaises(OSError):
            self.storage.append_record("tail", b"evidence\n")

    def test_quota_counts_old_and_temporary(self):
        self.storage.quota = 10
        self.storage.atomic_write("a", b"123456")
        with self.assertRaises(OSError) as raised:
            self.storage.atomic_write("a", b"12345")
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertEqual(self.storage.read("a"), b"123456")

    def test_symlinks_and_traversal(self):
        os.symlink("elsewhere", self.storage.root / "link")
        for path in ("link", "../escape"):
            with self.assertRaises(ValueError):
                self.storage.atomic_write(path, b"bad")

    def test_symlinked_state_directory_rejected(self):
        link = Path(self.tmp.name) / "linked-state"
        link.symlink_to(self.storage.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            Storage(link)
