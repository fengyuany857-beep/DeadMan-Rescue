import copy
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import stat
import uuid

from .crash import CrashInjector
from .params import RECORD_LIMIT, RESERVE, SNAPSHOT_LIMIT, TAIL_LIMIT
from .storage import StorageExhausted


class State(str, Enum):
    UNKNOWN = "UNKNOWN"
    DISARMED = "DISARMED"
    ARM_PENDING = "ARM_PENDING"
    ARMED = "ARMED"
    STAND_DOWN = "STAND_DOWN"
    RECONCILING = "RECONCILING"
    FAULT = "FAULT"
    QUARANTINE = "QUARANTINE"


class LoadStatus(str, Enum):
    OK = "OK"
    BAD_TAIL = "BAD_TAIL"
    MID_CORRUPTION = "MID_CORRUPTION"
    SEQ_ROLLBACK = "SEQ_ROLLBACK"
    UNKNOWN_VERSION = "UNKNOWN_VERSION"
    SNAPSHOT_CORRUPT = "SNAPSHOT_CORRUPT"
    HEAD_MISSING = "HEAD_MISSING"
    HEAD_INVALID = "HEAD_INVALID"
    DIR_MISSING = "DIR_MISSING"
    CACHE_AHEAD = "CACHE_AHEAD"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


FIELDS = ("version instance_generation seq tx_id phase type operation source_boot_id "
          "arm_id previous_arm_id consumed_by_boot_id stop_generation intent target channel "
          "rtc_samples mono_samples wall requested readback budget result error "
          "prev_checksum length checksum").split()


def validate_record(record):
    for key in ("instance_generation", "tx_id", "phase", "type", "operation"):
        if not isinstance(record[key], str) or not record[key] or len(record[key]) > 256:
            raise ValueError("invalid " + key)
    for key in ("source_boot_id", "arm_id", "previous_arm_id", "consumed_by_boot_id",
                "intent", "target", "channel"):
        if record[key] is not None and (not isinstance(record[key], str) or
                                         not record[key] or len(record[key]) > 256):
            raise ValueError("invalid " + key)
    if record["stop_generation"] is not None and (
            type(record["stop_generation"]) is not int or record["stop_generation"] < 0):
        raise ValueError("invalid stop generation")
    if record["budget"] is not None and not isinstance(record["budget"], dict):
        raise ValueError("invalid budget")
    for key in ("rtc_samples", "mono_samples"):
        if record[key] is not None and not isinstance(record[key], (dict, list)):
            raise ValueError("invalid samples")
    if record["phase"] not in ("PREPARED", "CLEARED_VERIFIED", "WRITTEN",
                                "READBACK_VERIFIED", "COMMITTED", "FAULT"):
        raise ValueError("unknown transaction phase")


def encode_record(values, previous):
    record = dict.fromkeys(FIELDS)
    record.update(values)
    record["prev_checksum"] = previous
    payload = {k: v for k, v in record.items() if k not in ("length", "checksum")}
    record["length"] = len(canonical(payload))
    record["checksum"] = hashlib.sha256(previous.encode() + canonical(payload)).hexdigest()
    data = canonical(record) + b"\n"
    if len(data) > RECORD_LIMIT:
        raise StorageExhausted("record exceeds limit")
    return record, data


def initial_state(generation):
    return dict(state=State.UNKNOWN.value, instance_generation=generation,
                stop_generation=0, armed_arm_id=None, pending_txs={}, intents={},
                consumed_arms={}, superseded_arms=[], reconciled_boot_ids=[],
                budget={}, write_fail_count=0, fault_flags=[], reoff_attempts={},
                control={}, last_alarm=None, last_samples={}, intent_wait=[],
                boots={}, reoff_candidates={}, quarantined_intents={},
                quarantined_transactions={}, last_protection_arm=None)


