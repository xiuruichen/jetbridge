"""``python -m jetbridge.crawl [--minutes N] [--once]`` (spawned detached by the CLI/MCP)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys

from jetbridge.paths import var_dir


def log_path():
    d = var_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "crawler.log"


def spawn(minutes: float | None = None, once: bool = False) -> int:
    log = open(log_path(), "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    args = [sys.executable, "-m", "jetbridge.crawl"]
    if minutes is not None:
        args += ["--minutes", str(minutes)]
    if once:
        args.append("--once")
    return subprocess.Popen(args, **kwargs).pid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-queries", type=int, default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from jetbridge.crawl.crawler import crawl

    totals = asyncio.run(crawl(minutes=a.minutes, once=a.once, max_queries=a.max_queries))
    logging.info("crawler finished: %s", totals)


if __name__ == "__main__":
    main()
