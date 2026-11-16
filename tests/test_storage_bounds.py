from unittest.mock import patch

from deadman_sim import params
from deadman_sim.ledger import State
from support import ModuleCase


class StorageBoundsTests(ModuleCase):
    def test_renewals_do_not_reparse_unchanged_verified_tail(self):
        self.arm()
        ledger = self.module.ledger
        with patch.object(ledger, "_load_disk", wraps=ledger._load_disk) as parse:
            self.assertTrue(all(result["success"] for result in self.module.run_for(1200)))
            self.assertEqual(parse.call_count, 0)

    def test_24h_renewals_compact_with_bounded_peak_and_rebuild(self):
        self.arm()
        with self.module.lock:
            self.module.ledger.append(tx_id="unfinished", phase="PREPARED", operation="observe")
            self.module._record(state=State.ARMED, operation="observe")
        results = self.module.run_for(24 * 3600 + 60)
        self.assertTrue(all(result["success"] for result in results))
        loaded = self.module.ledger.load()
        self.assertGreater(loaded.generation, 1)
        self.assertIn("unfinished", loaded.state["pending_txs"])
        self.assertEqual(loaded.state["budget"]["reserved"], 1)
        self.assertLessEqual(self.module.storage.usage(), params.TOTAL_LIMIT)
        self.assertLessEqual(self.module.storage.peak_usage, params.TOTAL_LIMIT)
        for path in self.module.storage.root.iterdir():
            if path.name.startswith("snapshot-"):
                self.assertLessEqual(path.stat().st_size, params.SNAPSHOT_LIMIT)
            if path.name.startswith("tail-"):
                self.assertLessEqual(path.stat().st_size, params.TAIL_LIMIT)
        rebuilt = self.make_module()
        recovered = rebuilt.ledger.load()
        self.assertEqual(recovered.state, loaded.state)
        self.assertEqual(recovered.seq, loaded.seq)

    def test_uncompressible_storage_stands_down_without_drop(self):
        self.arm()
        before = self.module.ledger.load()
        self.module.storage.quota = self.module.storage.usage() + 1
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.runtime_fault)
        self.assertEqual(self.module.ledger.load().seq, before.seq)
        self.assertEqual(self.module.ledger.load().state, before.state)
