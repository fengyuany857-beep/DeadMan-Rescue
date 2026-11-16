import math
import uuid


def new_boot_id():
    return str(uuid.uuid4())


class SimClock:
    def __init__(self, wall=0, elapsed=0):
        self.elapsed = elapsed
        self._boot_start = elapsed
        self._wall_offset = wall - elapsed
        self.boot_id = new_boot_id()
        self._listeners = []

    def monotonic(self):
        return self.elapsed - self._boot_start

    def wall(self):
        return self.elapsed + self._wall_offset

    def advance(self, sec):
        if not isinstance(sec, (int, float)) or not math.isfinite(sec) or sec < 0:
            raise ValueError("invalid advance")
        self.elapsed += sec
        for listener in tuple(self._listeners):
            listener(sec)

    def new_boot(self):
        self._boot_start = self.elapsed
        self.boot_id = new_boot_id()
        return self.boot_id

    def subscribe(self, listener):
        self._listeners.append(listener)

    def unsubscribe(self, listener):
        self._listeners.remove(listener)
