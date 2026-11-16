from pathlib import Path
import tempfile
import unittest

from deadman_sim.crash import CrashInjector, SimulatedCrash
from deadman_sim.ledger import Ledger, LoadStatus, canonical, digest
from deadman_sim.lock import SharedLock
from deadman_sim.storage import Storage, StorageExhausted


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(Path(self.tmp.name) / "deadman-rescue")
        self.storage.initialize()
        self.lock = SharedLock(self.tmp.name)
        self.addCleanup(self.lock.close)
        self.ledger = Ledger(self.storage, self.lock)
        with self.lock:
            self.ledger.initialize("instance-1")

    def append(self, **kwargs):
        with self.lock:
            return self.ledger.append(**kwargs)

    def tail_name(self):
        return self.ledger.load().head["tail"]

    def test_empty_and_reducer_compaction(self):
        self.assertEqual(self.ledger.load().state["state"], "UNKNOWN")
        self.append(tx_id="arm", operation="arm", phase="PREPARED", arm_id="a",
                    budget={"reserved": 1}, stop_generation=2)
        self.append(tx_id="intent", type="INTENT", intent="off", arm_id="a")
        before = self.ledger.load()
        with self.lock:
            self.ledger.compact()
        after = Ledger(self.storage, self.lock).load()
        self.assertEqual(after.status, LoadStatus.OK)
        self.assertEqual(after.state, before.state)
        self.assertIn("arm", after.state["pending_txs"])
        self.assertEqual(after.seq, before.seq)
        self.append(tx_id="arm", operation="arm", arm_id="a")
        self.assertEqual(self.ledger.load().state["armed_arm_id"], "a")

    def test_bad_tail_preserved_before_truncate(self):
        self.append()
        name = self.tail_name()
        self.storage.append_record(name, b'{"broken"')
        loaded = self.ledger.load()
        self.assertEqual(loaded.status, LoadStatus.BAD_TAIL)
        self.assertEqual(loaded.seq, 1)
        with self.assertRaises(RuntimeError):
            self.ledger.repair_bad_tail()
        with self.lock:
            self.storage.fail_next("ENOSPC")
            with self.assertRaises(OSError):
                self.ledger.repair_bad_tail()
            self.assertEqual(self.ledger.load().status, LoadStatus.BAD_TAIL)
            evidence = self.ledger.repair_bad_tail()
        self.assertEqual(self.storage.read(evidence), b'{"broken"')
        self.assertEqual(self.ledger.load().status, LoadStatus.OK)

    def test_mid_corruption_never_truncated(self):
        self.append()
        name = self.tail_name()
        self.storage.append_record(name, b"bad\n{}\n")
        self.assertEqual(self.ledger.load().status, LoadStatus.MID_CORRUPTION)
        with self.lock, self.assertRaises(ValueError):
            self.ledger.repair_bad_tail()

    def test_seq_rollback_and_unknown_version(self):
        record = self.append()
        name = self.tail_name()
        self.storage.append_record(name, canonical(record) + b"\n")
        self.assertEqual(self.ledger.load().status, LoadStatus.SEQ_ROLLBACK)
        record.update(seq=2, version=2)
        self.storage.atomic_write(name, canonical(record) + b"\n")
        self.assertEqual(self.ledger.load().status, LoadStatus.UNKNOWN_VERSION)

    def test_snapshot_head_directory_and_cache_errors(self):
        self.storage.atomic_write("state", canonical(dict(seq=99, instance_generation="instance-1")))
        self.assertEqual(self.ledger.load().status, LoadStatus.CACHE_AHEAD)
        self.storage.delete("state")
        snap = self.ledger.load().head["snapshot"]
        self.storage.atomic_write(snap, b"{}")
        self.assertEqual(self.ledger.load().status, LoadStatus.SNAPSHOT_CORRUPT)
        self.storage.atomic_write("HEAD", b"{}")
        self.assertEqual(self.ledger.load().status, LoadStatus.HEAD_INVALID)
        self.storage.delete("HEAD")
        self.assertEqual(self.ledger.load().status, LoadStatus.HEAD_MISSING)
        missing = Storage(Path(self.tmp.name) / "missing")
        self.assertEqual(Ledger(missing, self.lock).load().status, LoadStatus.DIR_MISSING)
        self.assertFalse(missing.root.exists())

    def test_head_switch_crashes_recover_only_selected_generation(self):
        self.append()
        for point in ("snapshot_written", "tail_written", "before_head_switch", "head_switched"):
            with self.subTest(point=point):
                before = self.ledger.load()
                self.ledger.crash = CrashInjector(point)
                with self.lock, self.assertRaises(SimulatedCrash):
                    self.ledger.compact()
                recovered = Ledger(self.storage, self.lock).load()
                self.assertEqual(recovered.status, LoadStatus.OK)
                self.assertEqual(recovered.state, before.state)
                self.assertEqual(recovered.seq, before.seq)
                self.assertEqual(recovered.generation,
                                 before.generation + (point == "head_switched"))

    def test_enospc_and_record_limit(self):
        self.storage.fail_next("ENOSPC")
        with self.lock, self.assertRaises(StorageExhausted):
            self.ledger.append(operation="arm")
        self.assertEqual(self.ledger.load().seq, 0)
        with self.lock, self.assertRaises(StorageExhausted):
            self.ledger.append(result="x" * 8192)
        self.assertEqual(self.ledger.load().seq, 0)

    def test_checksum_mutation(self):
        record = self.append()
        record["operation"] = "other"
        self.storage.atomic_write(self.tail_name(), canonical(record) + b"\n")
        self.assertEqual(self.ledger.load().status, LoadStatus.BAD_TAIL)

    def test_non_object_tail(self):
        self.storage.append_record(self.tail_name(), b"[]\n")
        self.assertEqual(self.ledger.load().status, LoadStatus.BAD_TAIL)

    def test_invalid_fields_not_written(self):
        for values in (dict(source_boot_id=[]), dict(phase="garbage"),
                       dict(stop_generation=-1), dict(budget=1)):
            with self.subTest(values=values), self.lock, self.assertRaises(ValueError):
                self.ledger.append(**values)
        self.assertEqual(self.ledger.load().seq, 0)

    def test_snapshot_valid_checksum_invalid_shape(self):
        loaded = self.ledger.load()
        snap = dict(version=1, generation=1, seq=0, checksum="", state={"state": "ARMED"})
        self.storage.atomic_write(loaded.head["snapshot"],
                                  canonical(dict(payload=snap, checksum=digest(snap))))
        self.assertEqual(self.ledger.load().status, LoadStatus.SNAPSHOT_CORRUPT)

    def test_preserves_consumption_budget_and_unfinished_fault(self):
        self.append(tx_id="intent", type="INTENT", intent="off", arm_id="old")
        self.append(type="BOOT", intent="intent", arm_id="old",
                    consumed_by_boot_id="boot-2", budget={"used": 1})
        self.append(tx_id="unfinished", phase="PREPARED", operation="arm", arm_id="new")
        self.append(tx_id="unfinished", phase="CLEARED_VERIFIED", operation="arm")
        self.append(tx_id="unfinished", phase="FAULT", operation="arm", error="write")
        before = self.ledger.load()
        with self.lock:
            self.ledger.compact()
        after = self.ledger.load()
        self.assertEqual(after.status, LoadStatus.OK)
        self.assertEqual(after.state, before.state)
        self.assertEqual(after.state["consumed_arms"], {"old": "boot-2"})
        self.assertTrue(after.state["intents"]["intent"]["consumed"])
        self.assertEqual(after.state["pending_txs"]["unfinished"]["arm_id"], "new")

    def test_head_rename_crashes(self):
        self.append()
        for point in ("HEAD:file_fsynced", "HEAD:before_rename", "HEAD:renamed", "HEAD:dir_fsynced"):
            with self.subTest(point=point):
                before = self.ledger.load()
                self.storage.crash = CrashInjector(point)
                with self.lock, self.assertRaises(SimulatedCrash):
                    self.ledger.compact()
                recovered = Ledger(self.storage, self.lock).load()
                self.assertEqual(recovered.status, LoadStatus.OK)
                self.assertEqual(recovered.state, before.state)
                self.assertEqual(recovered.generation, before.generation +
                                 (point in ("HEAD:renamed", "HEAD:dir_fsynced")))

    def test_quota_compaction_does_not_drop_evidence(self):
        self.append(tx_id="pending", phase="PREPARED", operation="arm", arm_id="a")
        before = self.ledger.load()
        self.storage.quota = self.storage.usage() + 1
        with self.lock, self.assertRaises(StorageExhausted):
            self.ledger.append(operation="arm")
        after = self.ledger.load()
        self.assertEqual(after.seq, before.seq)
        self.assertEqual(after.state, before.state)
