"""What can be learned about a Mac without airline sites or a person. Run on GitHub's Mac machines.

    uv run python tools/mac_smoke.py

It never contacts an airline, and it leaves nothing installed. A real desktop may differ.
"""

from __future__ import annotations

import asyncio
import os
import sys

from jetbridge import autorun, power, service
from jetbridge.acquire.broker import OFF_SCREEN_ARGS

PAGE = "data:text/html,<title>smoke</title>"


def background_task() -> str:
    try:
        autorun.install()
        loaded = autorun.is_installed()
        autorun.remove()
        return f"installed={loaded} removed={not autorun.is_installed()}"
    except RuntimeError as exc:
        return f"couldn't run here: {exc}"


def keep_awake() -> str:
    store = service.open_store()
    try:
        power.keep_awake(store)
        on = power.plugged_in_timers()
        power.restore(store)
        off = power.plugged_in_timers()
        return f"on={on} off={off}"
    except (OSError, RuntimeError) as exc:
        return f"couldn't run here: {exc}"


async def chrome_window() -> str:
    from patchright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="chrome", headless=False, args=OFF_SCREEN_ARGS
        )
        page = await browser.new_page()
        await page.goto(PAGE)
        position = await page.evaluate(
            "[window.screenX, window.screenY, screen.width, screen.height]"
        )
        await browser.close()
    x, y, width, height = position
    off = x + 1400 <= 0 or y + 1000 <= 0 or x >= width or y >= height
    return f"window at {x},{y} on a {width}x{height} screen: {'off' if off else 'ON'} screen"


def show(text: str) -> None:
    # As an annotation on GitHub, which can be read without signing in.
    print(f"::notice title=Mac smoke test::{text}" if os.environ.get("GITHUB_ACTIONS") else text)


def main() -> None:
    show(f"platform: {sys.platform}")
    show(f"background task: {background_task()}")
    show(f"keep awake: {keep_awake()}")
    try:
        show(f"chrome: {asyncio.run(chrome_window())}")
    except Exception as exc:  # the point is to report what happens here, whatever that is
        show(f"chrome: couldn't run here: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
