from deadman_sim.crash import CrashInjector, SimulatedCrash
from deadman_sim.ledger import State
from deadman_sim.system import Module
from support import ModuleCase


class EngineTests(ModuleCase):
    def test_crash_boundaries_recover_without_blind_actions(self):
        self.arm()
        for point in ("before_prepared", "after_prepared", "after_clear",
                      "after_clear_evidence", "after_write", "after_armed_verified", "after_commit"):
            with self.subTest(point=point):
                if self.module.state != State.ARMED:
                    self.assertTrue(self.module.repair_fault()["success"])
                    self.assertTrue(self.module.enable()["success"])
                    self.arm()
                self.clock.advance(60)
                self.module.crash = CrashInjector(point)
                with self.assertRaises(SimulatedCrash):
                    self.module.tick()
                rebuilt = self.make_module()
                recovered = rebuilt.recover()
                self.assertTrue(recovered["success"])
                self.assertEqual(rebuilt.executed_actions, [])
                if point in ("after_write", "after_armed_verified", "after_commit", "before_prepared"):
                    self.assertEqual(rebuilt.state, State.ARMED)
                elif point == "after_prepared":
                    self.assertEqual(rebuilt.state, State.FAULT)
                else:
                    self.assertEqual(rebuilt.state, State.ARM_PENDING)
                    self.assertIsNone(rebuilt.rtc.read_alarm())
                    self.assertTrue(rebuilt.tick()["success"])
                self.module = rebuilt

    def test_cross_boot_pending_is_reconciling(self):
        self.module.crash = CrashInjector("after_write")
        with self.assertRaises(SimulatedCrash):
            self.module.tick()
        self.clock.new_boot()
        rebuilt = self.make_module()
        result = rebuilt.recover()
        self.assertFalse(result["success"])
        self.assertEqual(rebuilt.state, State.RECONCILING)
        self.assertFalse(rebuilt.tick()["success"])

    def test_prepare_and_commit_storage_failure(self):
        self.module.storage.fail_next("ENOSPC")
        result = self.module.tick()
        self.assertFalse(result["success"])
        self.assertTrue(self.module.runtime_fault)
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertFalse(self.module.tick()["success"])

    def test_commit_failure_reports_uncertain_hardware(self):
        original = self.module.ledger.append
        def append(**values):
            if values.get("phase") == "COMMITTED" and values.get("operation") == "arm":
                self.module.storage.fail_next("ENOSPC")
            return original(**values)
        self.module.ledger.append = append
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.runtime_fault)
        self.assertIsNotNone(self.module.rtc.read_alarm())
        self.assertTrue(self.module.ledger.load().state["pending_txs"])

    def test_off_crash_after_stop_never_executes(self):
        self.arm()
        self.module.crash = CrashInjector("after_stop_generation")
        with self.assertRaises(SimulatedCrash):
            self.module.request_off()
        rebuilt = self.make_module()
        rebuilt.recover()
        self.assertEqual(rebuilt.state, State.STAND_DOWN)
        self.assertIsNone(rebuilt.rtc.read_alarm())
        self.assertFalse(rebuilt.tick()["success"])
        self.assertEqual(rebuilt.executed_actions, [])

    def test_off_pending_recovery_clears_without_action_replay(self):
        self.arm()
        self.module.crash = CrashInjector("after_prepared")
        with self.assertRaises(SimulatedCrash):
            self.module.request_off()
        rebuilt = self.make_module()
        self.assertTrue(rebuilt.recover()["success"])
        self.assertIsNone(rebuilt.rtc.read_alarm())
        self.assertEqual(rebuilt.executed_actions, [])
        self.assertEqual(rebuilt.state, State.STAND_DOWN)

    def test_recovery_refuses_unexplained_foreign_value(self):
        self.arm()
        self.module.crash = CrashInjector("after_prepared")
        with self.assertRaises(SimulatedCrash):
            self.module.tick()
        self.module.rtc.external_write(99999)
        rebuilt = self.make_module()
        rebuilt.recover()
        self.assertEqual(rebuilt.state, State.UNKNOWN)
        self.assertEqual(rebuilt.rtc.read_alarm(), 99999)
        self.assertFalse(rebuilt.tick()["success"])

    def test_recovery_untrusted_written_value_cleans_only_own(self):
        self.module.crash = CrashInjector("after_write")
        with self.assertRaises(SimulatedCrash):
            self.module.tick()
        self.module.rtc.jump(-100)
        rebuilt = self.make_module()
        rebuilt.recover()
        self.assertEqual(rebuilt.state, State.QUARANTINE)
        self.assertIsNone(rebuilt.rtc.read_alarm())
        self.assertFalse(rebuilt.resume()["success"])

    def test_conflict_between_clear_and_write_is_not_overwritten(self):
        original = self.module.crash.hit
        def hit(point):
            if point == "before_write":
                self.module.rtc.external_write(99999)
            return original(point)
        self.module.crash.hit = hit
        self.assertFalse(self.module.tick()["success"])
        self.assertEqual(self.module.state, State.QUARANTINE)
        self.assertEqual(self.module.rtc.read_alarm(), 99999)

    def test_off_clear_and_commit_crashes_do_not_replay_actions(self):
        for index, point in enumerate(("after_clear", "after_clear_evidence", "after_commit")):
            with self.subTest(point=point):
                root = self.root / ("case-" + str(index))
                root.mkdir()
                module = Module(root, root / "hardware", self.clock, self.sensors)
                self.addCleanup(module.close)
                self.assertTrue(module.install()["success"])
                self.assertTrue(module.enable()["success"])
                self.assertTrue(module.tick()["success"])
                module.crash = CrashInjector(point)
                with self.assertRaises(SimulatedCrash):
                    module.request_off()
                rebuilt = Module(root, root / "hardware", self.clock, self.sensors)
                self.addCleanup(rebuilt.close)
                self.assertTrue(rebuilt.recover()["success"])
                self.assertIsNone(rebuilt.rtc.read_alarm())
                self.assertEqual(rebuilt.executed_actions, [])
