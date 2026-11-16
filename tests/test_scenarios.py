from deadman_sim.classify import Evidence
from deadman_sim.ledger import State
from support import ModuleCase


class ScenarioTests(ModuleCase):
    def test_A_normal_reboot(self):
        self.arm()
        reboot = self.module.request_reboot()
        self.clock.advance(90)
        evidence = Evidence("normal_reboot", True, reboot["arm_id"], True, False, False)
        result = self.new_boot(evidence)
        self.assertEqual(result["conclusion"], "REBOOT_OK")
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertTrue(self.module.enable()["success"])
        self.arm()

    def test_B_reboot_falls_into_charging_layer(self):
        self.arm()
        reboot = self.module.request_reboot()
        self.clock.advance(240)
        self.assertTrue(self.module.rtc.fired)
        evidence = Evidence("rtc_wake", True, reboot["arm_id"], True, False, True)
        result = self.new_boot(evidence)
        self.assertEqual(result["conclusion"], "REBOOT_RESCUED")
        self.assertFalse(self.module.enable()["success"])

    def test_C_intentional_off_then_manual_boot(self):
        self.arm()
        off = self.module.request_off()
        self.clock.advance(3600)
        self.assertFalse(self.module.rtc.fired)
        result = self.new_boot(Evidence("manual_power_key", True, off["arm_id"]))
        self.assertEqual(result["conclusion"], "OFF_MANUAL")
        self.assertEqual(len(self.module.executed_actions), 1)

    def test_D_unregistered_disappearance(self):
        self.arm()
        self.module.run_for(180)
        arm = self.module.ledger.load().state["armed_arm_id"]
        self.clock.advance(600)
        self.assertTrue(self.module.rtc.fired)
        result = self.new_boot(Evidence("rtc_wake", True, arm, True, False, True))
        self.assertEqual(result["conclusion"], "RESCUED_ABNORMAL")
        self.assertFalse(self.module.enable()["success"])

    def test_E_failed_off_unknown_keeps_system_on(self):
        self.arm()
        self.module.rtc.clear_no_effect = True
        self.assertFalse(self.module.request_off()["success"])
        self.assertEqual(self.module.executed_actions, [])
        self.clock.advance(600)
        self.assertTrue(self.module.rtc.fired)
        result = self.new_boot()
        self.assertEqual(result["conclusion"], "OFF_OUTCOME_UNKNOWN")
        self.assertEqual(self.module.executed_actions, [])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertEqual(self.module.state, State.STAND_DOWN)

    def test_F_cancel_requires_explicit_resume(self):
        self.arm()
        reboot = self.module.request_reboot()
        self.assertEqual(self.module.cancel_intent()["error"], "INTENT_WAIT")
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.cancel_intent(reboot["tx_id"], confirmed_failure=True)["success"])
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.resume()["success"])
        self.arm()
