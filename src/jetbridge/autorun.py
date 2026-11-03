"""Running a check every 15 minutes, using whatever this computer has for that."""

from __future__ import annotations

from jetbridge import launchd, platforms, winsched


def _backend():
    return launchd if platforms.IS_MAC else winsched


def task_name() -> str:
    return _backend().TASK_NAME


def is_installed() -> bool:
    return _backend().is_installed()


def install() -> None:
    _backend().install()


def remove() -> None:
    _backend().remove()