def reduce_record(state, record):
    state = copy.deepcopy(state)
    tx, phase, kind = record["tx_id"], record["phase"], record["type"]
    if tx in state["pending_txs"]:
        previous = state["pending_txs"][tx]
        record = {key: (value if value is not None or key == "readback" else previous.get(key))
                  for key, value in record.items()}
    if record["stop_generation"] is not None:
        if record["stop_generation"] < state["stop_generation"]:
            raise ValueError("stop generation rollback")
        state["stop_generation"] = record["stop_generation"]
    if record["budget"] is not None:
        state["budget"] = record["budget"]
    if phase == "PREPARED":
        state["pending_txs"][tx] = record
        state["state"] = (State.ARM_PENDING.value if record["operation"] in ("arm", "renew")
                          else State.STAND_DOWN.value)
        if record["operation"] == "reconcile":
            state["state"] = State.RECONCILING.value
            state["control"].update(dict(enabled=False, supervisor_permit=False, retry=False))
    elif phase == "COMMITTED":
        state["pending_txs"].pop(tx, None)
    elif tx in state["pending_txs"]:
        state["pending_txs"][tx] = record
    if kind == "INTENT":
        old = state["intents"].get(tx, {})
        state["intents"][tx] = dict(record=record, consumed=old.get("consumed", False),
                                    cancelled=old.get("cancelled", False))
        state["state"] = State.STAND_DOWN.value
    if kind == "CANCEL" and record["intent"] in state["intents"]:
        state["intents"][record["intent"]]["cancelled"] = True
    if record["consumed_by_boot_id"] and phase == "COMMITTED":
        boot = record["consumed_by_boot_id"]
        if boot not in state["reconciled_boot_ids"]:
            state["reconciled_boot_ids"].append(boot)
        if record["arm_id"]:
            state["consumed_arms"][record["arm_id"]] = boot
        if record["intent"] in state["intents"]:
            state["intents"][record["intent"]]["consumed"] = True
    if kind == "REOFF_ATTEMPT":
        key = record["intent"]
        state["reoff_attempts"][key] = state["reoff_attempts"].get(key, 0) + 1
    if phase == "COMMITTED":
        if record["operation"] in ("arm", "renew", "rearm"):
            state["armed_arm_id"] = record["arm_id"]
            state["last_protection_arm"] = record["arm_id"]
            state["state"] = State.ARMED.value
            state["write_fail_count"] = 0
        if record["previous_arm_id"]:
            state["superseded_arms"].append(record["previous_arm_id"])
        if record["operation"] in ("clear", "disarm", "off", "reconcile"):
            state["armed_arm_id"] = None
            state["state"] = State.DISARMED.value
        if isinstance(record["result"], dict) and "state" in record["result"]:
            state["state"] = State(record["result"]["state"]).value
    result = record["result"] if isinstance(record["result"], dict) else {}
    if phase in ("COMMITTED", "FAULT"):
        state["control"].update(result.get("control", {}))
        if "write_fail_count" in result:
            state["write_fail_count"] = result["write_fail_count"]
        if "last_alarm" in result:
            state["last_alarm"] = result["last_alarm"]
        if "last_samples" in result:
            state["last_samples"] = result["last_samples"]
        if result.get("abort_tx"):
            state["pending_txs"].pop(result["abort_tx"], None)
        for key in result.get("abort_txs", []):
            state["pending_txs"].pop(key, None)
        if result.get("intent_wait"):
            state["intent_wait"].append(result["intent_wait"])
        if kind == "BOOT" and phase == "COMMITTED":
            state["boots"][record["consumed_by_boot_id"]] = result
        if result.get("reoff_candidate"):
            state["reoff_candidates"][record["consumed_by_boot_id"]] = result["reoff_candidate"]
        state["quarantined_intents"].update(result.get("quarantined_intents", {}))
    if phase == "FAULT":
        state["state"] = State.FAULT.value
        state["write_fail_count"] += 1
        state["fault_flags"].append(record["error"])
        if "write_fail_count" in result:
            state["write_fail_count"] = result["write_fail_count"]
        if "state" in result:
            state["state"] = State(result["state"]).value
    return state


@dataclass
class LoadResult:
    status: LoadStatus
    state: dict = field(default_factory=dict)
    records: list = field(default_factory=list)
    seq: int = 0
    checksum: str = ""
    generation: int = 0
    offset: int = 0
    head: dict = field(default_factory=dict)
    error: str = ""


