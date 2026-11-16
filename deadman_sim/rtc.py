import errno
import json
import math
import re

from .storage import Storage


class SimRTC:
    def __init__(self, hardware_dir, clock):
        self.storage = Storage(hardware_dir)
        if not self.storage.root.exists():
            self.storage.initialize()
        self.clock = clock
        self.fail_writes = self.fail_clears = 0
        self.clear_no_effect = False
        self.unreadable = False
        if not self.storage.path("rtc.json").exists():
            self._save({"counter": 3942, "alarm": None, "owner": None,
                        "clock_elapsed": clock.elapsed})
        self._advance(0)
        clock.subscribe(self._advance)

    def close(self):
        self.clock.unsubscribe(self._advance)

    def _load(self):
        return json.loads(self.storage.read("rtc.json"))

    def _save(self, state):
        self.storage.atomic_write("rtc.json", json.dumps(state).encode())

    def _advance(self, sec):
        state = self._load()
        state["counter"] += self.clock.elapsed - state["clock_elapsed"]
        state["clock_elapsed"] = self.clock.elapsed
        self._save(state)

    def read_counter(self):
        return self._load()["counter"]

    def read_alarm(self):
        if self.unreadable:
            raise OSError(errno.EIO, "unreadable RTC slot")
        return self._load()["alarm"]

    def write(self, text):
        if not isinstance(text, str) or not re.fullmatch(r"0|\+[1-9][0-9]*", text):
            raise ValueError("only 0 or +N accepted")
        attr = "fail_clears" if text == "0" else "fail_writes"
        if getattr(self, attr):
            setattr(self, attr, getattr(self, attr) - 1)
            return errno.EIO
        if text == "0" and self.clear_no_effect:
            return 0
        state = self._load()
        state["alarm"] = None if text == "0" else state["counter"] + int(text[1:])
        state["owner"] = None if text == "0" else "module"
        self._save(state)
        return 0

    def jump(self, delta):
        if type(delta) not in (int, float) or not math.isfinite(delta):
            raise ValueError("invalid jump")
        state = self._load()
        state["counter"] += delta
        self._save(state)

    def external_write(self, absolute):
        if absolute is not None and (type(absolute) not in (int, float) or
                                     not math.isfinite(absolute)):
            raise ValueError("invalid alarm")
        state = self._load()
        state.update(alarm=absolute, owner="external")
        self._save(state)

    @property
    def fired(self):
        alarm = self.read_alarm()
        return alarm is not None and alarm <= self.read_counter()
