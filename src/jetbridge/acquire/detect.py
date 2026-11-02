"""Block and challenge detection.

Bot managers rarely say "you are blocked" plainly, so classification uses the status code
plus body markers, and a genuine response must also contain an adapter's positive marker.
A block misread as "no availability" would be the worst possible bug for a watcher.
"""

from __future__ import annotations

import re

from jetbridge.acquire.types import Outcome, RawResponse

_CHALLENGE_MARKERS = [
    r"_abck",  # Akamai sensor cookie name echoed in challenge pages
    r"bm-verify",
    r"/_sec/cp_challenge",
    r"px-captcha",
    r"PerimeterX",
    r"captcha-delivery\.com",  # DataDome
    r"cf-chl-",  # Cloudflare
    r"Access Denied</title>",
    r"Pardon Our Interruption",
    r"Request unsuccessful\. Incapsula",
    r"errors\.edgesuite\.net",
    r"<title>\s*Challenge Validation",  # aa.com interactive challenge (seen 2026-10-31)
    r"Please confirm you(?:'|&#39;|’)re a human",
    r"I(?:'|&#39;|’)m not a robot",
]
CHALLENGE_RE = re.compile("|".join(_CHALLENGE_MARKERS), re.I)


def classify(raw: RawResponse, markers: str | tuple[str, ...]) -> tuple[Outcome, str | None]:
    wanted = (markers,) if isinstance(markers, str) else markers
    status = raw.status
    if status is None:
        return Outcome.ERROR, "no status (network failure)"
    if status == 429:
        return Outcome.RATE_LIMITED, "HTTP 429"
    if status in (401, 403, 444):  # Akamai answered Delta with 444 when it blocked (2026-10-31)
        return Outcome.BLOCKED, f"HTTP {status}"
    if status >= 500:
        return Outcome.ERROR, f"HTTP {status}"
    has_marker = any(marker in raw.body for marker in wanted)
    if not has_marker and CHALLENGE_RE.search(raw.body[:50_000]):
        return Outcome.BLOCKED, "challenge page markers"
    if not has_marker:
        return Outcome.ERROR, f"expected marker {wanted[0]!r} absent (len={len(raw.body)})"
    return Outcome.OK, None
