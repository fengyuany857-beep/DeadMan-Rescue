from itertools import product
import unittest

from deadman_sim.classify import Evidence, classify


class ClassifyTests(unittest.TestCase):
    def test_all_nine_mutually_exclusive_outcomes(self):
        cases = [("reboot", "normal_reboot", False, "REBOOT_OK"),
                 ("reboot", "rtc_wake", True, "REBOOT_RESCUED"),
                 ("reboot", "unknown", None, "REBOOT_OUTCOME_UNKNOWN"),
                 (None, "non_rtc_recovery", False, "SELF_RECOVERED"),
                 (None, "rtc_wake", True, "RESCUED_ABNORMAL"),
                 (None, "manual_power_key", False, "ABNORMAL_OUTCOME_UNKNOWN"),
                 ("off", "manual_power_key", False, "OFF_MANUAL"),
                 ("off", "rtc_wake", True, "OFF_RTC_WAKE"),
                 ("off", "unknown", None, "OFF_OUTCOME_UNKNOWN")]
        for intent, reason, fired, expected in cases:
            with self.subTest(expected=expected):
                evidence = Evidence(reason, True, "arm", True, False, fired)
                self.assertEqual(classify(intent, "arm", evidence), expected)

    def test_defaults_conflicts_and_missing_association_stay_unknown(self):
        self.assertEqual(classify("off", None, Evidence("manual_power_key", True)),
                         "OFF_MANUAL")
        for evidence in (None, Evidence("rtc_wake"),
                         Evidence("rtc_wake", True, "other", True, False, True),
                         Evidence("rtc_wake", True, "arm", True, True, True),
                         Evidence("normal_reboot", True, "arm", True, False, True),
                         Evidence("rtc_wake", True, "arm", False, False, True)):
            with self.subTest(evidence=evidence):
                self.assertEqual(classify("reboot", "arm", evidence), "REBOOT_OUTCOME_UNKNOWN")

    def test_manual_and_self_recovery_evidence(self):
        self.assertEqual(classify(None, "arm", Evidence("manual_power_key", True, "arm", True, False, False)),
                         "ABNORMAL_OUTCOME_UNKNOWN")
        for trusted, fired in ((False, False), (True, None), (True, True)):
            with self.subTest(trusted=trusted, fired=fired):
                self.assertEqual(classify(None, "arm", Evidence("non_rtc_recovery", True, "arm", trusted,
                                                               False, fired)), "ABNORMAL_OUTCOME_UNKNOWN")

    def test_mapping_and_named_mismatches_are_unknown_in_every_family(self):
        for intent, reason, unknown in (("off", "manual_power_key", "OFF_OUTCOME_UNKNOWN"),
                                        ("reboot", "normal_reboot", "REBOOT_OUTCOME_UNKNOWN"),
                                        (None, "non_rtc_recovery", "ABNORMAL_OUTCOME_UNKNOWN")):
            for evidence in (Evidence(reason, False, "arm", True, False, False),
                             Evidence(reason, True, "other", True, False, False)):
                with self.subTest(intent=intent, evidence=evidence):
                    self.assertEqual(classify(intent, "arm", evidence), unknown)

    def test_combination_table_has_exactly_one_outcome_in_correct_family(self):
        families = {"reboot": ("REBOOT_OK", "REBOOT_RESCUED", "REBOOT_OUTCOME_UNKNOWN"),
                    "off": ("OFF_MANUAL", "OFF_RTC_WAKE", "OFF_OUTCOME_UNKNOWN"),
                    None: ("SELF_RECOVERED", "RESCUED_ABNORMAL", "ABNORMAL_OUTCOME_UNKNOWN")}
        reasons = (None, "unknown", "normal_reboot", "non_rtc_recovery", "manual_power_key", "rtc_wake")
        for intent, reason, arm, named, mapping, trusted, conflict, fired in product(
                families, reasons, (None, "arm"), (None, "arm", "other"),
                (False, True), (False, True), (False, True), (None, False, True)):
            evidence = Evidence(reason, mapping, named, trusted, conflict, fired)
            outcome = classify(intent, arm, evidence)
            family = families[intent]
            with self.subTest(intent=intent, arm=arm, evidence=evidence):
                self.assertEqual(sum(outcome == member for member in family), 1)
                if not mapping or conflict or (named is not None and named != arm):
                    self.assertEqual(outcome, family[2])
                if outcome in ("REBOOT_RESCUED", "OFF_RTC_WAKE", "RESCUED_ABNORMAL"):
                    self.assertIsNotNone(arm)
                    self.assertEqual(named, arm)
                    self.assertTrue(trusted)
                    self.assertIs(fired, True)
