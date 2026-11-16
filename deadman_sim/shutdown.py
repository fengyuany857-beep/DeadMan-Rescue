import re


NORMAL_REASONS = frozenset({"userrequested", "shell", "normal"})
MAINTENANCE_TARGETS = frozenset({"recovery", "bootloader", "fastboot", "sideload"})


def parse_powerctl(value):
    if value is None or value == "":
        return {"action": "stop", "target": None, "raw": value}
    fallback = {"action": "maintenance", "target": "unknown", "raw": value}
    if not isinstance(value, str) or len(value) > 128:
        return fallback
    action, separator, reason = value.partition(",")
    if separator and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", reason):
        return fallback
    if action == "shutdown":
        return {"action": "off", "target": reason or None, "raw": value}
    if action == "reboot":
        if not separator or reason in NORMAL_REASONS:
            return {"action": "reboot", "target": reason or None, "raw": value}
        if reason in MAINTENANCE_TARGETS:
            return {"action": "maintenance", "target": reason, "raw": value}
    return fallback
