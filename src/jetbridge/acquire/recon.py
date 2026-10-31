"""Endpoint reconnaissance.

Opens a real (stealth) browser on a URL, optionally lets a human drive it, and
records every XHR/fetch exchange that returns JSON: method, URL, request
headers/body, status and response body. The capture is what we use to
(re)discover each airline's internal search endpoints when they drift.

Output: ``var/recon/<site>/<timestamp>.jsonl`` (one exchange per line).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from patchright.async_api import Request, Response, async_playwright

from jetbridge.paths import profile_dir, var_dir

_INTERESTING_TYPES = {"xhr", "fetch"}
_MAX_BODY = 2_000_000


@dataclass
class ReconResult:
    path: Path
    exchanges: int
    final_url: str
    title: str
    statuses: dict[int, int] = field(default_factory=dict)


async def run_recon(
    site: str,
    url: str,
    *,
    headless: bool = True,
    dwell_seconds: float = 25.0,
    interactive: bool = False,
    warmup_url: str | None = None,
    channel: str = "chrome",
) -> ReconResult:
    out_dir = var_dir() / "recon" / site
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    statuses: dict[int, int] = {}
    count = 0
    pending: list[asyncio.Task[None]] = []

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(site)),
            channel=channel,
            headless=headless,
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        fh = out_path.open("w", encoding="utf-8")

        async def record(resp: Response) -> None:
            nonlocal count
            req: Request = resp.request
            statuses[resp.status] = statuses.get(resp.status, 0) + 1
            if req.resource_type not in _INTERESTING_TYPES:
                return
            ctype = (await resp.all_headers()).get("content-type", "")
            body: str | None = None
            try:
                raw = await resp.body()
                if len(raw) <= _MAX_BODY:
                    body = raw.decode("utf-8", errors="replace")
            except Exception as exc:  # body unavailable (redirect, aborted)
                body = f"<<unavailable: {exc}>>"
            fh.write(
                json.dumps(
                    {
                        "t": time.time(),
                        "method": req.method,
                        "url": req.url,
                        "status": resp.status,
                        "content_type": ctype,
                        "req_headers": await req.all_headers(),
                        "req_body": req.post_data,
                        "body": body,
                    }
                )
                + "\n"
            )
            count += 1

        page.on("response", lambda r: pending.append(asyncio.ensure_future(record(r))))

        if warmup_url:
            await page.goto(warmup_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(6_000)
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        if interactive:
            print("Drive the browser; press Enter here when the results are visible...")
            await asyncio.get_running_loop().run_in_executor(None, input)
        else:
            await page.wait_for_timeout(int(dwell_seconds * 1000))
        if pending:  # bounded: streaming bodies may never complete
            await asyncio.wait(pending, timeout=15)
        final_url, title = page.url, await page.title()
        await page.screenshot(path=str(out_path.with_suffix(".png")), full_page=False)
        fh.close()
        await ctx.close()

    return ReconResult(out_path, count, final_url, title, statuses)