class Ledger:
    def __init__(self, storage, lock, crash=None):
        self.storage, self.lock = storage, lock
        self.crash = crash or CrashInjector()
        self._verified = None
        self._signature = None

    def _fingerprint(self, head):
        self.storage._check(self.storage.root)
        names = ["HEAD", "state"]
        if head:
            names += [head["snapshot"], head["tail"]]
        signature = []
        for name in names:
            path = self.storage.root / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                signature.append(None)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("unsafe ledger path")
            signature.append((info.st_dev, info.st_ino, info.st_size,
                              info.st_mtime_ns, info.st_ctime_ns))
        return tuple(signature)

    def _require(self):
        self.lock.require()
        if self.lock.state_dir != self.storage.root:
            raise ValueError("lock/state mismatch")

    def initialize(self, instance_generation=None):
        self._require()
        if list(self.storage.root.iterdir()):
            raise ValueError("new install must be explicitly empty")
        generation = instance_generation or str(uuid.uuid4())
        self._switch(initial_state(generation), 0, "", 1)
        return self.load()

    def _switch(self, state, seq, checksum, generation):
        snap = dict(version=1, generation=generation, seq=seq,
                    checksum=checksum, state=state)
        envelope = dict(payload=snap, checksum=digest(snap))
        data = canonical(envelope)
        if len(data) > SNAPSHOT_LIMIT:
            raise StorageExhausted("unfinished evidence exceeds snapshot limit")
        sn, tail = f"snapshot-{generation}.json", f"tail-{generation}.jsonl"
        self.storage.atomic_write(sn, data)
        self.crash.hit("snapshot_written")
        self.storage.atomic_write(tail, b"")
        self.crash.hit("tail_written")
        head = dict(version=1, generation=generation, snapshot=sn, tail=tail)
        self.crash.hit("before_head_switch")
        self.storage.atomic_write("HEAD", canonical(head))
        self.crash.hit("head_switched")

    def load(self):
        if self._verified is not None:
            signature = self._fingerprint(self._verified.head)
            if signature == self._signature:
                return self._verified
        result = self._load_disk()
        if result.status == LoadStatus.OK:
            self._verified = result
            self._signature = self._fingerprint(result.head)
        else:
            self._verified = None
        return result

    def _load_disk(self):
        result = LoadResult(LoadStatus.OK)
        if not self.storage.root.exists():
            result.status = LoadStatus.DIR_MISSING
            return result
        try:
            raw = self.storage.read("HEAD")
        except FileNotFoundError:
            result.status = LoadStatus.HEAD_MISSING
            return result
        try:
            head = json.loads(raw)
            g = head["generation"]
            if head != dict(version=1, generation=g, snapshot=f"snapshot-{g}.json",
                            tail=f"tail-{g}.jsonl") or type(g) is not int or g < 1:
                raise ValueError("invalid HEAD")
            result.head, result.generation = head, g
        except (ValueError, KeyError, TypeError):
            result.status = LoadStatus.HEAD_INVALID
            return result
        try:
            raw = self.storage.read(head["snapshot"])
            envelope = json.loads(raw)
            snap = envelope["payload"]
            if len(raw) > SNAPSHOT_LIMIT or digest(snap) != envelope["checksum"]:
                raise ValueError("snapshot checksum")
            if snap["version"] != 1:
                result.status = LoadStatus.UNKNOWN_VERSION
                return result
            if snap["generation"] != g or type(snap["seq"]) is not int or snap["seq"] < 0:
                raise ValueError("snapshot identity")
            result.state = snap["state"]
            template = initial_state(result.state["instance_generation"])
            if set(result.state) != set(template):
                raise ValueError("snapshot state fields")
            for key, value in template.items():
                if value is not None and type(result.state[key]) is not type(value):
                    raise ValueError("snapshot state type")
            State(result.state["state"])
            result.seq, result.checksum = snap["seq"], snap["checksum"]
        except (ValueError, KeyError, TypeError, OSError):
            result.status = LoadStatus.SNAPSHOT_CORRUPT
            return result
        try:
            tail = self.storage.read(head["tail"])
        except FileNotFoundError:
            result.status = LoadStatus.HEAD_INVALID
            return result
        lines = tail.splitlines(keepends=True)
        for index, line in enumerate(lines):
            try:
                r = json.loads(line)
                if not isinstance(r, dict):
                    raise ValueError("record must be an object")
                if type(r.get("version")) is not int or r.get("version") != 1:
                    result.status = LoadStatus.UNKNOWN_VERSION
                    break
                if type(r["seq"]) is not int or r["seq"] != result.seq + 1:
                    result.status = LoadStatus.SEQ_ROLLBACK
                    break
                if set(r) != set(FIELDS) or not line.endswith(b"\n"):
                    raise ValueError("incomplete record")
                if r["instance_generation"] != result.state["instance_generation"]:
                    result.status = LoadStatus.MID_CORRUPTION
                    break
                expected, _ = encode_record(r, result.checksum)
                if r != expected or len(line) > RECORD_LIMIT:
                    raise ValueError("record checksum/length")
                validate_record(r)
                result.state = reduce_record(result.state, r)
                result.records.append(r)
                result.seq, result.checksum = r["seq"], r["checksum"]
                result.offset += len(line)
            except (ValueError, TypeError, KeyError, StorageExhausted):
                result.status = (LoadStatus.BAD_TAIL if index == len(lines) - 1
                                 else LoadStatus.MID_CORRUPTION)
                break
        if result.status == LoadStatus.OK and self.storage.path("state").exists():
            try:
                cache = json.loads(self.storage.read("state"))
                if (cache["seq"] > result.seq or
                        cache["instance_generation"] != result.state["instance_generation"]):
                    result.status = LoadStatus.CACHE_AHEAD
            except (ValueError, KeyError, TypeError):
                pass
        return result

    def append(self, *, emergency=False, **values):
        self._require()
        loaded = self.load()
        if loaded.status != LoadStatus.OK:
            raise ValueError(loaded.status.value)
        if set(values) - set(FIELDS):
            raise ValueError("unknown fields")
        values.update(version=1, seq=loaded.seq + 1,
                      instance_generation=loaded.state["instance_generation"])
        values.setdefault("tx_id", str(uuid.uuid4()))
        values.setdefault("phase", "COMMITTED")
        values.setdefault("type", "TRANSACTION")
        values.setdefault("operation", "observe")
        record, data = encode_record(values, loaded.checksum)
        validate_record(record)
        next_state = reduce_record(loaded.state, record)
        size = len(self.storage.read(loaded.head["tail"]))
        reserve = 0 if emergency else RESERVE
        if (size + len(data) > TAIL_LIMIT - reserve or
                self.storage.usage() + len(data) + reserve > self.storage.quota):
            self.compact()
            loaded = self.load()
        try:
            self.storage.ensure_space(len(data) + reserve)
            self.storage.append_record(loaded.head["tail"], data)
        except OSError as exc:
            if exc.errno == 28:
                raise StorageExhausted() from exc
            raise
        self._verified = LoadResult(LoadStatus.OK, state=next_state,
            records=loaded.records + [record], seq=record["seq"], checksum=record["checksum"],
            generation=loaded.generation, offset=loaded.offset + len(data), head=loaded.head)
        self._signature = self._fingerprint(loaded.head)
        self.crash.hit("record_appended")
        return record

    def repair_bad_tail(self):
        self._require()
        loaded = self.load()
        if loaded.status != LoadStatus.BAD_TAIL:
            raise ValueError("only a bad tail may be truncated")
        tail = self.storage.read(loaded.head["tail"])
        evidence = f"quarantine-{uuid.uuid4().hex}.bin"
        self.storage.atomic_write(evidence, tail[loaded.offset:])
        self.crash.hit("bad_tail_quarantined")
        self.storage.truncate(loaded.head["tail"], loaded.offset)
        return evidence

    def compact(self):
        self._require()
        loaded = self.load()
        if loaded.status != LoadStatus.OK:
            raise ValueError(loaded.status.value)
        try:
            self._switch(loaded.state, loaded.seq, loaded.checksum, loaded.generation + 1)
        except OSError as exc:
            if exc.errno == 28:
                raise StorageExhausted() from exc
            raise
        for name in (loaded.head["snapshot"], loaded.head["tail"]):
            self.storage.delete(name)

    def write_cache(self):
        self._require()
        loaded = self.load()
        if loaded.status != LoadStatus.OK:
            raise ValueError(loaded.status.value)
        self.storage.atomic_write("state", canonical(dict(seq=loaded.seq,
            instance_generation=loaded.state["instance_generation"], state=loaded.state)))
        self._signature = self._fingerprint(loaded.head)
