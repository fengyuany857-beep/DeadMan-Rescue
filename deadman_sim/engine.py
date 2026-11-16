import uuid


class HardwareFailure(Exception):
    def __init__(self, step, observed=None, unreadable=False):
        super().__init__(step)
        self.step, self.observed, self.unreadable = step, observed, unreadable


class Transaction:
    def __init__(self, module, operation, requested=None, *, kind="TRANSACTION",
                 intent=None, target=None, final_state=None):
        self.module = module
        state = module.ledger.load().state
        tx = str(uuid.uuid4())
        old = state["armed_arm_id"]
        arm = state["instance_generation"] + ":" + tx if requested else old
        samples = module.sample()
        self.base = dict(tx_id=tx, operation=operation, type=kind,
                         source_boot_id=module.boot_id, arm_id=arm,
                         previous_arm_id=old if requested else None,
                         stop_generation=state["stop_generation"], intent=intent,
                         target=target, channel="rtc", requested=requested,
                         rtc_samples=[samples["rtc"]], mono_samples=[samples["mono"]],
                         wall=module.clock.wall(), budget=state["budget"],
                         result=dict(old_alarm=module.rtc.read_alarm(),
                                     target_alarm=(samples["rtc"] + requested if requested else None),
                                     samples=samples, final_state=final_state))

    @classmethod
    def restore(cls, module, record):
        tx = cls.__new__(cls)
        tx.module = module
        tx.base = {k: v for k, v in record.items()
                   if k not in ("version", "instance_generation", "seq", "phase",
                                "prev_checksum", "length", "checksum")}
        return tx

    def persist(self, phase, **updates):
        data = dict(self.base)
        data.update(updates)
        data["phase"] = phase
        self.module.crash.hit("before_" + phase.lower() + "_persist")
        record = self.module.ledger.append(emergency=self.base["requested"] is None, **data)
        self.module.crash.hit("after_" + phase.lower() + "_persist")
        return record

    def run(self):
        self.module.crash.hit("before_prepared")
        self.persist("PREPARED")
        self.module.crash.hit("after_prepared")
        if self.base["operation"] in ("off", "disarm") or self.base["type"] == "INTENT":
            self.module.crash.hit("after_stop_generation")
        return self.clear_and_finish()

    def clear_and_finish(self):
        module = self.module
        module.crash.hit("before_clear")
        module.lock.require()
        observed = module.read_slot()
        if observed is not None and not module._known_own(observed):
            raise HardwareFailure("conflict", observed)
        code = module.rtc.write("0")
        module.crash.hit("after_clear")
        observed = module.read_slot()
        if code or observed is not None:
            raise HardwareFailure("clear", observed)
        self.persist("CLEARED_VERIFIED", readback=None)
        module.crash.hit("after_clear_evidence")
        if self.base["requested"]:
            module.crash.hit("before_write")
            module.lock.require()
            observed = module.read_slot()
            if observed is not None:
                raise HardwareFailure("conflict", observed)
            code = module.rtc.write("+" + str(self.base["requested"]))
            module.crash.hit("after_write")
            observed = module.read_slot()
            remaining = None if observed is None else observed - module.rtc.read_counter()
            if (code or remaining is None or
                    not self.base["requested"] - module.tolerance <= remaining <= self.base["requested"]):
                raise HardwareFailure("write", observed)
            self.persist("READBACK_VERIFIED", readback=observed)
            module.crash.hit("after_armed_verified")
        return self.commit()

    def commit(self):
        module = self.module
        module.lock.require()
        slot = module.read_slot()
        expected = self.base["result"]["target_alarm"] if self.base["requested"] else None
        if slot != expected:
            raise HardwareFailure("conflict", slot)
        state = module.ledger.load().state
        if state["stop_generation"] != self.base["stop_generation"]:
            raise HardwareFailure("stop generation changed", slot)
        result = dict(self.base["result"])
        result.update(last_alarm=slot, last_samples=module.sample())
        final = result.get("final_state")
        if final:
            result["state"] = final
        module.crash.hit("before_commit")
        record = self.persist("COMMITTED", readback=slot, result=result)
        module.crash.hit("after_commit")
        module.ledger.write_cache()
        return dict(success=True, tx_id=record["tx_id"], arm_id=record["arm_id"], slot=slot)
