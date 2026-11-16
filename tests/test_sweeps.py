import ast
from collections import Counter
import copy
from pathlib import Path
import shutil
import tempfile
import unittest

from deadman_sim.classify import Evidence
from deadman_sim.clock import SimClock
from deadman_sim.crash import SimulatedCrash
from deadman_sim.ledger import LoadStatus, State, canonical
from deadman_sim.system import Module


def defined_crash_points():
    trees = [ast.parse(path.read_text()) for path in
             (Path(__file__).parents[1] / "deadman_sim").glob("*.py")]
    phases = {node.args[0].value.lower() for tree in trees for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
              node.func.attr == "persist" and node.args and isinstance(node.args[0], ast.Constant)}

    def expand(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name) and node.id == "name":
            return {"{name}"}
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
                isinstance(node.func.value, ast.Name) and node.func.value.id == "phase" and
                node.func.attr == "lower"):
            return phases
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return {left + right for left in expand(node.left) for right in expand(node.right)}
        raise AssertionError("Unenumerated crash expression: " + ast.dump(node))

    return {point for tree in trees for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
            node.func.attr == "hit" for point in expand(node.args[0])}


class SweepInjector:
    def __init__(self, target=None):
        self.target = target
        self.seen = set()

    def hit(self, point):
        normalized = "{name}:" + point.split(":", 1)[1] if ":" in point else point
        self.seen.add(normalized)
        if normalized == self.target:
            self.target = None
            raise SimulatedCrash(point)


