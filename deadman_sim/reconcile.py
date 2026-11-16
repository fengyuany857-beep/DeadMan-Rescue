from dataclasses import asdict
import uuid

from .budget import settle
from .classify import Evidence, classify
from .engine import HardwareFailure
from .ledger import LoadStatus, State, retired_boot


def reconcile(module, evidence=None):
    slot = module.read_slot()
    loaded = module.ledger.load()
    if loaded.status != LoadStatus.OK:
        return dict(success=False, status=loaded.status.value, state=State.UNKNOWN.value,
                    slot=slot, slot_empty=slot is None)
    state = loaded.state
    if module.boot_id in state["reconciled_boot_ids"]:
        module.ledger.write_cache()
        return dict(success=True, repeated=True, **state["boots"][module.boot_id])
    if retired_boot(state, module.boot_id):
        # Forgotten results cannot authorize another consumption or REOFF.
        return dict(success=False, error="retired/ambiguous boot identity")
    current_intents = [item for item in state["intents"].values()
                       if item["record"]["source_boot_id"] == module.boot_id and
                       not item["consumed"] and not item["cancelled"]]
    if current_intents:
        return dict(success=False, state=State.STAND_DOWN.value, error="current boot intent")
    pending = next((r for r in state["pending_txs"].values()
                    if r["type"] == "BOOT" and r["source_boot_id"] == module.boot_id), None)
    if pending:
        base = {key: value for key, value in pending.items() if key not in (
            "version", "instance_generation", "seq", "prev_checksum", "checksum", "length", "phase")}
        observation = dict(base["result"])
        evidence = Evidence(**observation["evidence"])
    else:
        evidence = evidence or Evidence()
        active = [(key, item["record"]) for key, item in state["intents"].items()
                  if not item["consumed"] and not item["cancelled"] and
                  key not in state["quarantined_intents"]]
        invalid = {key: "identity/arm mismatch" for key, record in active
                   if record["instance_generation"] != state["instance_generation"] or
                   (record["arm_id"] != state["last_protection_arm"] and not (
                       key in state["pending_txs"] and record["requested"] and
                       record["previous_arm_id"] == state["armed_arm_id"]))}
        if len(active) > 1:
            invalid.update({key: "multiple active intents" for key, _ in active})
        if invalid:
            module._stop(State.QUARANTINE, "conflicting old intents")
            module._record(state=State.QUARANTINE, operation="isolate",
                           result=dict(quarantined_intents=invalid))
            return dict(success=False, state=State.QUARANTINE.value, error="conflicting intents")
        key, intent = active[0] if active else (None, None)
        arm = intent["arm_id"] if intent else state["armed_arm_id"]
        unknown_txs = list(state["pending_txs"])
        prior_boots = {key: record["result"] for key, record in state["pending_txs"].items()
                       if record["type"] == "BOOT"}
        conclusion = classify(intent["intent"] if intent else None, arm, evidence) if arm or intent else None
        if prior_boots or (unknown_txs and (not intent or intent["phase"] != "COMMITTED")):
            conclusion = ("OFF_OUTCOME_UNKNOWN" if intent and intent["intent"] == "off" else
                          "REBOOT_OUTCOME_UNKNOWN" if intent else "ABNORMAL_OUTCOME_UNKNOWN")
        observation = dict(evidence=asdict(evidence), observed_slot=slot, old_arm_id=arm,
                           prior_phase=intent["phase"] if intent else
                               {key: record["phase"] for key, record in state["pending_txs"].items()},
                           conclusion=conclusion, intent_id=key, unknown_txs=unknown_txs,
                           prior_boot_observations=prior_boots,
                           state=State.RECONCILING.value,
                           control=dict(enabled=False, supervisor_permit=False, retry=False))
        base = dict(tx_id=str(uuid.uuid4()), type="BOOT", operation="reconcile",
                    source_boot_id=module.boot_id, arm_id=arm, intent=key,
                    stop_generation=state["stop_generation"] + 1,
                    result=observation, readback=slot, channel="rtc",
                    rtc_samples=[module.rtc.read_counter()], mono_samples=[module.clock.monotonic()])
        module.crash.hit("boot_before_observation")
        module.ledger.append(emergency=True, phase="PREPARED", **base)
        module._release_owner()
        module.crash.hit("boot_after_observation")
    if not module._known_own(slot, loaded):
        module._stop(State.QUARANTINE, "unknown boot slot")
        return dict(success=False, state=State.QUARANTINE.value, error="unknown ownership", slot=slot)
    module.lock.require()
    module.crash.hit("boot_before_clear")
    slot = module.read_slot()
    if not module._known_own(slot):
        module._stop(State.QUARANTINE, "boot slot changed before clear")
        return dict(success=False, state=State.QUARANTINE.value, error="unknown ownership", slot=slot)
    code = module.rtc.write("0")
    module.crash.hit("boot_after_clear")
    try:
        slot = module.read_slot()
        if code or slot is not None:
            raise HardwareFailure("boot clear", slot)
    except HardwareFailure as exc:
        module.ledger.append(emergency=True, phase="FAULT",
                             **dict(base, error=exc.step, result=dict(observation, state=State.FAULT.value,
                                     control=dict(enabled=False, supervisor_permit=False, fault_latched=True))))
        return dict(success=False, error=exc.step, slot_unknown=exc.unreadable)
    module.ledger.append(emergency=True, phase="CLEARED_VERIFIED", **dict(base, readback=None))
    module.crash.hit("boot_after_clear_evidence")
    state = module.ledger.load().state
    conclusion = observation["conclusion"]
    budget = settle(state["budget"], module.boot_id, conclusion)
    candidate = None
    intent = state["intents"].get(observation["intent_id"])
    if (conclusion == "OFF_RTC_WAKE" and intent and not intent["consumed"] and
            intent["record"]["phase"] == "COMMITTED" and not observation["unknown_txs"]):
        candidate = dict(intent_id=observation["intent_id"], boot_id=module.boot_id,
                         arm_id=base["arm_id"], clear_verified=True)
    final = State.STAND_DOWN if state["control"].get("fault_latched") else State.DISARMED
    result = dict(observation, state=final.value, clear_verified=True,
                  last_alarm=None, last_samples=module.sample(), budget=budget,
                  reoff_candidate=candidate, abort_txs=observation["unknown_txs"])
    module.crash.hit("boot_before_consumption")
    module.ledger.append(emergency=True, phase="COMMITTED", **dict(base,
        consumed_by_boot_id=module.boot_id, readback=None, budget=budget, result=result))
    module.crash.hit("boot_after_consumption")
    module.ledger.write_cache()
    module.crash.hit("boot_after_cache")
    module._recovered = True
    response = dict(success=True, **result)
    if candidate:
        response["reoff"] = module._attempt_reoff(candidate)
    return response
