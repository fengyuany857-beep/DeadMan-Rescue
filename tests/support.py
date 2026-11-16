from pathlib import Path
import tempfile
import unittest

from deadman_sim.clock import SimClock
from deadman_sim.sensors import SimSensors
from deadman_sim.system import Module


class ModuleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = SimClock()
        self.sensors = SimSensors()
        self.modules = []
        self.module = self.make_module()
        self.assertTrue(self.module.install()["success"])
        self.assertTrue(self.module.enable()["success"])

    def make_module(self, config=None):
        module = Module(self.root, self.root / "hardware", self.clock, self.sensors, config=config)
        self.modules.append(module)
        self.addCleanup(module.close)
        return module

    def new_boot(self, evidence=None, config=None):
        self.module.close()
        self.clock.new_boot()
        self.module = self.make_module(config)
        return self.module.boot(evidence)

    def arm(self):
        self.assertTrue(self.module.tick()["success"])
        return self.module.rtc.read_alarm()
