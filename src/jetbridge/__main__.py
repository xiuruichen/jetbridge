"""``python -m jetbridge``. Under pythonw (the scheduled task) there is no console, so output
goes to the log file."""

from __future__ import annotations

import sys

from jetbridge.paths import log_path

if sys.stdout is None or sys.stderr is None:
    path = log_path()
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(path.with_suffix(".old.log"))
    log = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log

from jetbridge.cli import app

app(prog_name="jetbridge")
