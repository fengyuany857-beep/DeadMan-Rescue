from dataclasses import dataclass


@dataclass(frozen=True)
class Evidence:
    reason: str = "unknown"
    mapping_verified: bool = False  # T2/T3 待裁决: never infer a verified mapping.
    arm_id: str = None
    trusted_timebase: bool = False
    external_conflict: bool = False
    rtc_fired_before: bool = None
    raw: str = None


def classify(intent, arm_id, evidence=None):
    evidence = evidence or Evidence()
    prefix = "REBOOT" if intent == "reboot" else "OFF" if intent == "off" else "ABNORMAL"
    unknown = prefix + "_OUTCOME_UNKNOWN"
    if (not evidence.mapping_verified or evidence.external_conflict or
            (evidence.arm_id is not None and evidence.arm_id != arm_id)):
        return unknown
    if intent == "off" and evidence.reason == "manual_power_key" and evidence.rtc_fired_before is not True:
        return "OFF_MANUAL"
    if not evidence.trusted_timebase:
        return unknown
    if evidence.reason == "rtc_wake" and evidence.rtc_fired_before is True:
        if not arm_id or evidence.arm_id != arm_id:
            return unknown
        return "REBOOT_RESCUED" if intent == "reboot" else "OFF_RTC_WAKE" if intent == "off" else "RESCUED_ABNORMAL"
    if evidence.rtc_fired_before is False:
        if intent == "reboot" and evidence.reason == "normal_reboot":
            return "REBOOT_OK"
        if intent is None and evidence.reason in ("non_rtc_recovery", "normal_reboot"):
            return "SELF_RECOVERED"
    return unknown
