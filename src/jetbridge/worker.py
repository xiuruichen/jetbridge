"""Background worker: ``python -m jetbridge.worker <search_id>``.

Searches take tens of minutes, far longer than an MCP tool call may block, so
the MCP server spawns this detached process and clients poll ``search_status``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from jetbridge.paths import var_dir


def log_path(sid: str):
    d = var_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.log"


def spawn(sid: str) -> int:
    """Start a detached worker for ``sid``; returns its PID."""
    log = open(log_path(sid), "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-m", "jetbridge.worker", sid], **kwargs)
    return proc.pid


def main() -> None:
    sid = sys.argv[1]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from jetbridge.runner import _set, run_search

    _set(sid, worker_pid=os.getpid())
    path = asyncio.run(run_search(sid))
    logging.info("report written: %s", path)


if __name__ == "__main__":
    main()
