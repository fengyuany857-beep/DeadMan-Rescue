from dataclasses import dataclass
import math

from . import params


@dataclass
class SimSensors:
    battery: float = 80
    temp: float = 25

    def healthy(self):
        return (type(self.battery) in (int, float) and math.isfinite(self.battery) and
                params.LOW_BATTERY <= self.battery <= 100 and
                type(self.temp) in (int, float) and math.isfinite(self.temp) and
                self.temp < params.HIGH_TEMP)
