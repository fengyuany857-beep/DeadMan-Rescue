import shutil

from deadman_sim.ledger import State
from support import ModuleCase


class DisarmTests(ModuleCase):
    def test_disarm_complete_closing_records(self):
        self.arm()
        result = self.module.disarm()
        self.assertTrue(result["success"])
        self.assertTrue(result["slot_empty"])
        self.assertEqual(result["pending_failures"], [])
        self.assertIsNotNone(result["arm_id"])
        self.assertEqual(self.module.state, State.DISARMED)
        records = [r for r in self.module.ledger.load().records if r["tx_id"] == result["tx_id"]]
        self.assertEqual([r["phase"] for r in records], ["PREPARED", "CLEARED_VERIFIED", "COMMITTED"])
        self.assertFalse(self.module.supervisor_restart()["success"])
        self.assertFalse(self.module.tick()["success"])

    def test_disarm_refuses_foreign(self):
        self.arm()
        self.module.rtc.external_write(9999)
        result = self.module.disarm()
        self.assertFalse(result["success"])
        self.assertFalse(result["slot_empty"])
        self.assertEqual(self.module.rtc.read_alarm(), 9999)

    def test_forced_directory_delete_leaves_unowned_residue(self):
        alarm = self.arm()
        shutil.rmtree(self.module.storage.root)
        self.assertFalse(self.module.tick()["success"])
        rebuilt = self.make_module()
        result = rebuilt.recover()
        self.assertEqual(result["status"], "DIR_MISSING")
        self.assertEqual(rebuilt.state, State.UNKNOWN)
        self.assertEqual(rebuilt.rtc.read_alarm(), alarm)
        self.assertFalse(rebuilt.disarm()["success"])
        self.assertFalse(rebuilt.storage.root.exists())

    def test_uninstall_clears_before_deleting(self):
        self.arm()
        self.assertTrue(self.module.uninstall()["success"])
        self.assertFalse(self.module.storage.root.exists())
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertTrue(self.module.rtc.storage.path("uninstall.json").exists())

    def test_disarm_does_not_bypass_fault_repair(self):
        self.arm()
        self.module.rtc.fail_writes = 3
        for _ in range(3):
            self.module.tick()
        self.assertEqual(self.module.state, State.FAULT)
        self.assertTrue(self.module.disarm()["success"])
        self.assertFalse(self.module.enable()["success"])
        self.assertTrue(self.module.repair_fault()["success"])
        self.assertTrue(self.module.enable()["success"])
