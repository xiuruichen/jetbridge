"""Turn a query into a classified answer. Every request, including the browser's first page
load and the navigation fallback, passes through the governor first."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from jetbridge.acquire.broker import SessionBroker
from jetbridge.acquire.detect import classify
from jetbridge.acquire.governor import Governor, GovernorDeferred
from jetbridge.acquire.sites import get_site
from jetbridge.acquire.types import Outcome, RawResponse, SchemaDrift
from jetbridge.models import AnswerKind, Query, SearchAnswer
from jetbridge.paths import raw_dir

KEEP_RAW = 50


class Fetcher(Protocol):
    async def search(self, query: Query, deadline: float) -> SearchAnswer: ...

    async def close(self) -> None: ...


def interpret(
    raw: RawResponse, governor: Governor, site: str, save: Callable[[RawResponse, str], None]
) -> SearchAnswer | None:
    """Classify a response. Returns None when it merely wasn't the expected page."""
    details = get_site(site)
    raw.meta["site"] = site
    outcome, detail = classify(raw, details.markers)
    if outcome in (Outcome.BLOCKED, Outcome.RATE_LIMITED):
        save(raw, "blocked")
        until = governor.record_block(site)
        return SearchAnswer(AnswerKind.BLOCKED, detail=detail, retry_at=until)
    if outcome is Outcome.ERROR:
        return None
    try:
        parsed = details.parse(raw.body)
    except SchemaDrift as exc:
        save(raw, "drift")
        return SearchAnswer(AnswerKind.DRIFT, detail=str(exc))
    governor.record_ok(site)
    if parsed.site_error or parsed.unreadable:
        save(raw, "partial")
    elif os.environ.get("JETBRIDGE_KEEP_RAW") == "1":  # for recording test fixtures
        save(raw, "ok")
    return SearchAnswer(AnswerKind.ANSWER, parsed=parsed)


def save_raw(raw: RawResponse, label: str) -> None:
    """Keep failed responses so a broken parser can be repaired against real data."""
    folder = raw_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    site = raw.meta.get("site", "site")
    (folder / f"{stamp}-{site}-{label}-{raw.tier}.html").write_text(
        raw.body, encoding="utf-8", errors="replace"
    )
    files = sorted(folder.glob("*.html"))
    for old in files[: max(0, len(files) - KEEP_RAW)]:
        old.unlink(missing_ok=True)


_WHITESPACE = re.compile(r"\s+")


class LiveFetcher:
    """Searches any site through one Chrome session, which is opened only when needed."""

    def __init__(self, governor: Governor, broker: SessionBroker | None = None):
        self.governor = governor
        self.broker = broker or SessionBroker()

    async def close(self) -> None:
        await self.broker.close()

    async def search(self, query: Query, deadline: float) -> SearchAnswer:
        site = query.program
        details = get_site(site)

        async def gate() -> None:
            await self.governor.acquire(site, deadline)

        try:
            if not self.broker.is_open(site):
                await gate()  # opening loads the site's home page
            page = await self.broker.page(site, details.warmup_url)

            raw = await details.fetch(page, query, gate)
            answer = interpret(raw, self.governor, site, save_raw)
            if answer is not None:
                return answer
            if raw.status is None or raw.status >= 500:
                return SearchAnswer(AnswerKind.ERROR, detail=classify(raw, details.markers)[1])
            if not raw.body.strip():
                # Seen from united.com for dates too far ahead. It says nothing about the page
                # format, so it mustn't raise a format warning, and it isn't "no seats" either.
                return SearchAnswer(
                    AnswerKind.ERROR, detail=f"{details.domain} sent an empty reply"
                )

            # Status looked fine but it isn't the results page: try the real page flow once.
            if details.fallback is not None:
                await gate()
                raw = await details.fallback(page, query)
                raw.meta["site"] = site
                answer = interpret(raw, self.governor, site, save_raw)
                if answer is not None:
                    return answer
            detail = classify(raw, details.markers)[1]
            if raw.status is None or raw.status >= 500:
                return SearchAnswer(AnswerKind.ERROR, detail=detail)
            save_raw(raw, "unexpected")
            return SearchAnswer(AnswerKind.DRIFT, detail=f"unexpected page: {detail}")
        except GovernorDeferred as exc:
            return SearchAnswer(AnswerKind.DEFERRED, detail=exc.why, retry_at=exc.retry_at)
        except Exception as exc:  # browser crash, timeout, profile in use
            await self.broker.close()
            message = _WHITESPACE.sub(" ", str(exc))[:300]
            return SearchAnswer(AnswerKind.ERROR, detail=f"{type(exc).__name__}: {message}")
