"""Keeping this PC awake while it's plugged in, because checks can't run while it sleeps.

This only ever changes the plugged-in timers, only after someone says yes, and remembers what they
were so they can be put back.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from jetbridge.store import Store

_PREVIOUS = "keep_awake_previous"
_INDEX = re.compile(r":\s*(0x[0-9a-fA-F]+)\s*$")
_CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class PluggedInTimers:
    """Seconds until sleep and hibernate while plugged in. Zero means never."""

    sleep_s: int
    hibernate_s: int

    @property
    def stays_awake(self) -> bool:
        return self.sleep_s == 0 and self.hibernate_s == 0


def _powercfg(*args: str) -> str:
    result = subprocess.run(
        ["powercfg", *args],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or result.stdout.strip() or "powercfg failed")
    return result.stdout


def parse_plugged_in(output: str) -> int:
    """The plugged-in value from ``powercfg /query``. Windows words this in the PC's language, so
    this goes by position: the last two values are the plugged-in one and then the battery one."""
    values = [m[1] for line in output.splitlines() if (m := _INDEX.search(line))]
    if len(values) < 2:
        raise ValueError("powercfg's answer didn't have the expected values")
    return int(values[-2], 16)


def plugged_in_timers() -> PluggedInTimers | None:
    """None if this PC won't say, which isn't worth worrying anyone about."""
    try:
        return PluggedInTimers(
            parse_plugged_in(_powercfg("/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE")),
            parse_plugged_in(_powercfg("/query", "SCHEME_CURRENT", "SUB_SLEEP", "HIBERNATEIDLE")),
        )
    except (OSError, ValueError):
        return None


def _set_minutes(setting: str, seconds: int) -> None:
    # Windows takes whole minutes here, and rounding down to zero would mean "never".
    _powercfg("/change", setting, str((seconds + 59) // 60))


def keep_awake(store: Store) -> None:
    """Never sleep or hibernate while plugged in. Raises ``OSError`` if Windows refuses."""
    timers = plugged_in_timers()
    if timers is None:
        raise OSError("Windows wouldn't say what its sleep settings are")
    if timers.stays_awake:
        return
    if store.get(_PREVIOUS) is None:
        saved = {"sleep_s": timers.sleep_s, "hibernate_s": timers.hibernate_s}
        store.set(_PREVIOUS, json.dumps(saved))
    _set_minutes("standby-timeout-ac", 0)
    _set_minutes("hibernate-timeout-ac", 0)


def restore(store: Store) -> bool:
    """Put back the timers from before ``keep_awake``. False if jetbridge never changed them."""
    saved = store.get(_PREVIOUS)
    if saved is None:
        return False
    previous = json.loads(saved)
    _set_minutes("standby-timeout-ac", previous["sleep_s"])
    _set_minutes("hibernate-timeout-ac", previous["hibernate_s"])
    store.delete(_PREVIOUS)
    return True
