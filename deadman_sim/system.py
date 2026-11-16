from dataclasses import replace
import json
import math
from pathlib import Path
import uuid

from . import params
from .budget import fresh_budget, reserve
from .crash import CrashInjector, SimulatedCrash
from .engine import HardwareFailure, Transaction
from .ledger import Ledger, LoadStatus, State, canonical, initial_state
from .lock import IdentityChanged, SharedLock
from .rtc import SimRTC
from .reconcile import reconcile
from .sensors import SimSensors
from .shutdown import MAINTENANCE_TARGETS, parse_powerctl
from .storage import Storage


class Module:
    def __init__(self, root, hardware_dir, clock, sensors=None, crash=None, config=None):
        self.root = Path(root).absolute()
        self.clock, self.boot_id = clock, clock.boot_id
        self.sensors = sensors if sensors is not None else SimSensors()
        self.crash = crash or CrashInjector()
        self.config = config or {}
        self.tolerance = self.config.get("rtc_tolerance", params.RTC_TOLERANCE)
        if type(self.tolerance) not in (int, float) or not math.isfinite(self.tolerance) or self.tolerance < 0:
            raise ValueError("negative tolerance")
        self.storage = Storage(self.root / "deadman-rescue")
        self.lock = SharedLock(self.root)
        self.ledger = Ledger(self.storage, self.lock, self.crash)
        self.rtc = SimRTC(hardware_dir, clock)
        self.token = str(uuid.uuid4())
        self.runtime_fault = False
        self._recovered = False
        self.last_error = None
        self.closed = False
        self.crashed = False
        self.boot_completed = False
        # T2/T3 / offline RescueX contract pending: unknown never resets a budget.
        self.rescuex_healthy = self.config.get("rescuex_healthy")
        self.daemon_healthy = True
        self.manual_intervention = False
        self.maintenance = False
        self._healthy_since = None
        self._instance_lock = None

    def _can_claim(self):
        if self._instance_lock is None:
            self._instance_lock = SharedLock(self.root, lock_name=".deadman-rescue.instance.lock")
        if self._instance_lock.held:
            self._instance_lock.require()
            return True
        try:
            self._instance_lock.acquire()
            return True
        except BlockingIOError:
            return False

    def _claim(self):
        self._instance_lock.require()

    def _release_owner(self):
        if self._instance_lock is not None:
            self._instance_lock.release()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._instance_lock is not None:
            self._instance_lock.close()
            self._instance_lock = None
        self.rtc.close()
        self.lock.close()

    @property
    def executed_actions(self):
        store = self.rtc.storage
        if not store.path("actions.jsonl").exists():
            return []
        return [json.loads(line) for line in store.read("actions.jsonl").splitlines()]

    @property
    def state(self):
        if self.runtime_fault:
            return State.FAULT
        loaded = self.ledger.load()
        return State(loaded.state["state"]) if loaded.status == LoadStatus.OK else State.UNKNOWN

    def read_slot(self):
        try:
            return self.rtc.read_alarm()
        except OSError as exc:
            raise HardwareFailure("read", unreadable=True) from exc

    def sample(self):
        before = self.clock.monotonic()
        rtc = self.rtc.read_counter()
        return dict(rtc=rtc, mono=self.clock.monotonic(), mono_before=before,
                    boot_id=self.boot_id)

    def _entry(self, action, *, recover=True, allow_runtime_fault=False):
        if self.closed or self.crashed:
            return dict(success=False, error="process not alive")
        if self.clock.boot_id != self.boot_id:
            return dict(success=False, error="stale boot instance")
        if self.runtime_fault and not allow_runtime_fault:
            return dict(success=False, error="runtime FAULT")
        try:
            with self.lock:
                if recover and not self._recovered:
                    result = self._recover_locked()
                    if self.ledger.load().status != LoadStatus.OK:
                        return result
                if recover and self.ledger.load().status != LoadStatus.OK:
                    slot = self.read_slot()
                    return dict(success=False, status=self.ledger.load().status.value,
                                slot_empty=slot is None, slot=slot)
                return action()
        except BlockingIOError:
            return dict(success=False, skipped=True, error="lock busy")
        except SimulatedCrash:
            self._healthy_since = None
            self.crashed = True
            self._release_owner()
            raise
        except HardwareFailure as exc:
            self._healthy_since = None
            self.runtime_fault = True
            self.last_error = str(exc)
            return dict(success=False, error=str(exc), slot_unknown=exc.unreadable)
        except (OSError, IdentityChanged, ValueError) as exc:
            self._healthy_since = None
            self.runtime_fault = True
            self.last_error = str(exc)
            try:
                slot = self.read_slot()
            except HardwareFailure:
                return dict(success=False, error=str(exc), slot_unknown=True,
                            slot=None, slot_empty=None)
            return dict(success=False, error=str(exc), slot_unknown=False,
                        slot=slot, slot_empty=slot is None)

    def _record(self, *, state=None, control=None, **values):
        loaded = self.ledger.load()
        if loaded.status != LoadStatus.OK:
            raise ValueError(loaded.status.value)
        result = values.pop("result", {})
        if state is not None:
            result["state"] = State(state).value
        if control is not None:
            result["control"] = control
        values.setdefault("stop_generation", loaded.state["stop_generation"])
        values.setdefault("source_boot_id", self.boot_id)
        return self.ledger.append(emergency=True, result=result, **values)

    def install(self):
        def install():
            if self.read_slot() is not None:
                return dict(success=False, error="unknown occupied slot")
            if not self.storage.root.exists():
                self.storage.initialize()
                self.lock.directory = self.lock._dir_identity()
            self.ledger.initialize()
            self._record(state=State.DISARMED, operation="install",
                         control=dict(enabled=False, supervisor_permit=False))
            self._recovered = True
            return dict(success=True)
        return self._entry(install, recover=False)

    def _trusted_time(self, samples):
        now = self.sample()
        return (samples and samples.get("boot_id") == self.boot_id and
                now["mono"] >= samples["mono"] and
                abs((now["rtc"] - samples["rtc"]) -
                    (now["mono"] - samples["mono"])) <= self.tolerance)

    def _known_own(self, slot, loaded=None):
        if slot is None:
            return True
        loaded = loaded or self.ledger.load()
        if loaded.status != LoadStatus.OK:
            return False
        expected = [loaded.state["last_alarm"]]
        for key, item in loaded.state["intents"].items():
            if not item["consumed"] and not item["cancelled"] and key not in loaded.state["quarantined_intents"]:
                result = item["record"].get("result") or {}
                expected.extend([result.get("old_alarm"), result.get("target_alarm")])
        for tx in loaded.state["pending_txs"].values():
            result = tx.get("result") or {}
            expected.extend([result.get("target_alarm"), result.get("old_alarm")])
        return slot in expected

    def _stop(self, state=State.STAND_DOWN, reason="stopped", *, sensor_failure=False):
        current = self.ledger.load().state
        self._release_owner()
        control = dict(enabled=False, supervisor_permit=False, retry=False)
        if sensor_failure:
            self._healthy_since = None
            control["sensor_stand_down"] = True
        if state in (State.FAULT, State.QUARANTINE):
            control["fault_latched"] = True
        return self._record(state=state, operation="stop", error=reason,
                            stop_generation=current["stop_generation"] + 1,
                            control=control)

    def _clear_owned(self, operation="clear", final_state=State.STAND_DOWN):
        slot = self.read_slot()
        if not self._known_own(slot):
            return dict(success=False, error="unknown ownership", slot=slot)
        tx = Transaction(self, operation, final_state=final_state.value)
        try:
            result = tx.run()
            for key in list(self.ledger.load().state["pending_txs"]):
                self._record(operation="abort", state=final_state,
                             result=dict(abort_tx=key))
            return result
        except HardwareFailure as exc:
            return self._failed(tx, exc, recoverable=False)

    def _failed(self, tx, exc, recoverable=False):
        loaded = self.ledger.load()
        try:
            slot = self.read_slot()
            unknown = False
        except HardwareFailure:
            slot, unknown = None, True
        count = loaded.state["write_fail_count"] + 1
        retry = (recoverable and not unknown and slot is None and
                 count < params.WRITE_FAIL_MAX and self.sensors.healthy())
        terminal = State.QUARANTINE if exc.step == "conflict" else State.FAULT
        result = dict(tx.base["result"], state=(State.ARM_PENDING if retry else terminal).value,
                      write_fail_count=count, hardware_state=("unknown" if unknown else
                          "known_empty" if slot is None else "known_value"),
                      observed=slot, control=dict(retry=retry),
                      abort_tx=tx.base["tx_id"] if retry else None)
        tx.persist("FAULT", error=exc.step, readback=slot, result=result)
        if not retry:
            self._stop(terminal, exc.step)
            if not unknown and slot is not None and self._known_own(slot):
                cleanup = Transaction(self, "clear", final_state=State.FAULT.value)
                try:
                    cleanup.run()
                    slot = None
                except HardwareFailure:
                    pass
            self._record(state=terminal, operation="alarm", error=exc.step,
                         result=dict(residue="unknown" if unknown or slot is not None else "disarmed"))
        return dict(success=False, error=exc.step, retry=retry, slot=slot, slot_unknown=unknown)

    def _recover_locked(self, claim=False):
        slot = self.read_slot()
        loaded = self.ledger.load()
        self._recovered = True
        if loaded.status != LoadStatus.OK:
            return dict(success=False, status=loaded.status.value, slot=slot,
                        slot_empty=slot is None, state=State.UNKNOWN.value)
        state = loaded.state
        if (state["control"].get("enabled") and state["control"].get("boot_id") == self.boot_id and
                not self._can_claim()):
            return dict(success=False, skipped=True, error="live daemon owner")
        if state["state"] == State.FAULT.value and state["control"].get("fault_latched"):
            self.ledger.write_cache()
            return dict(success=True, state=State.FAULT.value, slot=slot)
        for record in list(state["pending_txs"].values()):
            if record["type"] == "BOOT":
                if record["source_boot_id"] == self.boot_id:
                    return reconcile(self)
                self._record(state=State.RECONCILING, operation="recovery",
                             control=dict(enabled=False, supervisor_permit=False))
                return dict(success=False, state=State.RECONCILING.value, slot=slot)
            if record["phase"] == "FAULT":
                continue
            if record["source_boot_id"] != self.boot_id:
                self._record(state=State.RECONCILING, operation="recovery",
                             control=dict(enabled=False, supervisor_permit=False))
                return dict(success=False, state=State.RECONCILING.value, slot=slot)
            tx = Transaction.restore(self, record)
            result = record["result"] or {}
            if record["requested"] is None:
                if self._known_own(slot, loaded):
                    try:
                        tx.clear_and_finish()
                    except HardwareFailure as exc:
                        return self._failed(tx, exc)
                else:
                    self._stop(State.QUARANTINE, "foreign slot during recovery")
                continue
            trusted = (self._trusted_time(result.get("samples")) and
                       record["stop_generation"] == state["stop_generation"])
            if slot == result.get("old_alarm") and record["phase"] == "PREPARED":
                self._record(phase="FAULT", state=State.FAULT, operation="abort",
                             error="prepared without hardware change",
                             result=dict(abort_tx=record["tx_id"]),
                             control=dict(enabled=False, supervisor_permit=False, fault_latched=True))
            elif slot == result.get("target_alarm") and slot is not None and trusted and self.sensors.healthy():
                try:
                    tx.persist("READBACK_VERIFIED", readback=slot)
                    tx.commit()
                except HardwareFailure as exc:
                    return self._failed(tx, exc)
            elif slot is None and trusted and self.sensors.healthy():
                if record["type"] == "INTENT":
                    self._stop(State.FAULT, "interrupted reboot has no wakealarm")
                else:
                    self._record(state=State.ARM_PENDING, operation="abort",
                                 result=dict(abort_tx=record["tx_id"]), control=dict(retry=True))
            else:
                final_state = State.QUARANTINE if not trusted else State.UNKNOWN
                self._stop(final_state,
                           "unexplained pending hardware")
                if self._known_own(slot, loaded):
                    self._clear_owned(final_state=final_state)
        loaded = self.ledger.load()
        state = loaded.state
        if not state["pending_txs"] and state["state"] == State.ARM_PENDING.value and slot is not None:
            self._stop(State.QUARANTINE, "foreign slot during pending retry")
        if not state["pending_txs"] and state["state"] == State.ARMED.value:
            if not self._trusted_time(state["last_samples"]):
                if state["last_samples"].get("boot_id") != self.boot_id:
                    self._record(state=State.RECONCILING, operation="recovery",
                                 control=dict(enabled=False, supervisor_permit=False))
                else:
                    self._stop(State.QUARANTINE, "RTC jump")
                    self._clear_owned(final_state=State.QUARANTINE)
            elif slot != state["last_alarm"]:
                self._stop(State.QUARANTINE, "slot conflict")
        state = self.ledger.load().state
        if not state["control"].get("enabled"):
            self._release_owner()
        if claim and state["control"].get("enabled") and state["state"] in (
                State.ARMED.value, State.ARM_PENDING.value, State.DISARMED.value):
            if not self._can_claim():
                return dict(success=False, skipped=True, error="live daemon owner")
            if state["control"].get("fault_latched"):
                return dict(success=False, error="trusted fault repair required")
            self._record(operation="claim", control=dict(owner=self.token))
            self._claim()
            self.expected_stop = state["stop_generation"]
        self.ledger.write_cache()
        return dict(success=True, state=self.ledger.load().state["state"], slot=self.read_slot())

    def recover(self):
        return self._entry(lambda: self._recover_locked(claim=True), recover=False)

    def enable(self):
        def enable():
            loaded = self.ledger.load()
            if loaded.status != LoadStatus.OK:
                return dict(success=False, error=loaded.status.value)
            state = loaded.state
            if state["state"] not in (State.DISARMED.value, State.STAND_DOWN.value):
                return dict(success=False, error="state not eligible")
            if state["control"].get("fault_latched"):
                return dict(success=False, error="trusted fault repair required")
            if any(not item["consumed"] and not item["cancelled"] and
                   key not in state["quarantined_intents"]
                   for key, item in state["intents"].items()):
                return dict(success=False, error="unresolved intent")
            if not self.sensors.healthy():
                self._stop(reason="unsafe sensors", sensor_failure=True)
                return dict(success=False, error="unsafe sensors")
            if state["control"].get("sensor_stand_down"):
                self._observe_health_locked()
                state = self.ledger.load().state
                if state["control"].get("sensor_stand_down"):
                    return dict(success=False, error="stable observation required")
            if self.read_slot() is not None:
                self._stop(State.QUARANTINE, "occupied on enable")
                return dict(success=False, error="occupied slot")
            try:
                budget = reserve(state["budget"], self.boot_id)
            except ValueError:
                return dict(success=False, error="budget exhausted")
            if not self._can_claim():
                return dict(success=False, skipped=True, error="live daemon owner")
            self._record(state=State.DISARMED, operation="enable", budget=budget,
                         control=dict(enabled=True, supervisor_permit=True, owner=self.token,
                                      retry=False, boot_id=self.boot_id))
            self.expected_stop = state["stop_generation"]
            self._claim()
            return dict(success=True)
        return self._entry(enable)

    def resume(self):
        return self.enable()

    def tick(self):
        def tick():
            self._observe_health_locked()
            loaded = self.ledger.load()
            if loaded.status != LoadStatus.OK:
                return dict(success=False, error=loaded.status.value)
            state, control = loaded.state, loaded.state["control"]
            if (not control.get("enabled") or control.get("owner") != self.token or
                    not control.get("supervisor_permit") or control.get("sensor_stand_down") or
                    state["stop_generation"] != getattr(self, "expected_stop", None) or
                    self.boot_id != control.get("boot_id")):
                self._release_owner()
                return dict(success=False, skipped=True, error="no renewal permit")
            if not self._can_claim():
                return dict(success=False, skipped=True, error="live daemon owner")
            if state["state"] not in (State.ARMED.value, State.DISARMED.value,
                                      State.ARM_PENDING.value):
                return dict(success=False, skipped=True, error="state prohibits renewal")
            if not self.sensors.healthy():
                self._stop(reason="unsafe sensors", sensor_failure=True)
                cleanup = self._clear_owned(final_state=State.STAND_DOWN)
                return dict(success=False, stopped=True, error="unsafe sensors", cleanup=cleanup)
            slot = self.read_slot()
            if state["last_samples"] and not self._trusted_time(state["last_samples"]):
                self._stop(State.QUARANTINE, "RTC jump")
                self._clear_owned(final_state=State.QUARANTINE)
                return dict(success=False, error="RTC jump")
            retry = control.get("retry") and state["state"] == State.ARM_PENDING.value
            if (retry and slot is not None) or (not retry and slot != state["last_alarm"]):
                self._stop(State.QUARANTINE, "slot conflict")
                return dict(success=False, error="slot conflict", observed=slot,
                            expected=state["last_alarm"])
            if not state["budget"].get("reserved") or state["budget"].get("session_boot_id") != self.boot_id:
                return dict(success=False, error="no budget reservation")
            tx = Transaction(self, "renew" if state["armed_arm_id"] else "arm",
                             params.WINDOW, final_state=State.ARMED.value)
            try:
                return tx.run()
            except HardwareFailure as exc:
                return self._failed(tx, exc, recoverable=exc.step == "write")
        return self._entry(tick)

    def boot(self, bootreason=None):
        return self._entry(lambda: reconcile(self, bootreason), recover=False)

    def reconcile_boot(self, evidence=None):
        return self.boot(evidence)

    def _observe_health_locked(self):
        state = self.ledger.load().state
        rescuex = self.rescuex_healthy() if callable(self.rescuex_healthy) else self.rescuex_healthy
        healthy = (self.boot_completed is True and rescuex is True and self.daemon_healthy and
                   self.boot_id in state.get("reconciled_boot_ids", []) and
                   self.sensors.healthy() and not self.runtime_fault and
                   not state.get("write_fail_count") and
                   state.get("state") not in (State.FAULT.value, State.QUARANTINE.value,
                                               State.UNKNOWN.value, State.RECONCILING.value) and
                   not state.get("control", {}).get("fault_latched") and
                   self.read_slot() is None)
        if not healthy:
            self._healthy_since = None
            return dict(success=False, reset=False, error="health not verified")
        now = self.clock.monotonic()
        if self._healthy_since is None:
            self._healthy_since = now
            budget = dict(state["budget"], stable_boot_id=self.boot_id,
                          stable_since=now, cooldown_since=now)
            self._record(operation="health_observation", budget=budget)
        if now - self._healthy_since < max(params.STABLE_RUN, params.COOLDOWN):
            return dict(success=True, reset=False)
        if state["budget"].get("used") or state["budget"].get("reserved"):
            budget = fresh_budget()
            budget.update(stable_boot_id=self.boot_id, stable_since=now, cooldown_since=now)
            self._record(operation="budget_reset", budget=budget, state=State.DISARMED,
                         control=dict(enabled=False, supervisor_permit=False, sensor_stand_down=False))
            self._healthy_since = None
            return dict(success=True, reset=True)
        if state["control"].get("sensor_stand_down"):
            self._record(operation="sensor_recovery", control=dict(sensor_stand_down=False))
            self._healthy_since = None
        return dict(success=True, reset=False)

    def observe_health(self):
        return self._entry(self._observe_health_locked)

    def _attempt_reoff(self, candidate):
        state = self.ledger.load().state
        key = candidate["intent_id"]
        if not self.config.get("reoff_enabled", params.REOFF_ENABLED):
            return dict(success=False, skipped=True, error="REOFF disabled")
        if (candidate["boot_id"] != self.boot_id or not candidate["clear_verified"] or
                state["reoff_attempts"].get(key, 0) >= params.REOFF_MAX or
                state["pending_txs"] or state["control"].get("fault_latched") or
                self.manual_intervention or self.maintenance or not self.sensors.healthy() or
                self.read_slot() is not None):
            return dict(success=False, skipped=True, error="REOFF gates")
        self._stop(reason="REOFF")
        self.crash.hit("reoff_before_attempt")
        self._record(type="REOFF_ATTEMPT", operation="reoff", intent=key,
                     state=State.STAND_DOWN, result=dict(consuming_boot_id=self.boot_id))
        self.crash.hit("reoff_after_attempt")
        if self.manual_intervention or self.maintenance or not self.sensors.healthy():
            return dict(success=False, error="REOFF cancelled")
        outcome = self.config.get("reoff_command_result", "success")
        self.crash.hit("reoff_before_action")
        intent = self.ledger.load().state["intents"].get(key)
        if (not intent or intent["cancelled"] or self.manual_intervention or
                self.maintenance or not self.sensors.healthy() or self.read_slot() is not None):
            return dict(success=False, error="REOFF cancelled")
        self.rtc.storage.append_record("actions.jsonl", canonical(dict(
            action="shutdown", tx_id=key, boot_id=self.boot_id, reoff=True, outcome=outcome)) + b"\n")
        self.crash.hit("reoff_after_action")
        if outcome != "success":
            self._stop(State.QUARANTINE, "REOFF request failed/unknown")
            return dict(success=False, error="REOFF request failed/unknown")
        return dict(success=True)

    def trusted_recover(self):
        def recover():
            loaded = self.ledger.load()
            slot = self.read_slot()
            if loaded.status == LoadStatus.BAD_TAIL:
                self.ledger.repair_bad_tail()
                loaded = self.ledger.load()
            if not loaded.state:
                if slot is not None:
                    return dict(success=False, error="unknown ownership")
                if loaded.status not in (LoadStatus.DIR_MISSING, LoadStatus.HEAD_MISSING):
                    return dict(success=False, error="no verified chain for recovery")
                if not self.storage.root.exists():
                    self.storage.initialize()
                    self.lock.directory = self.lock._dir_identity()
                state = initial_state(str(uuid.uuid4()))
                state["budget"] = dict(fresh_budget(), used=params.RTC_RESCUE_MAX)
            else:
                if not self._known_own(slot, replace(loaded, status=LoadStatus.OK)):
                    return dict(success=False, error="unknown ownership")
                state = dict(loaded.state, instance_generation=str(uuid.uuid4()),
                             state=State.STAND_DOWN.value, pending_txs={}, last_alarm=slot,
                             quarantined_transactions=dict(loaded.state["quarantined_transactions"],
                                                           **loaded.state["pending_txs"]),
                             control=dict(enabled=False, supervisor_permit=False),
                             quarantined_intents=dict(loaded.state["quarantined_intents"], **{
                                 key: "old instance generation" for key in loaded.state["intents"]}))
            if self.storage.path("state").exists():
                self.storage.atomic_write("quarantine-cache-" + uuid.uuid4().hex,
                                          self.storage.read("state"))
            generation = loaded.generation + 1
            while self.storage.path(f"snapshot-{generation}.json").exists():
                generation += 1
            self.ledger._switch(state, 0, "", generation)
            if self.storage.path("state").exists():
                self.storage.delete("state")
            self._stop(reason="trusted integrity recovery")
            result = self._clear_owned(operation="disarm", final_state=State.DISARMED)
            if not result["success"]:
                return result
            self.runtime_fault = False
            self._recovered = True
            return dict(success=True, instance_generation=state["instance_generation"])
        return self._entry(recover, recover=False, allow_runtime_fault=True)

    def run_for(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid duration")
        results = []
        remaining = seconds
        while remaining >= params.RENEW:
            self.clock.advance(params.RENEW)
            remaining -= params.RENEW
            results.append(self.tick())
        if remaining:
            self.clock.advance(remaining)
        return results

    def supervisor_restart(self):
        def restart():
            state = self.ledger.load().state
            if state.get("state") not in (State.ARMED.value, State.DISARMED.value):
                return dict(success=False, error="supervisor prohibited")
            if not state["control"].get("supervisor_permit"):
                return dict(success=False, error="supervisor revoked")
            return self._recover_locked(claim=True)
        return self._entry(restart)

    def repair_fault(self):
        def repair():
            if self.ledger.load().status != LoadStatus.OK:
                return dict(success=False, error="untrusted ledger")
            self._stop(reason="trusted repair")
            result = self._clear_owned(operation="disarm", final_state=State.DISARMED)
            if result["success"]:
                self._record(operation="repair", control=dict(fault_latched=False))
                self.runtime_fault = False
            return result
        return self._entry(repair, recover=False, allow_runtime_fault=True)

    def _shutdown(self, kind, target=None, execute=True):
        state = self.ledger.load().state
        if kind == "reboot" and (state["state"] in (
                State.FAULT.value, State.QUARANTINE.value, State.UNKNOWN.value,
                State.RECONCILING.value) or state["control"].get("sensor_stand_down") or
                not self.sensors.healthy()):
            return dict(success=False, error="reboot arming prohibited")
        superseded = []
        for key, item in state["intents"].items():
            record = item["record"]
            if (record["source_boot_id"] == self.boot_id and not item["cancelled"] and
                    not item["consumed"] and record["phase"] == "COMMITTED"):
                if record["intent"] == kind:
                    return dict(success=True, merged=True, tx_id=key)
                if kind == "reboot" or record["intent"] == "off":
                    return dict(success=False, skipped=True, error="stronger registered stop")
                superseded.append(key)
        if kind == "reboot" and (not state["budget"].get("reserved") or
                                  state["budget"].get("session_boot_id") != self.boot_id):
            return dict(success=False, error="no budget reservation")
        if kind == "reboot" and state["last_samples"] and not self._trusted_time(state["last_samples"]):
            self._stop(State.QUARANTINE, "RTC jump")
            self._clear_owned(final_state=State.QUARANTINE)
            return dict(success=False, error="RTC jump")
        self._stop(reason=kind)
        requested = params.REBOOT_WAKE if kind == "reboot" else None
        if not self._known_own(self.read_slot()):
            self._stop(State.QUARANTINE, "foreign shutdown slot")
            return dict(success=False, error="unknown ownership")
        tx = Transaction(self, "rearm" if requested else "off" if kind == "off" else "disarm",
                         requested, kind="INTENT" if kind in ("off", "reboot") else "TRANSACTION",
                         intent=kind if kind in ("off", "reboot") else None, target=target,
                         final_state=State.STAND_DOWN.value)
        try:
            result = tx.run()
        except HardwareFailure as exc:
            return self._failed(tx, exc)
        for key in superseded:
            self._record(type="CANCEL", intent=key, operation="cancel", state=State.STAND_DOWN)
        if execute:
            action = "shutdown" if kind == "off" else "reboot" if kind == "reboot" else "reboot," + target
            self.crash.hit("before_action")
            self.rtc.storage.append_record("actions.jsonl", canonical(dict(
                action=action, tx_id=result["tx_id"], boot_id=self.boot_id)) + b"\n")
            self.crash.hit("after_action")
        return result

    def request_off(self):
        return self._entry(lambda: self._shutdown("off"))

    def request_reboot(self):
        return self._entry(lambda: self._shutdown("reboot"))

    def request_maintenance(self, target):
        if target not in MAINTENANCE_TARGETS:
            return self._entry(lambda: self._shutdown("maintenance", "unknown", execute=False))
        return self._entry(lambda: self._shutdown("maintenance", target))

    def sigterm_trap(self, powerctl_value=None, delivered=True):
        if not delivered:
            return dict(success=False, skipped=True, error="signal lost")
        parsed = parse_powerctl(powerctl_value)
        kind = parsed["action"]
        def trap():
            state = self.ledger.load().state
            if state["state"] in (State.FAULT.value, State.QUARANTINE.value):
                return dict(success=False, skipped=True, error="stronger stop state")
            return self._shutdown(kind, parsed["target"], execute=False)
        return self._entry(trap)

    def cancel_intent(self, tx_id=None, *, confirmed_failure=False):
        def cancel():
            state = self.ledger.load().state
            candidates = [(key, item) for key, item in state["intents"].items()
                          if item["record"]["source_boot_id"] == self.boot_id and
                          not item["consumed"] and not item["cancelled"] and
                          (tx_id is None or tx_id == key)]
            if not candidates:
                return dict(success=False, error="no matching intent")
            key, _ = candidates[-1]
            if not confirmed_failure:
                self._record(state=State.STAND_DOWN, operation="wait", result=dict(intent_wait=key))
                return dict(success=False, error="INTENT_WAIT")
            self._stop(reason="confirmed command failure")
            result = self._clear_owned(final_state=State.STAND_DOWN)
            if result["success"]:
                self._record(type="CANCEL", intent=key, operation="cancel", state=State.STAND_DOWN)
            return result
        return self._entry(cancel)

    def disarm(self):
        def disarm():
            loaded = self.ledger.load()
            if loaded.status != LoadStatus.OK:
                slot = self.read_slot()
                return dict(success=False, slot_empty=slot is None, arm_id=None,
                            tx_id=None, pending_failures=[loaded.status.value])
            arm = loaded.state["armed_arm_id"]
            self._stop(reason="disarm")
            result = self._clear_owned(operation="disarm", final_state=State.DISARMED)
            return dict(result, slot_empty=self.read_slot() is None, arm_id=arm,
                        pending_failures=[] if result["success"] else [result.get("error")])
        result = self._entry(disarm, allow_runtime_fault=True)
        result.setdefault("slot_empty", None)
        result.setdefault("arm_id", None)
        result.setdefault("tx_id", None)
        result.setdefault("pending_failures", [] if result["success"] else
                          [result.get("error", result.get("status", "unknown"))])
        return result

    def uninstall(self):
        def uninstall():
            self._stop(reason="uninstall")
            result = self._clear_owned(operation="disarm", final_state=State.DISARMED)
            if not result["success"]:
                return result
            self.rtc.storage.atomic_write("uninstall.json", canonical(result))
            self.lock.require()
            self.storage.remove_directory()
            return dict(result, removed=True)
        return self._entry(uninstall)
