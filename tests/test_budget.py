from deadman_sim.classify import Evidence
from deadman_sim.crash import CrashInjector, SimulatedCrash
from support import ModuleCase


class BudgetTests(ModuleCase):
    def test_interrupted_rescue_reconciliation_cannot_be_refunded_by_later_reboot(self):
        self.arm()
        reboot = self.module.request_reboot()
        self.module.close()
        self.clock.new_boot()
        self.module = self.make_module()
        self.module.crash = CrashInjector("boot_after_clear_evidence")
        rescue_boot = self.clock.boot_id
        with self.assertRaises(SimulatedCrash):
            self.module.boot(Evidence("rtc_wake", True, reboot["arm_id"], True, False, True))
        result = self.new_boot(Evidence("normal_reboot", True, reboot["arm_id"], True, False, False))
        self.assertTrue(result["success"])
        state = self.module.ledger.load().state
        self.assertEqual(state["budget"]["used"], 1)
        self.assertEqual(state["budget"]["reserved"], 0)
        self.assertEqual(result["conclusion"], "REBOOT_OUTCOME_UNKNOWN")
        self.assertEqual(state["pending_txs"], {})
        self.assertEqual(state["consumed_arms"][reboot["arm_id"]], self.clock.boot_id)
        observations = result["prior_boot_observations"]
        self.assertEqual(next(iter(observations.values()))["conclusion"], "REBOOT_RESCUED")
        self.assertNotIn(rescue_boot, state["reconciled_boot_ids"])
        self.assertTrue(self.module.boot()["repeated"])
        self.assertEqual(self.module.ledger.load().state["budget"]["used"], 1)

    def test_prepared_crash_reservation_survives_unknown_and_exhausts(self):
        self.module.crash = CrashInjector("after_prepared")
        with self.assertRaises(SimulatedCrash):
            self.module.tick()
        self.assertEqual(self.module.ledger.load().state["budget"]["reserved"], 1)
        result = self.new_boot()
        self.assertTrue(result["success"])
        budget = self.module.ledger.load().state["budget"]
        self.assertEqual((budget["reserved"], budget["used"]), (0, 1))
        self.assertFalse(self.module.enable()["success"])

    def test_verified_normal_reboot_releases_reservation(self):
        self.arm()
        result = self.module.request_reboot()
        result = self.new_boot(Evidence("normal_reboot", True, result["arm_id"], True, False, False))
        self.assertEqual(result["conclusion"], "REBOOT_OK")
        self.assertEqual(self.module.ledger.load().state["budget"]["used"], 0)
        self.assertTrue(self.module.enable()["success"])

    def test_stable_reset_requires_all_health_conditions(self):
        self.arm()
        self.new_boot()
        self.clock.advance(10000)
        self.assertFalse(self.module.observe_health()["reset"])
        self.module.boot_completed = True
        self.module.rescuex_healthy = True
        self.assertFalse(self.module.observe_health()["reset"])
        self.clock.advance(1799)
        self.assertFalse(self.module.observe_health()["reset"])
        self.clock.advance(1)
        self.assertTrue(self.module.observe_health()["reset"])
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.enable()["success"])

    def test_health_failure_and_process_rebuild_restart_timers(self):
        self.arm()
        self.new_boot()
        self.module.boot_completed = True
        self.module.rescuex_healthy = True
        self.module.observe_health()
        self.clock.advance(1790)
        self.sensors.temp = 45
        self.module.observe_health()
        self.sensors.temp = 25
        self.module.observe_health()
        self.clock.advance(1790)
        self.module.close()
        self.module = self.make_module()
        self.module.boot_completed = True
        self.module.rescuex_healthy = True
        self.module.boot()
        self.module.observe_health()
        self.clock.advance(10)
        self.assertFalse(self.module.observe_health()["reset"])
        self.clock.advance(1790)
        self.assertTrue(self.module.observe_health()["reset"])

    def test_boot_change_gets_no_cooldown_credit(self):
        self.arm()
        self.new_boot()
        self.module.boot_completed = True
        self.module.rescuex_healthy = True
        self.module.observe_health()
        self.clock.advance(1799)
        self.new_boot()
        self.module.boot_completed = True
        self.module.rescuex_healthy = True
        self.module.observe_health()
        self.clock.advance(1)
        self.assertFalse(self.module.observe_health()["reset"])
        self.clock.advance(1799)
        self.assertTrue(self.module.observe_health()["reset"])
