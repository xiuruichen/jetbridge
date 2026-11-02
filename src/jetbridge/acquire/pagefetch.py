"""A fetch made from inside a warmed page, using its real cookies and browser networking."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from jetbridge.acquire.types import RawResponse

if TYPE_CHECKING:
    from patchright.async_api import Page

REQUEST_TIMEOUT_MS = 90_000

# Each site calls this before every request it makes, so no request can skip the governor.
Gate = Callable[[], Awaitable[None]]

_FETCH_JS = """async ({url, method, headers, body, timeoutMs}) => {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {method, headers, body, credentials: 'include', signal: ctl.signal});
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: null, text: String(e)};
  } finally { clearTimeout(t); }
}"""


async def page_fetch(
    page: Page,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> RawResponse:
    result: dict[str, Any] = await page.evaluate(
        _FETCH_JS,
        {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
            "timeoutMs": REQUEST_TIMEOUT_MS,
        },
    )
    return RawResponse(status=result["status"], body=result["text"] or "", tier="T1")
