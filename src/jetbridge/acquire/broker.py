"""One Chrome session at a time, with a separate persistent profile for each site.

Recon showed headless browsers are rejected on these sites, so this drives the installed
Google Chrome, visible but placed off-screen, with its own profile separate from your normal
Chrome. It never logs in and never attempts to solve a challenge.
"""

from __future__ import annotations

import contextlib
import os

from patchright.async_api import BrowserContext, Page, Playwright, async_playwright

from jetbridge.paths import profile_dir


class SessionBroker:
    def __init__(self) -> None:
        self.show = os.environ.get("JETBRIDGE_SHOW_BROWSER") == "1"
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._site: str | None = None

    def is_open(self, site: str) -> bool:
        return self._site == site and self._page is not None and not self._page.is_closed()

    async def page(self, site: str, warmup_url: str, settle_ms: int = 6000) -> Page:
        if self.is_open(site):
            return self._page  # type: ignore[return-value]
        await self.close()
        self._pw = await async_playwright().start()
        args = [] if self.show else ["--window-position=-2400,-2400", "--window-size=1400,1000"]
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(site)),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=args,
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._site = site
        await self._page.goto(warmup_url, wait_until="domcontentloaded", timeout=90_000)
        await self._page.wait_for_timeout(settle_ms)
        return self._page

    async def close(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._ctx, self._pw, self._page, self._site = None, None, None, None
        if ctx is not None:
            with contextlib.suppress(Exception):
                await ctx.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()
