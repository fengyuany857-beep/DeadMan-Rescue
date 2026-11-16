import errno
from pathlib import Path
import shutil
import tempfile
import unittest

from deadman_sim.clock import SimClock
from deadman_sim.rtc import SimRTC


class RTCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = SimClock(wall=10000000)
        self.rtc = SimRTC(Path(self.tmp.name) / "hardware", self.clock)
        self.addCleanup(lambda: self.rtc.close())

    def test_counter_reboot_rebuild_and_state_deletion(self):
        state = Path(self.tmp.name) / "state"
        state.mkdir()
        self.assertEqual(self.rtc.read_counter(), 3942)
        self.assertIsNone(self.rtc.read_alarm())
        self.assertEqual(self.rtc.write("+600"), 0)
        self.clock.advance(40)
        self.clock.new_boot()
        self.assertEqual(self.clock.monotonic(), 0)
        shutil.rmtree(state)
        rebuilt = SimRTC(Path(self.tmp.name) / "hardware", self.clock)
        self.addCleanup(rebuilt.close)
        self.clock.advance(560)
        self.assertEqual(rebuilt.read_counter(), 4542)
        self.assertTrue(rebuilt.fired)
        self.assertEqual(rebuilt.read_alarm(), 4542)

    def test_counter_keeps_counting_when_no_rtc_object_exists(self):
        self.rtc.write("+600")
        self.clock.advance(40)
        self.rtc.close()
        self.clock.advance(560)
        self.clock.new_boot()
        rebuilt = SimRTC(Path(self.tmp.name) / "hardware", self.clock)
        self.rtc = rebuilt
        self.assertEqual(rebuilt.read_counter(), 4542)
        self.assertTrue(rebuilt.fired)
        self.assertEqual(rebuilt.read_alarm(), 4542)
        rebuilt.close()
        self.clock.advance(100)
        again = SimRTC(Path(self.tmp.name) / "hardware", self.clock)
        self.rtc = again
        self.assertEqual(again.read_counter(), 4642)
        self.assertTrue(again.fired)

    def test_closed_rtc_preserves_counter_jumps_and_alarm_clear(self):
        self.rtc.jump(-100)
        self.rtc.write("0")
        self.rtc.close()
        self.clock.advance(200)
        rebuilt = SimRTC(Path(self.tmp.name) / "hardware", self.clock)
        self.rtc = rebuilt
        self.assertEqual(rebuilt.read_counter(), 4042)
        self.assertIsNone(rebuilt.read_alarm())
        self.assertFalse(rebuilt.fired)

    def test_faults_and_relative_only(self):
        for invalid in ("1234", "+0", "-1", "+1\n", "garbage", ""):
            with self.assertRaises(ValueError):
                self.rtc.write(invalid)
        self.rtc.fail_writes = 1
        self.assertEqual(self.rtc.write("+600"), errno.EIO)
        self.assertIsNone(self.rtc.read_alarm())
        self.rtc.external_write(4000)
        self.assertEqual(self.rtc._load()["owner"], "external")
        self.rtc.fail_clears = 1
        self.assertEqual(self.rtc.write("0"), errno.EIO)
        self.rtc.clear_no_effect = True
        self.assertEqual(self.rtc.write("0"), 0)
        self.assertEqual(self.rtc.read_alarm(), 4000)
        self.rtc.jump(100)
        self.assertTrue(self.rtc.fired)
        self.rtc.jump(-200)
        self.assertFalse(self.rtc.fired)
        self.rtc.unreadable = True
        with self.assertRaises(OSError):
            self.rtc.read_alarm()

    def test_clock_rejects_backwards(self):
        for value in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.clock.advance(value)