class SweepTests(unittest.TestCase):
    entries = ("renew", "off", "reboot", "maintenance", "disarm", "boot", "reoff",
               "renew_fault", "off_fault", "reboot_fault", "maintenance_fault", "disarm_fault",
               "boot_fault", "compact", "bad_tail")

    def fixture(self, root, entry):
        operation = entry.removesuffix("_fault")
        clock = SimClock()
        module = Module(root, Path(root) / "hardware", clock, config={"reoff_enabled": entry == "reoff"})
        self.assertTrue(module.install()["success"])
        self.assertTrue(module.enable()["success"])
        self.assertTrue(module.tick()["success"])
        clock.advance(60)
        evidence = None
        if operation in ("boot", "reoff"):
            if entry == "reoff":
                off = module.request_off()
                evidence = Evidence("rtc_wake", True, off["arm_id"], True, False, True)
            module.close()
            clock.new_boot()
            module = Module(root, Path(root) / "hardware", clock,
                            config={"reoff_enabled": entry == "reoff"})
        if entry == "renew_fault":
            module.rtc.fail_writes = 1
        if entry.endswith("_fault") and entry != "renew_fault":
            module.rtc.fail_clears = 1
        if entry == "bad_tail":
            module.storage.append_record(module.ledger.load().head["tail"], b'{"broken"')
        return module, evidence

    def invoke(self, module, entry, evidence):
        entry = entry.removesuffix("_fault")
        if entry in ("renew", "renew_fault"):
            return module.tick()
        if entry in ("off", "off_fault"):
            return module.request_off()
        if entry == "reboot":
            return module.request_reboot()
        if entry == "maintenance":
            return module.request_maintenance("recovery")
        if entry == "disarm":
            return module.disarm()
        if entry in ("boot", "reoff"):
            return module.boot(evidence)
        def ledger_action():
            if entry == "compact":
                module.ledger.compact()
            else:
                module.ledger.repair_bad_tail()
            return {"success": True}
        return module._entry(ledger_action, recover=False)

    def attach(self, module, injector):
        module.crash = module.ledger.crash = module.storage.crash = module.rtc.storage.crash = injector

    def rebuild(self, module, next_boot=False):
        module.close()
        if next_boot:
            module.clock.new_boot()
        return Module(module.root, module.rtc.storage.root, module.clock, config=module.config)

    def clone_fixture(self, source, root):
        shutil.copytree(source.root, root, dirs_exist_ok=True)
        module = Module(root, Path(root) / "hardware", copy.deepcopy(source.clock), config=source.config)
        module.token, module._recovered = source.token, source._recovered
        if hasattr(source, "expected_stop"):
            module.expected_stop = source.expected_stop
        module.rtc.fail_writes = source.rtc.fail_writes
        module.rtc.fail_clears = source.rtc.fail_clears
        return module

    def recover_case(self, module, entry, evidence, next_boot):
        entry = entry.removesuffix("_fault")
        if entry == "bad_tail" and module.ledger.load().status == LoadStatus.BAD_TAIL:
            with module.lock:
                module.ledger.repair_bad_tail()
        if next_boot or entry in ("boot", "reoff"):
            return module.boot(evidence)
        return module.recover()

    def assert_invariants(self, module, entry, next_boot, actions_before, foreign=None):
        entry = entry.removesuffix("_fault")
        state = module.ledger.load().state
        self.assertLessEqual(state["budget"].get("used", 0), 1)
        boots = state["reconciled_boot_ids"]
        self.assertEqual(len(boots), len(set(boots)))
        self.assertTrue(all(count <= 1 for count in Counter(
            (action["tx_id"], action.get("reoff", False)) for action in module.executed_actions).values()))
        if entry != "reoff":
            self.assertEqual(module.executed_actions, actions_before)
        if foreign is not None:
            self.assertEqual(module.rtc.read_alarm(), foreign)
            self.assertIn(module.state, {State.QUARANTINE, State.UNKNOWN, State.FAULT,
                                         State.DISARMED, State.STAND_DOWN})
        elif next_boot or entry in ("boot", "reoff"):
            self.assertIn(module.state, {State.DISARMED, State.STAND_DOWN, State.QUARANTINE})
            self.assertIsNone(module.rtc.read_alarm())
        elif entry in ("renew", "renew_fault", "compact", "bad_tail"):
            self.assertIn(module.state, {State.ARMED, State.ARM_PENDING, State.FAULT})
        else:
            self.assertIn(module.state, {State.STAND_DOWN, State.DISARMED, State.FAULT})

    def test_every_defined_crash_point_per_reachable_entry(self):
        registry = defined_crash_points()
        covered = set()
        for entry in self.entries:
            with tempfile.TemporaryDirectory() as template_root:
                source, evidence = self.fixture(template_root, entry)
                source.close()
                with tempfile.TemporaryDirectory() as root:
                    module = self.clone_fixture(source, root)
                    try:
                        trace = SweepInjector()
                        self.attach(module, trace)
                        self.invoke(module, entry, evidence)
                        reachable = trace.seen
                    finally:
                        module.close()
                self.assertTrue(reachable <= registry, reachable - registry)
                for point in sorted(reachable):
                    for mode in ("same", "next"):
                        with self.subTest(entry=entry, point=point, mode=mode), tempfile.TemporaryDirectory() as root:
                            module = self.clone_fixture(source, root)
                            try:
                                injector = SweepInjector(point)
                                self.attach(module, injector)
                                with self.assertRaises(SimulatedCrash):
                                    self.invoke(module, entry, evidence)
                                self.assertIsNone(injector.target)
                                covered.add(point)
                                actions = module.executed_actions
                                module = self.rebuild(module, mode == "next")
                                self.recover_case(module, entry, evidence, mode == "next")
                                self.assert_invariants(module, entry, mode == "next", actions)
                                before = module.ledger.load().state
                                self.recover_case(module, entry, evidence, mode == "next")
                                after = module.ledger.load().state
                                if module.boot_id in before["reconciled_boot_ids"]:
                                    self.assertEqual(after["budget"].get("used"), before["budget"].get("used"))
                                    self.assertEqual(after["consumed_arms"], before["consumed_arms"])
                                for arm, boot in before["consumed_arms"].items():
                                    self.assertEqual(after["consumed_arms"][arm], boot)
                                self.assert_invariants(module, entry, mode == "next", actions)
                                foreign = 999999
                                module.rtc.external_write(foreign)
                                module = self.rebuild(module, mode == "next")
                                self.recover_case(module, entry, evidence, mode == "next")
                                self.assert_invariants(module, entry, mode == "next", actions, foreign)
                            finally:
                                module.close()
        self.assertEqual(covered, registry, "Defined crash points not swept: " + repr(registry - covered))

    def test_module_storage_faults_at_each_persist_phase(self):
        for entry in ("renew", "off", "reboot", "disarm", "boot"):
            phases = ("PREPARED", "CLEARED_VERIFIED", "COMMITTED")
            if entry in ("renew", "reboot"):
                phases += ("READBACK_VERIFIED",)
            for phase in phases:
                for fault in ("ENOSPC", "EIO", "fsync", "rename"):
                    with self.subTest(entry=entry, phase=phase, fault=fault), tempfile.TemporaryDirectory() as root:
                        module, evidence = self.fixture(root, entry)
                        try:
                            append = module.ledger.append
                            fired = []
                            def fail_at_phase(**values):
                                if values.get("phase") == phase and not fired:
                                    fired.append(phase)
                                    # Tail append has no rename. Exercise the actual snapshot/HEAD
                                    # rename in compaction at this persistence boundary instead.
                                    module.storage.fail_next(fault)
                                    if fault == "rename":
                                        module.ledger.compact()
                                return append(**values)
                            module.ledger.append = fail_at_phase
                            result = self.invoke(module, entry, evidence)
                            self.assertEqual(fired, [phase])
                            self.assertFalse(result["success"])
                            self.assertTrue(module.runtime_fault)
                            self.assertEqual(module.state, State.FAULT)
                            slot = module.rtc.read_alarm()
                            self.assertEqual(result["slot"], slot)
                            self.assertEqual(result["slot_empty"], slot is None)
                            self.assertFalse(result["slot_unknown"])
                            seq = module.ledger.load().seq
                            self.assertFalse(module.tick()["success"])
                            self.assertEqual(module.ledger.load().seq, seq)
                            self.assertEqual(module.rtc.read_alarm(), slot)
                            self.assertEqual(module.executed_actions, [])
                        finally:
                            module.close()

    def test_storage_fault_with_unreadable_slot_reports_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            module, _ = self.fixture(root, "renew")
            try:
                alarm = module.rtc.read_alarm()
                append = module.ledger.append
                def fail_prepare(**values):
                    if values.get("phase") == "PREPARED":
                        module.rtc.unreadable = True
                        module.storage.fail_next("ENOSPC")
                    return append(**values)
                module.ledger.append = fail_prepare
                result = module.tick()
                self.assertFalse(result["success"])
                self.assertTrue(result["slot_unknown"])
                self.assertIsNone(result["slot_empty"])
                self.assertTrue(module.runtime_fault)
                module.rtc.unreadable = False
                self.assertEqual(module.rtc.read_alarm(), alarm)
            finally:
                module.close()

    def test_module_integrity_failures_preserve_foreign_slot(self):
        for status in (LoadStatus.BAD_TAIL, LoadStatus.MID_CORRUPTION, LoadStatus.SEQ_ROLLBACK,
                       LoadStatus.SNAPSHOT_CORRUPT, LoadStatus.DIR_MISSING, LoadStatus.CACHE_AHEAD,
                       LoadStatus.UNKNOWN_VERSION):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                module, _ = self.fixture(root, "renew")
                try:
                    loaded = module.ledger.load()
                    tail = loaded.head["tail"]
                    if status == LoadStatus.BAD_TAIL:
                        module.storage.append_record(tail, b"broken")
                    elif status == LoadStatus.MID_CORRUPTION:
                        module.storage.append_record(tail, b"broken\n{}\n")
                    elif status in (LoadStatus.SEQ_ROLLBACK, LoadStatus.UNKNOWN_VERSION):
                        record = dict(loaded.records[-1])
                        if status == LoadStatus.UNKNOWN_VERSION:
                            record.update(version=999, seq=loaded.seq + 1)
                        module.storage.append_record(tail, canonical(record) + b"\n")
                    elif status == LoadStatus.SNAPSHOT_CORRUPT:
                        module.storage.atomic_write(loaded.head["snapshot"], b"{}")
                    elif status == LoadStatus.DIR_MISSING:
                        shutil.rmtree(module.storage.root)
                    else:
                        module.storage.atomic_write("state", canonical(dict(seq=loaded.seq + 100,
                            instance_generation=loaded.state["instance_generation"])))
                    module.rtc.external_write(999999)
                    module = self.rebuild(module, True)
                    result = module.boot()
                    self.assertFalse(result["success"])
                    self.assertEqual(result["status"], status.value)
                    self.assertEqual(module.state, State.UNKNOWN)
                    self.assertFalse(module.tick()["success"])
                    self.assertEqual(module.rtc.read_alarm(), 999999)
                    self.assertEqual(module.executed_actions, [])
                    if status == LoadStatus.DIR_MISSING:
                        self.assertFalse(module.storage.root.exists())
                finally:
                    module.close()

    def test_old_instance_intent_is_quarantined_without_consumption(self):
        with tempfile.TemporaryDirectory() as root:
            module, _ = self.fixture(root, "off")
            try:
                off = module.request_off()
                loaded = module.ledger.load()
                state = dict(loaded.state, instance_generation="replacement-instance")
                with module.lock:
                    module.ledger._switch(state, loaded.seq, loaded.checksum, loaded.generation + 1)
                    module.storage.delete("state")
                module = self.rebuild(module, True)
                result = module.boot(Evidence("manual_power_key", True, off["arm_id"]))
                self.assertFalse(result["success"])
                state = module.ledger.load().state
                self.assertEqual(module.state, State.QUARANTINE)
                self.assertIn(off["tx_id"], state["quarantined_intents"])
                self.assertFalse(state["intents"][off["tx_id"]]["consumed"])
                self.assertEqual(state["consumed_arms"], {})
                self.assertEqual(state["reconciled_boot_ids"], [])
                self.assertFalse(module.tick()["success"])
                self.assertEqual(len(module.executed_actions), 1)
            finally:
                module.close()

    def test_maintenance_after_prepared_recovers_without_action(self):
        with tempfile.TemporaryDirectory() as root:
            module, _ = self.fixture(root, "maintenance")
            try:
                self.attach(module, SweepInjector("after_prepared"))
                with self.assertRaises(SimulatedCrash):
                    module.request_maintenance("recovery")
                module = self.rebuild(module)
                self.assertTrue(module.recover()["success"])
                self.assertEqual(module.state, State.STAND_DOWN)
                self.assertIsNone(module.rtc.read_alarm())
                self.assertEqual(module.executed_actions, [])
                self.assertFalse(module.tick()["success"])
            finally:
                module.close()
