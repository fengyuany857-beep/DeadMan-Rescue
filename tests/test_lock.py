from pathlib import Path
import tempfile
import unittest

from deadman_sim.lock import IdentityChanged, SharedLock


class LockTests(unittest.TestCase):
    def test_same_process_exclusion(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "deadman-rescue").mkdir(mode=0o700)
            first, second = SharedLock(root), SharedLock(root)
            try:
                with first:
                    with self.assertRaises(BlockingIOError):
                        second.acquire()
                with second:
                    second.require()
            finally:
                first.close()
                second.close()

    def test_identity_replacement_stops_old_object(self):
        for target in ("directory", "lock"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                state = Path(root) / "deadman-rescue"
                state.mkdir(mode=0o700)
                lock = SharedLock(root)
                try:
                    with lock:
                        if target == "directory":
                            state.rename(Path(root) / "old")
                            state.mkdir(mode=0o700)
                        else:
                            lock.path.rename(Path(root) / "old-lock")
                            lock.path.touch(mode=0o600)
                        with self.assertRaises(IdentityChanged):
                            lock.require()
                    with self.assertRaises(IdentityChanged):
                        lock.acquire()
                finally:
                    lock.close()
