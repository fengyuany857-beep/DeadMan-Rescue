import tempfile

from deadman_sim.classify import Evidence
from deadman_sim.crash import CrashInjector, SimulatedCrash
from deadman_sim.ledger import HISTORY_LIMIT, LoadStatus, State, canonical, retired_boot
from deadman_sim.clock import SimClock
from deadman_sim.system import Module
from support import ModuleCase


class ReconcileTests(ModuleCase):
    def test_off_without_prior_arm_manual_boot(self):
        off = self.module.request_off()
        self.assertTrue(off["success"])
        self.assertIsNone(off["arm_id"])
        result = self.new_boot(Evidence("manual_power_key", True))
        self.assertEqual(result["conclusion"], "OFF_MANUAL")
        self.assertEqual(self.module.ledger.load().state["budget"]["used"], 0)

    def test_abnormal_boot_idempotent_and_no_second_charge(self):
        self.arm()
        arm = self.module.ledger.load().state["armed_arm_id"]
        self.clock.advance(600)
        self.assertTrue(self.module.rtc.fired)
        result = self.new_boot(Evidence("rtc_wake", True, arm, True, False, True))
        self.assertEqual(result["conclusion"], "RESCUED_ABNORMAL")
        state = self.module.ledger.load().state
        self.assertEqual(state["consumed_arms"][arm], self.clock.boot_id)
        self.assertEqual(state["budget"]["used"], 1)
        seq = self.module.ledger.load().seq
        self.assertTrue(self.module.boot()["repeated"])
        self.assertEqual(self.module.ledger.load().seq, seq)
        rebuilt = self.make_module()
        self.assertTrue(rebuilt.boot()["repeated"])
        self.assertEqual(rebuilt.ledger.load().state["budget"]["used"], 1)

    def test_crash_each_boot_boundary(self):
        for point in ("boot_before_observation", "boot_after_observation", "boot_before_clear",
                      "boot_after_clear", "boot_after_clear_evidence", "boot_before_consumption",
                      "boot_after_consumption", "boot_after_cache"):
            with self.subTest(point=point), tempfile.TemporaryDirectory(dir=self.root) as root:
                clock = SimClock()
                module = Module(root, self.root / (point + "-hardware"), clock)
                self.addCleanup(module.close)
                module.install()
                module.enable()
                module.tick()
                arm = module.ledger.load().state["armed_arm_id"]
                module.close()
                clock.new_boot()
                module = Module(root, self.root / (point + "-hardware"), clock,
                                crash=CrashInjector(point))
                self.addCleanup(module.close)
                with self.assertRaises(SimulatedCrash):
                    module.boot(Evidence(raw="unmapped raw bootreason"))
                if point == "boot_after_clear_evidence":
                    pending = next(iter(module.ledger.load().state["pending_txs"].values()))
                    self.assertIsNone(pending["readback"])
                rebuilt = Module(root, self.root / (point + "-hardware"), clock)
                self.addCleanup(rebuilt.close)
                result = rebuilt.recover() if point == "boot_after_clear_evidence" else rebuilt.boot()
                self.assertTrue(result["success"])
                state = rebuilt.ledger.load().state
                self.assertEqual(state["consumed_arms"][arm], clock.boot_id)
                self.assertEqual(state["reconciled_boot_ids"].count(clock.boot_id), 1)
                self.assertEqual(state["budget"]["used"], 1)
                self.assertIsNone(rebuilt.rtc.read_alarm())
                self.assertEqual(rebuilt.executed_actions, [])
                if point != "boot_before_observation":
                    self.assertEqual(state["boots"][clock.boot_id]["evidence"]["raw"],
                                     "unmapped raw bootreason")

    def test_cross_boot_uncommitted_reboot_is_unknown_and_cleared(self):
        self.arm()
        self.module.crash = CrashInjector("after_write")
        with self.assertRaises(SimulatedCrash):
            self.module.request_reboot()
        result = self.new_boot()
        self.assertTrue(result["success"])
        self.assertEqual(result["conclusion"], "REBOOT_OUTCOME_UNKNOWN")
        self.assertIsNone(self.module.rtc.read_alarm())

    def test_conflict_immediately_before_clear_not_overwritten(self):
        self.arm()
        self.module.close()
        self.clock.new_boot()
        self.module = self.make_module()
        original = self.module.crash.hit
        def hit(point):
            if point == "boot_before_clear":
                self.module.rtc.external_write(99999)
            original(point)
        self.module.crash.hit = hit
        self.assertFalse(self.module.boot()["success"])
        self.assertEqual(self.module.rtc.read_alarm(), 99999)

    def test_clear_failure_keeps_pending_consumption(self):
        self.arm()
        self.module.close()
        self.clock.new_boot()
        self.module = self.make_module()
        self.module.rtc.clear_no_effect = True
        self.assertFalse(self.module.boot()["success"])
        state = self.module.ledger.load().state
        self.assertEqual(self.module.state, State.FAULT)
        self.assertTrue(state["pending_txs"])
        self.assertNotIn(self.clock.boot_id, state["reconciled_boot_ids"])
        self.assertFalse(self.module.enable()["success"])

    def test_foreign_boot_slot_untouched(self):
        self.arm()
        self.module.rtc.external_write(99999)
        result = self.new_boot()
        self.assertFalse(result["success"])
        self.assertEqual(self.module.state, State.QUARANTINE)
        self.assertEqual(self.module.rtc.read_alarm(), 99999)

    def test_current_consumed_and_cancelled_intents_not_reused(self):
        self.arm()
        off = self.module.request_off()
        self.assertEqual(self.module.boot()["error"], "current boot intent")
        arm = self.module.ledger.load().state["last_protection_arm"]
        first = self.new_boot(Evidence("manual_power_key", True, arm))
        self.assertEqual(first["conclusion"], "OFF_MANUAL")
        second = self.new_boot()
        self.assertIsNone(second["conclusion"])
        self.assertTrue(self.module.ledger.load().state["intents"][off["tx_id"]]["consumed"])

    def test_cancelled_intent_not_classified_as_off(self):
        self.arm()
        self.module.request_off()
        self.assertTrue(self.module.cancel_intent(confirmed_failure=True)["success"])
        self.assertIsNone(self.new_boot()["conclusion"])

    def test_mismatched_intent_quarantined(self):
        self.arm()
        with self.module.lock:
            self.module.ledger.append(type="INTENT", intent="off", arm_id="wrong",
                                      source_boot_id=self.clock.boot_id)
        result = self.new_boot()
        self.assertFalse(result["success"])
        self.assertEqual(self.module.state, State.QUARANTINE)

    def test_enospc_observation_never_clears_or_succeeds(self):
        alarm = self.arm()
        self.module.close()
        self.clock.new_boot()
        self.module = self.make_module()
        self.module.storage.fail_next("ENOSPC")
        self.assertFalse(self.module.boot()["success"])
        self.assertEqual(self.module.rtc.read_alarm(), alarm)
        self.assertTrue(self.module.runtime_fault)

    def test_enospc_consumption_preserves_pending_and_reservation(self):
        self.arm()
        self.module.close()
        self.clock.new_boot()
        self.module = self.make_module()
        original = self.module.crash.hit
        def hit(point):
            if point == "boot_before_consumption":
                self.module.storage.fail_next("ENOSPC")
            original(point)
        self.module.crash.hit = hit
        self.assertFalse(self.module.boot()["success"])
        state = self.module.ledger.load().state
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertNotIn(self.clock.boot_id, state["reconciled_boot_ids"])
        self.assertTrue(state["pending_txs"])
        self.assertEqual(state["budget"]["reserved"], 1)

    def test_integrity_recovery_crash_does_not_remove_rollback_gate(self):
        self.arm()
        generation = self.module.ledger.load().state["instance_generation"]
        self.module.storage.atomic_write("state", canonical(dict(seq=99999, instance_generation=generation)))
        self.module.crash = CrashInjector("snapshot_written")
        self.module.ledger.crash = self.module.crash
        with self.assertRaises(SimulatedCrash):
            self.module.trusted_recover()
        self.module = self.make_module()
        self.assertFalse(self.module.enable()["success"])
        self.assertFalse(self.module.boot()["success"])
        self.assertTrue(self.module.trusted_recover()["success"])
        self.assertFalse(self.module.tick()["success"])

    def test_cache_rollback_requires_new_generation_and_explicit_enable(self):
        self.arm()
        old = self.module.ledger.load().state["instance_generation"]
        self.module.storage.atomic_write("state", canonical(dict(seq=99999, instance_generation=old)))
        self.assertFalse(self.module.boot()["success"])
        self.assertEqual(self.module.state, State.UNKNOWN)
        result = self.module.trusted_recover()
        self.assertTrue(result["success"])
        self.assertNotEqual(result["instance_generation"], old)
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertFalse(self.module.tick()["success"])
        self.assertTrue(self.module.enable()["success"])

    def test_trusted_recovery_clears_and_isolates_uncommitted_owned_arm(self):
        self.module.crash = CrashInjector("after_write")
        with self.assertRaises(SimulatedCrash):
            self.module.tick()
        self.module = self.make_module()
        state = self.module.ledger.load().state
        generation = state["instance_generation"]
        pending = dict(state["pending_txs"])
        self.module.storage.atomic_write("state", canonical(dict(seq=99999, instance_generation=generation)))
        self.assertTrue(self.module.trusted_recover()["success"])
        state = self.module.ledger.load().state
        self.assertEqual(state["pending_txs"], {})
        self.assertEqual(state["quarantined_transactions"], pending)
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertFalse(self.module.tick()["success"])

    def test_sensor_stand_down_survives_trusted_recovery_without_restarting_observation(self):
        self.arm()
        self.sensors.battery = 19
        self.assertFalse(self.module.tick()["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertTrue(self.module.ledger.load().state["control"]["sensor_stand_down"])
        self.sensors.battery = 80
        self.assertEqual(self.module.enable(), dict(success=False, error="stable observation required"))
        self.assertTrue(self.module.boot()["success"])
        self.module.boot_completed = True
        self.module.rescuex_healthy = True
        self.clock.advance(100)
        self.assertEqual(self.module.enable(), dict(success=False, error="stable observation required"))
        loaded = self.module.ledger.load()
        budget = loaded.state["budget"]
        self.assertEqual(budget["stable_since"], self.clock.monotonic())
        self.assertEqual(budget["cooldown_since"], self.clock.monotonic())
        self.clock.advance(600)
        self.module.storage.atomic_write("state", canonical(dict(
            seq=loaded.seq + 1, instance_generation=loaded.state["instance_generation"])))
        self.assertEqual(self.module.ledger.load().status, LoadStatus.CACHE_AHEAD)
        self.assertTrue(self.module.trusted_recover()["success"])
        self.assertEqual(self.module.enable(), dict(success=False, error="stable observation required"))
        recovered = self.module.ledger.load().state
        self.assertEqual(recovered["control"]["sensor_stand_down"],
                         loaded.state["control"]["sensor_stand_down"])
        self.assertFalse(recovered["control"]["enabled"])
        self.assertFalse(recovered["control"]["supervisor_permit"])
        self.assertEqual(recovered["budget"], budget)
        self.assertIsNone(self.module.rtc.read_alarm())
        self.clock.advance(1199)
        self.assertEqual(self.module.enable(), dict(success=False, error="stable observation required"))
        self.clock.advance(1)
        self.assertTrue(self.module.enable()["success"])
        self.assertFalse(self.module.ledger.load().state["control"]["sensor_stand_down"])

    def test_fault_latch_survives_trusted_recovery_until_explicit_repair(self):
        self.arm()
        self.module.rtc.fail_writes = 3
        for expected in (1, 2, 3):
            self.assertFalse(self.module.tick()["success"])
            self.assertEqual(self.module.ledger.load().state["write_fail_count"], expected)
        loaded = self.module.ledger.load()
        self.assertEqual(self.module.state, State.FAULT)
        self.assertTrue(loaded.state["control"]["fault_latched"])
        self.assertFalse(self.module.enable()["success"])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.module.storage.atomic_write("state", canonical(dict(
            seq=loaded.seq + 1, instance_generation=loaded.state["instance_generation"])))
        self.assertEqual(self.module.ledger.load().status, LoadStatus.CACHE_AHEAD)
        self.assertTrue(self.module.trusted_recover()["success"])
        self.assertEqual(self.module.enable(), dict(success=False, error="trusted fault repair required"))
        recovered = self.module.ledger.load().state
        self.assertEqual(recovered["control"]["fault_latched"], loaded.state["control"]["fault_latched"])
        self.assertFalse(recovered["control"]["enabled"])
        self.assertFalse(recovered["control"]["supervisor_permit"])
        self.assertEqual(recovered["write_fail_count"], loaded.state["write_fail_count"])
        self.assertFalse(self.module.runtime_fault)
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertTrue(self.module.repair_fault()["success"])
        self.assertFalse(self.module.ledger.load().state["control"]["fault_latched"])
        self.assertTrue(self.module.enable()["success"])

    def test_trusted_recovery_preserves_compressed_retired_boot_replay_guard(self):
        self.arm()
        boot_ids = []
        for _ in range(70):
            self.assertTrue(self.new_boot()["success"])
            boot_ids.append(self.clock.boot_id)
        with self.module.lock:
            # compact() runs compress_completed() and persists its tombstones.
            self.module.ledger.compact()
        loaded = self.module.ledger.load()
        state = loaded.state
        self.assertEqual(state["pending_txs"], {})
        self.assertEqual(state["reconciled_boot_ids"], boot_ids[-HISTORY_LIMIT:])
        self.assertNotIn(boot_ids[0], state["boots"])
        self.assertTrue(retired_boot(state, boot_ids[0]))
        tombstones = state["control"]["retired_boots"]
        generation = state["instance_generation"]
        self.module.storage.atomic_write("state", canonical(dict(
            seq=loaded.seq + 1, instance_generation=generation)))
        self.assertEqual(self.module.ledger.load().status, LoadStatus.CACHE_AHEAD)
        self.assertFalse(self.module.boot()["success"])
        self.assertTrue(self.module.trusted_recover()["success"])
        recovered = self.module.ledger.load()
        self.assertNotEqual(recovered.state["instance_generation"], generation)
        self.assertFalse(recovered.state["control"]["enabled"])
        self.assertFalse(recovered.state["control"]["supervisor_permit"])

        self.module.close()
        self.clock.boot_id = boot_ids[0]
        self.module = self.make_module()
        before = self.module.ledger.load()
        result = self.module.boot()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "retired/ambiguous boot identity")
        after = self.module.ledger.load()
        self.assertEqual(after.seq, before.seq)
        self.assertEqual(after.records, before.records)
        self.assertEqual(after.state, before.state)
        self.assertEqual(after.state["budget"], before.state["budget"])
        self.assertEqual(after.state["consumed_arms"], before.state["consumed_arms"])
        self.assertEqual(after.state["control"]["retired_boots"], tombstones)
        self.assertTrue(retired_boot(after.state, boot_ids[0]))
        self.assertEqual(self.module.executed_actions, [])

        for boot_id in boot_ids[-HISTORY_LIMIT:]:
            with self.subTest(recent_boot=boot_id):
                self.module.close()
                self.clock.boot_id = boot_id
                self.module = self.make_module()
                result = self.module.boot()
                self.assertTrue(result["success"])
                self.assertTrue(result["repeated"])
                self.assertEqual(result["conclusion"], before.state["boots"][boot_id]["conclusion"])
                self.assertEqual(self.module.ledger.load().seq, before.seq)
                self.assertEqual(self.module.ledger.load().state, before.state)

        result = self.new_boot()
        self.assertNotIn(self.clock.boot_id, boot_ids)
        self.assertTrue(result["success"])
        self.assertFalse(result.get("repeated", False))
        accepted = self.module.ledger.load()
        self.assertGreater(accepted.seq, before.seq)
        self.assertIn(self.clock.boot_id, accepted.state["reconciled_boot_ids"])
        self.assertTrue(any(record["type"] == "BOOT" and record["phase"] == "COMMITTED" and
                            record["source_boot_id"] == self.clock.boot_id
                            for record in accepted.records))
