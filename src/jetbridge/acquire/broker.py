"""Session broker: one persistent, real-Chrome context per site.

Recon showed headless Chromium is rejected outright by Akamai-protected sites
(net::ERR_HTTP2_PROTOCOL_ERROR on united.com). We therefore drive the user's
installed Google Chrome, headful, with a persistent per-site profile so the
bot-manager state (and opt-in logins) survive across runs. The window is
placed off-screen by default so it does not get in the way.

Adapters never touch Playwright directly; they ask the broker for a warmed
page and run in-page ``fetch`` (tier T1) or form posts through it.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

from patchright.async_api import BrowserContext, Page, Playwright, async_playwright

from jetbridge.paths import profile_dir


@dataclass
class _Session:
    ctx: BrowserContext
    page: Page
    warmed_at: float
    requests: int = 0


class SessionBroker:
    def __init__(
        self,
        *,
        channel: str = "chrome",
        offscreen: bool | None = None,
        max_requests_per_session: int = 150,
        max_session_age_s: float = 1800,
    ):
        self.channel = channel
        if offscreen is None:
            offscreen = os.environ.get("JETBRIDGE_SHOW_BROWSER", "0") != "1"
        self.offscreen = offscreen
        self.max_requests = max_requests_per_session
        self.max_age = max_session_age_s
        self._pw: Playwright | None = None
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> SessionBroker:
        self._pw = await async_playwright().start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        for site in list(self._sessions):
            await self.close(site)
        if self._pw:
            await self._pw.stop()

    def _lock(self, site: str) -> asyncio.Lock:
        return self._locks.setdefault(site, asyncio.Lock())

    async def page(self, site: str, warmup_url: str, settle_ms: int = 6000) -> Page:
        """Return a warmed page for ``site``, (re)creating the session if stale."""
        async with self._lock(site):
            sess = self._sessions.get(site)
            stale = sess is not None and (
                sess.requests >= self.max_requests
                or time.monotonic() - sess.warmed_at > self.max_age
            )
            if sess is not None and (stale or sess.page.is_closed()):
                await self._close_unlocked(site)
                sess = None
            if sess is None:
                sess = await self._open(site, warmup_url, settle_ms)
                self._sessions[site] = sess
            sess.requests += 1
            return sess.page

    async def _open(self, site: str, warmup_url: str, settle_ms: int) -> _Session:
        assert self._pw is not None, "use SessionBroker as an async context manager"
        args = (
            ["--window-position=-2400,-2400", "--window-size=1400,1000"] if self.offscreen else []
        )
        ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(site)),
            channel=self.channel,
            headless=False,
            no_viewport=True,
            args=args,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(warmup_url, wait_until="domcontentloaded", timeout=90_000)
        # Let bot-manager sensor scripts run and set their cookies.
        await page.wait_for_timeout(settle_ms)
        return _Session(ctx=ctx, page=page, warmed_at=time.monotonic())

    async def wait_for_human(self, site: str, timeout_s: float = 300) -> bool:
        """If the browser is visible, give the user time to solve a challenge in the
        open tab. Returns True when the page no longer looks like a challenge.
        jetbridge never solves CAPTCHAs itself."""
        sess = self._sessions.get(site)
        if self.offscreen or sess is None or sess.page.is_closed():
            return False
        from jetbridge.acquire.detect import _CHALLENGE_RE

        print(
            f"[jetbridge] {site}: human verification shown in the Chrome window; "
            f"solve it there (waiting up to {int(timeout_s)}s)...",
            flush=True,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            try:
                html = await sess.page.content()
            except Exception:
                continue
            if not _CHALLENGE_RE.search(html[:50000]):
                return True
        return False

    async def rotate(self, site: str) -> None:
        """Drop the session (after a block); the next ``page()`` call re-warms."""
        async with self._lock(site):
            await self._close_unlocked(site)

    async def close(self, site: str) -> None:
        async with self._lock(site):
            await self._close_unlocked(site)

    async def _close_unlocked(self, site: str) -> None:
        sess = self._sessions.pop(site, None)
        if sess is not None:
            try:
                await sess.ctx.close()
            except Exception:
                pass


# JS helper executed inside the page: same-origin/CORS fetch with the page's
# real cookies, TLS fingerprint and bot-manager headers.
FETCH_JS = """async ({url, method, headers, body, timeoutMs}) => {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {method, headers, body, credentials: 'include', signal: ctl.signal});
    const text = await r.text();
    return {status: r.status, text, headers: Object.fromEntries(r.headers.entries())};
  } catch (e) {
    return {status: null, text: String(e), headers: {}};
  } finally { clearTimeout(t); }
}"""


async def page_fetch(
    page: Page,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_ms: int = 60_000,
) -> dict[str, Any]:
    return await page.evaluate(
        FETCH_JS,
        {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
            "timeoutMs": timeout_ms,
        },
    )
