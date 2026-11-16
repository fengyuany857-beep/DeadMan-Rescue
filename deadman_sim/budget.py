import uuid

from . import params


def fresh_budget():
    return dict(incident_id=str(uuid.uuid4()), max=params.RTC_RESCUE_MAX,
                used=0, reserved=0, session_id=None, session_boot_id=None,
                settled_boot_ids=[], stable_boot_id=None, stable_since=None, cooldown_since=None)


def reserve(budget, boot_id):
    result = dict(fresh_budget(), **budget)
    if result["session_boot_id"] == boot_id and result["reserved"]:
        return result
    if result["used"] + result["reserved"] >= result["max"]:
        raise ValueError("budget exhausted")
    result.update(reserved=1, session_boot_id=boot_id, session_id=str(uuid.uuid4()))
    return result


def settle(budget, boot_id, conclusion):
    result = dict(fresh_budget(), **budget)
    if boot_id in result["settled_boot_ids"]:
        return result
    if conclusion not in ("REBOOT_OK", "OFF_MANUAL"):
        result["used"] += result["reserved"]
    result.update(reserved=0, session_id=None, session_boot_id=None,
                  settled_boot_ids=result["settled_boot_ids"] + [boot_id],
                  stable_boot_id=boot_id, stable_since=None, cooldown_since=None)
    return result
