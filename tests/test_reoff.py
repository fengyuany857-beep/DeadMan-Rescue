from deadman_sim.classify import Evidence
from deadman_sim.crash import CrashInjector, SimulatedCrash
from deadman_sim.ledger import State
from support import ModuleCase


class ReoffTests(ModuleCase):
    def off_cycle(self):
        self.arm()
        result = self.module.request_off()
        self.module.close()
        self.clock.new_boot()
        self.module = self.make_module(dict(reoff_enabled=True))
        return result, Evidence("rtc_wake", True, result["arm_id"], True, False, True)

    def test_disabled_default_and_enabled_once(self):
        off, evidence = self.off_cycle()
        self.module.config["reoff_enabled"] = False
        result = self.module.boot(evidence)
        self.assertFalse(result["reoff"]["success"])
        self.assertEqual(len(self.module.executed_actions), 1)
        self.assertEqual(self.module.ledger.load().state["reoff_attempts"], {})

    def test_enabled_attempt_bound_to_consuming_boot_and_not_repeated(self):
        off, evidence = self.off_cycle()
        result = self.module.boot(evidence)
        self.assertTrue(result["reoff"]["success"])
        self.assertEqual(len(self.module.executed_actions), 2)
        self.assertEqual(self.module.ledger.load().state["reoff_attempts"][off["tx_id"]], 1)
        self.module.boot(evidence)
        self.new_boot(evidence, dict(reoff_enabled=True))
        self.assertEqual(len(self.module.executed_actions), 2)

    def test_crash_after_attempt_never_reissues(self):
        off, evidence = self.off_cycle()
        self.module.crash = CrashInjector("reoff_after_attempt")
        with self.assertRaises(SimulatedCrash):
            self.module.boot(evidence)
        self.module = self.make_module(dict(reoff_enabled=True))
        self.assertTrue(self.module.boot(evidence)["repeated"])
        self.assertEqual(len(self.module.executed_actions), 1)
        self.assertEqual(self.module.ledger.load().state["reoff_attempts"][off["tx_id"]], 1)

    def test_unknown_command_result_quarantined(self):
        _, evidence = self.off_cycle()
        self.module.config["reoff_command_result"] = "unknown"
        result = self.module.boot(evidence)
        self.assertFalse(result["reoff"]["success"])
        self.assertEqual(self.module.state, State.QUARANTINE)
        self.module.boot(evidence)
        self.assertEqual(len(self.module.executed_actions), 2)

    def test_manual_intervention_after_attempt_cancels_action(self):
        _, evidence = self.off_cycle()
        original = self.module.crash.hit
        def hit(point):
            if point == "reoff_before_action":
                self.module.manual_intervention = True
            original(point)
        self.module.crash.hit = hit
        self.assertFalse(self.module.boot(evidence)["reoff"]["success"])
        self.assertEqual(len(self.module.executed_actions), 1)

    def test_clear_failure_prevents_attempt_and_action(self):
        _, evidence = self.off_cycle()
        self.module.rtc.fail_clears = 1
        self.assertFalse(self.module.boot(evidence)["success"])
        self.assertEqual(self.module.ledger.load().state["reoff_attempts"], {})
        self.assertEqual(len(self.module.executed_actions), 1)

    def test_trusted_cancellation_immediately_before_action(self):
        off, evidence = self.off_cycle()
        original = self.module.crash.hit
        def hit(point):
            if point == "reoff_before_action":
                self.module._record(type="CANCEL", intent=off["tx_id"])
            original(point)
        self.module.crash.hit = hit
        self.assertFalse(self.module.boot(evidence)["reoff"]["success"])
        self.assertEqual(len(self.module.executed_actions), 1)
