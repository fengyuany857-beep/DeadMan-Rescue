from deadman_sim.ledger import State
from deadman_sim.shutdown import parse_powerctl
from support import ModuleCase


class ShutdownTests(ModuleCase):
    def test_off_commits_before_action(self):
        self.arm()
        result = self.module.request_off()
        self.assertTrue(result["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertEqual(self.module.state, State.STAND_DOWN)
        self.assertEqual(self.module.executed_actions[0]["action"], "shutdown")
        intent = self.module.ledger.load().state["intents"][result["tx_id"]]
        self.assertEqual(intent["record"]["phase"], "COMMITTED")

    def test_reboot_resigns_and_trap_merges(self):
        self.arm()
        old = self.module.ledger.load().state["armed_arm_id"]
        result = self.module.request_reboot()
        self.assertTrue(result["success"])
        self.assertNotEqual(result["arm_id"], old)
        self.assertEqual(self.module.rtc.read_alarm() - self.module.rtc.read_counter(), 240)
        before = self.module.ledger.load().seq
        self.assertTrue(self.module.sigterm_trap("reboot")["merged"])
        self.assertEqual(self.module.ledger.load().seq, before)
        self.assertEqual(len(self.module.executed_actions), 1)
        self.assertFalse(self.module.tick()["success"])

    def test_maintenance_never_resigns(self):
        self.arm()
        self.assertTrue(self.module.request_maintenance("recovery")["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertEqual(self.module.executed_actions[-1]["action"], "reboot,recovery")

    def test_clear_failure_does_not_execute(self):
        self.arm()
        self.module.rtc.clear_no_effect = True
        self.assertFalse(self.module.request_off()["success"])
        self.assertEqual(self.module.state, State.FAULT)
        self.assertEqual(self.module.executed_actions, [])
        self.assertIsNotNone(self.module.rtc.read_alarm())

    def test_parse_powerctl(self):
        for value, action in (("shutdown", "off"), ("shutdown,userrequested", "off"),
                              ("reboot", "reboot"), ("reboot,shell", "reboot"),
                              ("reboot,recovery", "maintenance"),
                              ("reboot,bootloader", "maintenance"),
                              ("reboot,unlisted", "maintenance"),
                              ("shutdown,a,b", "maintenance"),
                              ("x" * 129, "maintenance"), ("", "stop")):
            with self.subTest(value=value):
                self.assertEqual(parse_powerctl(value)["action"], action)

    def test_lost_and_delivered_trap(self):
        alarm = self.arm()
        self.assertTrue(self.module.sigterm_trap("shutdown", delivered=False)["skipped"])
        self.assertEqual(self.module.rtc.read_alarm(), alarm)
        self.assertTrue(self.module.sigterm_trap("shutdown,reason")["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertEqual(self.module.executed_actions, [])

    def test_empty_trap_is_stop_not_intent(self):
        self.arm()
        self.module.sigterm_trap("")
        self.assertEqual(self.module.ledger.load().state["intents"], {})
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertEqual(self.module.state, State.STAND_DOWN)

    def test_cancel_requires_confirmation_and_explicit_resume(self):
        self.arm()
        result = self.module.request_reboot()
        self.assertEqual(self.module.cancel_intent()["error"], "INTENT_WAIT")
        self.assertFalse(self.module.resume()["success"])
        self.assertTrue(self.module.cancel_intent(result["tx_id"], confirmed_failure=True)["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.resume()["success"])
        self.arm()

    def test_unknown_and_malformed_trap_are_safe_maintenance(self):
        self.arm()
        result = self.module.sigterm_trap("reboot,unlisted")
        self.assertTrue(result["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertEqual(self.module.executed_actions, [])
        self.assertEqual(self.module.ledger.load().state["intents"], {})

    def test_maintenance_trap_strengthens_registered_reboot(self):
        self.arm()
        result = self.module.request_reboot()
        self.assertTrue(result["success"])
        self.assertTrue(self.module.sigterm_trap("reboot,recovery")["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        intent = self.module.ledger.load().state["intents"][result["tx_id"]]
        self.assertTrue(intent["cancelled"])

    def test_trap_does_not_weaken_fault(self):
        self.arm()
        self.module.rtc.clear_no_effect = True
        self.assertFalse(self.module.request_off()["success"])
        self.assertFalse(self.module.sigterm_trap("reboot")["success"])
        self.assertEqual(self.module.state, State.FAULT)
