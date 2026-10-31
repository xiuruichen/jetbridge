"""Block / challenge detection.

Bot managers rarely say "you are blocked" plainly. We classify by status code
plus body markers. A misclassified block that looks like "no availability"
would silently destroy comprehensiveness, so adapters must also assert a
positive marker of a genuine response (see ``require_marker``).
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
_CHALLENGE_RE = re.compile("|".join(_CHALLENGE_MARKERS), re.I)


def classify(raw: RawResponse, require_marker: str | None = None) -> tuple[Outcome, str | None]:
    s = raw.status
    if s is None:
        return Outcome.ERROR, "no status (network failure)"
    if s == 429:
        return Outcome.RATE_LIMITED, "HTTP 429"
    if s in (401, 403):
        return Outcome.BLOCKED, f"HTTP {s}"
    head = raw.body[:20000]
    if s >= 500:
        return Outcome.ERROR, f"HTTP {s}"
    if _CHALLENGE_RE.search(head) and (require_marker is None or require_marker not in raw.body):
        return Outcome.BLOCKED, "challenge page markers"
    if require_marker is not None and require_marker not in raw.body:
        return Outcome.ERROR, f"expected marker {require_marker!r} absent (len={len(raw.body)})"
    return Outcome.OK, None
