from deadman_sim.ledger import State
from support import ModuleCase


class DaemonTests(ModuleCase):
    def test_stop_generation_upgrade_blocks_old_instance(self):
        self.arm()
        old_stop = self.module.expected_stop
        controller = self.make_module()
        self.assertTrue(controller.disarm()["success"])
        self.assertGreater(controller.ledger.load().state["stop_generation"], old_stop)
        seq = controller.ledger.load().seq
        self.assertFalse(self.module.tick()["success"])
        self.assertFalse(self.module.supervisor_restart()["success"])
        self.assertEqual(controller.ledger.load().seq, seq)
        self.assertIsNone(controller.rtc.read_alarm())

    def test_many_renewals_reuse_budget(self):
        self.arm()
        results = self.module.run_for(3600)
        self.assertEqual(len(results), 60)
        self.assertTrue(all(r["success"] for r in results))
        self.assertEqual(self.module.rtc.read_alarm() - self.module.rtc.read_counter(), 600)
        state = self.module.ledger.load().state
        self.assertEqual(state["budget"]["reserved"], 1)
        self.assertEqual(len(state["superseded_arms"]), 60)

    def test_rtc_jumps_quarantine(self):
        for delta in (500, -500):
            with self.subTest(delta=delta):
                self.arm()
                self.module.rtc.jump(delta)
                self.assertFalse(self.module.tick()["success"])
                self.assertEqual(self.module.state, State.QUARANTINE)
                self.assertIsNone(self.module.rtc.read_alarm())
                self.assertFalse(self.module.tick()["success"])
                self.assertTrue(self.module.repair_fault()["success"])
                self.assertTrue(self.module.enable()["success"])

    def test_foreign_slot_untouched(self):
        self.arm()
        foreign = self.module.rtc.read_counter() + 1200
        self.module.rtc.external_write(foreign)
        self.assertFalse(self.module.tick()["success"])
        self.assertEqual(self.module.state, State.QUARANTINE)
        self.assertEqual(self.module.rtc.read_alarm(), foreign)
        self.module.run_for(180)
        self.assertEqual(self.module.rtc.read_alarm(), foreign)

    def test_write_fail_max_and_supervisor(self):
        self.arm()
        self.module.rtc.fail_writes = 3
        for expected in (1, 2, 3):
            result = self.module.tick()
            self.assertFalse(result["success"])
            self.assertEqual(self.module.ledger.load().state["write_fail_count"], expected)
        self.assertEqual(self.module.state, State.FAULT)
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertFalse(self.module.supervisor_restart()["success"])
        self.assertFalse(self.module.enable()["success"])
        self.assertTrue(self.module.repair_fault()["success"])
        self.assertTrue(self.module.enable()["success"])
        self.arm()
        self.assertEqual(self.module.ledger.load().state["write_fail_count"], 0)

    def test_success_resets_recoverable_failure_count(self):
        self.arm()
        self.module.rtc.fail_writes = 1
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.tick()["success"])
        self.assertEqual(self.module.ledger.load().state["write_fail_count"], 0)

    def test_sensors_stop_and_clear(self):
        for battery, temp in ((None, 25), (19, 25), (80, None), (80, 45)):
            with self.subTest(battery=battery, temp=temp):
                self.arm()
                self.sensors.battery, self.sensors.temp = battery, temp
                self.module.tick()
                self.assertEqual(self.module.state, State.STAND_DOWN)
                self.assertIsNone(self.module.rtc.read_alarm())
                self.assertFalse(self.module.enable()["success"])
                self.sensors.battery, self.sensors.temp = 80, 25
                self.assertTrue(self.module.enable()["success"])

    def test_lock_loser_and_unpermitted_instance(self):
        self.arm()
        second = self.make_module()
        seq = self.module.ledger.load().seq
        with self.module.lock:
            self.assertTrue(second.tick()["skipped"])
        self.assertFalse(second.tick()["success"])
        self.assertEqual(self.module.ledger.load().seq, seq)
        self.assertFalse(second.recover()["success"])
        self.assertTrue(self.module.tick()["success"])
