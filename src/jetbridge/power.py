"""Keeping this computer awake while it's plugged in, because checks can't run while it sleeps.

This only ever changes the plugged-in timers, only after someone says yes, and remembers what they
were so they can be put back.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from jetbridge import launchd, platforms
from jetbridge.store import Store

_PREVIOUS = "keep_awake_previous"
_INDEX = re.compile(r":\s*(0x[0-9a-fA-F]+)\s*$")
_MAC_ON = "mac"


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
        **platforms.quiet(),
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or result.stdout.strip() or "powercfg failed")
    return result.stdout


def _pmset() -> str:
    return subprocess.run(
        ["pmset", "-g", "custom"], capture_output=True, text=True, errors="replace", check=True
    ).stdout


def parse_pmset(output: str) -> int:
    """Seconds until a Mac sleeps on mains power, from ``pmset -g custom``. Zero means never."""
    in_mains = False
    for line in output.splitlines():
        if line.strip().endswith("Power:"):
            in_mains = line.strip().startswith("AC")
        match = re.match(r"\s*sleep\s+(\d+)", line)
        if in_mains and match:
            return int(match[1]) * 60
    raise ValueError("pmset's answer didn't have the expected values")


def parse_plugged_in(output: str) -> int:
    """The plugged-in value from ``powercfg /query``. Windows words this in its own language, so
    this goes by position: the last two values are the plugged-in one and then the battery one."""
    values = [m[1] for line in output.splitlines() if (m := _INDEX.search(line))]
    if len(values) < 2:
        raise ValueError("powercfg's answer didn't have the expected values")
    return int(values[-2], 16)


def plugged_in_timers() -> PluggedInTimers | None:
    """None if this computer won't say, which isn't worth worrying anyone about."""
    if platforms.IS_MAC:
        try:
            sleep_s = 0 if launchd.keeping_awake() else parse_pmset(_pmset())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return PluggedInTimers(sleep_s, 0)
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
    """Never sleep or hibernate while plugged in. Raises ``OSError`` if that's refused."""
    timers = plugged_in_timers()
    if timers is None:
        raise OSError("this computer wouldn't say what its sleep settings are")
    if timers.stays_awake:
        return
    if platforms.IS_MAC:
        try:
            launchd.start_keep_awake()
        except RuntimeError as exc:
            raise OSError(str(exc)) from exc
        store.set(_PREVIOUS, _MAC_ON)
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
    if saved == _MAC_ON:
        launchd.stop_keep_awake()
        store.delete(_PREVIOUS)
        return True
    previous = json.loads(saved)
    _set_minutes("standby-timeout-ac", previous["sleep_s"])
    _set_minutes("hibernate-timeout-ac", previous["hibernate_s"])
    store.delete(_PREVIOUS)
    return True
