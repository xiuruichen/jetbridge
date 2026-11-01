"""MCP server: exposes jetbridge's deterministic engine to Claude Code / Desktop.

Run: ``jetbridge-mcp`` (stdio). Claude turns the user's request into a
SearchSpec, asks clarifying questions, starts the search, polls, and narrates
the results, quoting option IDs and engine numbers only.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from jetbridge.models.spec import Profile, SearchSpec

INSTRUCTIONS = """\
jetbridge is a decision engine for award (miles) + cash flights. It searches airline sites
directly, optimizes how to seat everyone under the user's balances and preferences, verifies
the best options live, and returns a DECISION BRIEF (recommendation, Pareto frontier,
trade-offs, sensitivity, action plan). Your job is to be the expert advisor on top of it:
elicit preferences, interrogate the results, run follow-ups, and recommend.

1. FRAME THE PROBLEM (ask instead of assuming)
   - One-way or round trip (return window)? Must the party fly together (same_flights /
     same_day / any)? Lowest acceptable cabin (min_cabin)? Drive to other airports
     (nearby_origins)? Destinations: airports, metros (TYO, OSA, SEL, SHA, BJS) or regions
     (ASIA_EAST, ASIA_SOUTHEAST, ASIA); weight 1.0 for stated targets, 0.6-0.8 for
     "also acceptable".
   - Preferences (SearchSpec.preferences, USD per passenger): how much a lie-flat seat is
     worth PER FLOWN HOUR (cabin_premium_usd_per_hour; default business $130/h), value of
     time, tolerance for positioning on a separate ticket, risk_aversion (0-1). Translate
     plain answers ("business really matters on the long flight", "we hate overnight
     layovers") into these numbers and tell the user what you set.
   - Balances: get_profile; if missing, ask for approximate balances incl. card points
     (amex_mr, chase_ur, citi_ty, capital_one, bilt) and save with set_profile.
2. LOOK FIRST, THEN SEARCH (seats.aero model):
   - cached_availability gives an INSTANT answer from the availability store (every past
     search and the background crawler feed it). Use it to orient the user in seconds;
     cells carry their age and are NOT live-verified.
   - plan_search -> report query count, how many are served from cache, ETA, excluded
     programs and any site cooling down -> start_search -> poll search_status. The engine
     refines automatically (split-seat completion, gateway long-haul, cash positioning).
   - Sites are rate-governed: a block cools that site down for every process instead of
     hammering it; affected queries are DEFERRED, and every search becomes a watch the
     background crawler fills. If coverage has gaps, offer start_crawler and a re-run
     later (served mostly from cache, so fast). site_health shows budgets/cooldowns.
   - alerts lists changes seen between snapshots (new space, price drops, more seats).
3. REASON over get_brief (not just get_results):
   - Lead with the recommendation and WHY (utility, redemption cpp vs cash, risk).
   - Walk the trade-offs: each alternative's tradeoff line is exact; use it.
   - Use explain_option / compare_options before claiming why one option beats another.
   - Stress-test: if the pick depends on a valuation (see sensitivity) or on balances, run
     what_if (different cpp, risk_aversion, together='any', card-point balances) and report
     whether the recommendation changes.
   - Close gaps with targeted_search when evidence suggests value: preferred cabin has
     2 of 3 seats (try the adjacent day, or a 1-pax search on another flight), a coverage
     gap on a key route, a promising gateway date. Keep it to a few queries.
   - Be candid: if the best redemption is poor vs cash, say "pay cash"; if no option seats
     everyone in business, say so and show the best mixed-cabin or split plan.
4. ACT: present the action plan in order (verify -> transfer -> book scarcest first ->
   positioning). Transfers are IRREVERSIBLE; never advise transferring before space is
   re-confirmed.

Accuracy rules (strict)
- Every number (miles, taxes, cash, seats, times, flight numbers, cpp) must come from tool
  output, cited with its option ID. Never estimate, round or invent prices.
- State verification status and observed time; report coverage % and gaps.
- Distinguish facts (engine output) from your judgement (clearly labelled).
"""

server = MCPServer(name="jetbridge", instructions=INSTRUCTIONS)


def _profile() -> Profile:
    from jetbridge.store.db import kv_get

    return Profile.model_validate(kv_get("profile", {}))


@server.tool()
def get_profile() -> dict[str, Any]:
    """Current balances (miles and card points), cents-per-point overrides and login opt-ins."""
    from jetbridge.kb import load_kb

    kb = load_kb()
    p = _profile()
    return {
        "profile": p.model_dump(mode="json"),
        "known_currencies": kb.currencies,
        "default_cpp": {k: str(v) for k, v in kb.cpp.items()},
    }


@server.tool()
def set_profile(
    balances: dict[str, int] | None = None,
    cpp_overrides: dict[str, float] | None = None,
    logins_enabled: list[str] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Update balances (currency id -> amount), cpp overrides and login opt-ins.
    Merges into the existing profile unless replace=true."""
    from jetbridge.kb import load_kb
    from jetbridge.store.db import kv_set

    kb = load_kb()
    data = Profile().model_dump(mode="json") if replace else _profile().model_dump(mode="json")
    for k, v in (balances or {}).items():
        if k not in kb.currencies:
            raise ValueError(f"unknown currency {k!r}; known: {sorted(kb.currencies)}")
        data["balances"][k] = int(v)
    for k, v in (cpp_overrides or {}).items():
        data["cpp_overrides"][k] = str(v)
    for a in logins_enabled or []:
        if a not in data["logins_enabled"]:
            data["logins_enabled"].append(a)
    prof = Profile.model_validate(data)
    kv_set("profile", prof.model_dump(mode="json"))
    return prof.model_dump(mode="json")


@server.tool()
def plan_search(spec: SearchSpec, max_queries_per_adapter: int = 2000) -> dict[str, Any]:
    """Create a search from a SearchSpec and return its plan (queries, estimated minutes,
    programs excluded and why). Does not start it; call start_search with the search_id."""
    from jetbridge.runner import create_search

    sid, plan = create_search(spec, _profile(), max_queries_per_adapter=max_queries_per_adapter)
    return {"search_id": sid, "plan": plan.summary()}


@server.tool()
def start_search(search_id: str) -> dict[str, Any]:
    """Start a planned search in a background worker. Returns immediately; poll search_status."""
    from jetbridge.store.db import SearchRow, session
    from jetbridge.worker import log_path, spawn

    with session() as s:
        row = s.get(SearchRow, search_id)
        if row is None:
            raise ValueError(f"unknown search {search_id}")
        if row.status == "running":
            return {"search_id": search_id, "status": "already running"}
    pid = spawn(search_id)
    return {
        "search_id": search_id,
        "status": "started",
        "worker_pid": pid,
        "log": str(log_path(search_id)),
    }


@server.tool()
def search_status(search_id: str) -> dict[str, Any]:
    """Status, current stage, and coverage-ledger progress of a search."""
    from jetbridge.runner import ledger_stats
    from jetbridge.store.db import SearchRow, session

    with session() as s:
        row = s.get(SearchRow, search_id)
        if row is None:
            raise ValueError(f"unknown search {search_id}")
        info = {
            "search_id": row.id,
            "status": row.status,
            "stage": row.stage,
            "error": row.error,
            "report_path": row.report_path,
            "plan": row.plan,
        }
    counts = ledger_stats(search_id)
    total = sum(sum(v.values()) for v in counts.values())
    finished = sum(n for v in counts.values() for k, n in v.items() if k != "pending")
    info["ledger"] = counts
    info["progress_pct"] = round(100 * finished / total, 1) if total else 0.0
    return info


@server.tool()
def get_results(search_id: str, limit: int = 8) -> dict[str, Any]:
    """Top options (with option IDs, per-leg flights, miles, taxes, seats, verification) plus
    coverage summary for a finished search."""
    from pathlib import Path

    from jetbridge.store.db import SearchRow, session

    with session() as s:
        row = s.get(SearchRow, search_id)
        if row is None or not row.report_path:
            raise ValueError("search not finished (no report yet)")
        report = Path(row.report_path)
    summary = json.loads((report.parent / "summary.json").read_text(encoding="utf-8"))
    summary["options"] = summary["options"][:limit]
    summary["report_markdown"] = str(report)
    summary["report_html"] = str(report.parent / "report.html")
    return summary


@server.tool()
def list_searches(limit: int = 10) -> list[dict[str, Any]]:
    """Recent searches."""
    from jetbridge.store.db import SearchRow, session

    with session() as s:
        rows = list(s.scalars(select(SearchRow).order_by(SearchRow.created_at.desc()).limit(limit)))
        return [
            {
                "search_id": r.id,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
                "plan": r.plan,
            }
            for r in rows
        ]


@server.tool()
def kb_program(program: str) -> dict[str, Any]:
    """Knowledge-base facts for a program: partner airlines, transfer sources, notes, freshness."""
    from jetbridge.kb import load_kb

    kb = load_kb()
    p = kb.programs[program]
    return {
        "program": p.model_dump(mode="json"),
        "funding_sources": [t.model_dump(mode="json") for t in kb.funding_sources(program)],
        "last_verified": {k: str(v) for k, v in kb.last_verified.items()},
    }


@server.tool()
def get_brief(search_id: str) -> dict[str, Any]:
    """The decision brief of a finished search: recommendation, Pareto frontier,
    alternatives with exact trade-offs, cpp sensitivity, action plan, insights."""
    from pathlib import Path

    from jetbridge.store.db import SearchRow, session

    with session() as s:
        row = s.get(SearchRow, search_id)
        if row is None or not row.report_path:
            raise ValueError("search not finished (no report yet)")
        report = Path(row.report_path)
    summary = json.loads((report.parent / "summary.json").read_text(encoding="utf-8"))
    plan = summary.get("plan") or {}
    return {
        "brief": summary.get("brief"),
        "coverage_pct": summary.get("coverage_pct"),
        "coverage": summary.get("coverage"),
        "plan_notes": plan.get("notes"),
        "excluded_programs": plan.get("excluded_programs"),
        "report_html": str(report.parent / "report.html"),
    }


@server.tool()
def explain_option(option_id: str) -> dict[str, Any]:
    """Everything about one option: per-passenger assignments, every segment, layovers
    (overnight/tight/airport change/separate ticket), comfort deficit, risk factors,
    redemption value vs cash, verification, and which option dominates it (if any)."""
    from jetbridge.store.db import OptionRow, session

    with session() as s:
        row = s.get(OptionRow, option_id)
        if row is None:
            raise ValueError(f"unknown option {option_id}")
        data = dict(row.data)
        data["status"] = row.status
        data["rank"] = row.rank
        data["search_id"] = row.search_id
    for a in data.get("assignments", []):
        segs = [sg for p in a.get("positioning", []) for sg in p["itinerary"]["segments"]]
        segs += a["offer"]["itinerary"]["segments"]
        a["flights"] = [
            f"{sg['marketing_carrier']}{sg['flight_number']} {sg['origin']}-{sg['destination']} "
            f"{sg['departs_at']} -> {sg['arrives_at']} {sg['cabin']}"
            for sg in segs
        ]
    return data


@server.tool()
def compare_options(option_a: str, option_b: str) -> dict[str, Any]:
    """Side-by-side comparison of two options on every decision dimension, with the
    exact differences (b minus a)."""
    a, b = explain_option(option_a), explain_option(option_b)
    da, db = a.get("analysis") or {}, b.get("analysis") or {}
    dims = ["utility_usd", "points_value_usd", "hours", "comfort_deficit_usd", "risk"]

    def side(oid: str, o: dict, d: dict) -> dict:
        return {
            "id": oid,
            "label": o.get("label"),
            "status": o["status"],
            "miles": o["miles_by_program"],
            "cash_usd": o["cash_usd"],
            **{k: d.get(k) for k in dims},
            "risk_reasons": d.get("risk_reasons"),
        }

    diff = {k: (round(db[k] - da[k], 3) if k in da and k in db else None) for k in dims}
    return {
        "a": side(option_a, a, da),
        "b": side(option_b, b, db),
        "b_minus_a": diff,
        "cash_b_minus_a": round(float(b["cash_usd"]) - float(a["cash_usd"]), 2),
        "note": "utility is USD on the user's preferences (lower is better)",
    }


def _modified(search_id: str, balances, cpp_overrides, preferences, together, min_cabin):  # type: ignore[no-untyped-def]
    from jetbridge.runner import _load

    spec, prof = _load(search_id)
    pd = prof.model_dump(mode="json")
    if balances is not None:
        pd["balances"] = {**pd["balances"], **balances}
    if cpp_overrides is not None:
        pd["cpp_overrides"] = {
            **pd["cpp_overrides"],
            **{k: str(v) for k, v in cpp_overrides.items()},
        }
    sd = spec.model_dump(mode="json")
    if preferences:
        sd["preferences"] = {**sd["preferences"], **preferences}
    if together:
        sd["together"] = together
    if min_cabin:
        sd["min_cabin"] = min_cabin
    return SearchSpec.model_validate(sd), Profile.model_validate(pd)


@server.tool()
def what_if(
    search_id: str,
    balances: dict[str, int] | None = None,
    cpp_overrides: dict[str, float] | None = None,
    preferences: dict[str, Any] | None = None,
    together: str | None = None,
    min_cabin: str | None = None,
) -> dict[str, Any]:
    """Re-optimize a finished search under changed balances / valuations / preferences /
    constraints WITHOUT re-searching or overwriting it (~10-60 s). Returns the decision
    brief under the hypothetical, so you can tell the user whether the answer changes."""
    from jetbridge.runner import what_if as _what_if

    spec, prof = _modified(search_id, balances, cpp_overrides, preferences, together, min_cabin)
    return _what_if(search_id, prof, spec)


@server.tool()
def apply_changes(
    search_id: str,
    preferences: dict[str, Any] | None = None,
    together: str | None = None,
    min_cabin: str | None = None,
) -> dict[str, Any]:
    """Like what_if, but commits the changed preferences/constraints to the search and
    rewrites its report (with the current saved profile)."""
    from jetbridge.runner import rereport
    from jetbridge.store.db import SearchRow, session

    spec, _ = _modified(search_id, None, None, preferences, together, min_cabin)
    with session() as s:
        row = s.get(SearchRow, search_id)
        assert row is not None
        row.spec = spec.model_dump(mode="json")
    path = rereport(search_id, _profile(), spec)
    return get_brief(search_id) | {"report_markdown": path}


@server.tool()
async def targeted_search(
    search_id: str, searches: list[dict[str, Any]], reason: str
) -> dict[str, Any]:
    """Run up to 8 extra live searches inside an existing search, then re-optimize.
    Each search: {adapter: united|american|delta|alaska|google_flights, origin, destination,
    date: YYYY-MM-DD, pax, cabin?: Y|W|J|F (google_flights)}. ``reason`` is recorded in the
    coverage ledger. Takes ~20-90 s per search per site."""
    from datetime import date as _date

    from jetbridge.acquire.adapters import registry
    from jetbridge.acquire.types import Query
    from jetbridge.models.core import Cabin
    from jetbridge.runner import brief_for, targeted

    if len(searches) > 8:
        raise ValueError("at most 8 targeted searches per call")
    ad = registry()
    qs = []
    for x in searches:
        if x["adapter"] not in ad:
            raise ValueError(f"unknown adapter {x['adapter']}; known: {sorted(ad)}")
        qs.append(
            Query(
                x["adapter"],
                "day",
                x["origin"].upper(),
                x["destination"].upper(),
                _date.fromisoformat(x["date"]),
                int(x.get("pax", 1)),
                Cabin(x["cabin"]) if x.get("cabin") else None,
            )
        )
    out = await targeted(search_id, qs, reason)
    out["brief"] = brief_for(search_id).to_dict()
    return out


# --------------------------------------------------------------------------- cache + crawler
@server.tool()
def cached_availability(
    origins: list[str],
    destinations: list[str],
    start: str,
    end: str,
    cabins: list[str] | None = None,
    pax: int = 1,
    max_age_hours: float | None = None,
    limit: int = 150,
) -> dict[str, Any]:
    """INSTANT seats.aero-style lookup in the availability store: the latest known lowest
    miles / taxes / seats / nonstop / carriers per (program, route, date, cabin). Codes may be
    airports, metros or regions. Cells come from past searches and the crawler, carry their
    age, and are not live-verified; a cell counts for ``pax`` only if its price is valid for
    that party size (exact party, or a price-independent program with enough exact seats)."""
    from datetime import date as _date
    from datetime import timedelta

    from jetbridge.kb import load_kb
    from jetbridge.store.availability import search

    kb = load_kb()

    def expand(codes: list[str]) -> list[str]:
        out: list[str] = []
        for c in codes:
            out += [a for a in kb.expand(c.upper()) if a not in out]
        return out

    rows = search(
        expand(origins),
        expand(destinations),
        _date.fromisoformat(start),
        _date.fromisoformat(end),
        cabins=[c.upper() for c in cabins] if cabins else None,
        pax=pax,
        max_age=timedelta(hours=max_age_hours) if max_age_hours else None,
    )
    return {"cells": rows[:limit], "total": len(rows), "verified": False}


@server.tool()
def site_health() -> dict[str, Any]:
    """Rate governor state per site: requests used vs cap in the sliding window, learned cap
    from past blocks, remaining cooldown, and whether the crawler is running."""
    from jetbridge.acquire.adapters import registry
    from jetbridge.acquire.engine import site_adapters
    from jetbridge.acquire.governor import Governor
    from jetbridge.crawl.crawler import crawler_status

    gov = Governor(site_adapters=site_adapters(registry()))
    return {"sites": gov.usage(), "crawler": crawler_status()}


@server.tool()
def start_crawler(minutes: float | None = None) -> dict[str, Any]:
    """Start the background crawler (detached): it keeps every watched route fresh and fills
    deferred queries at a sustainable rate (half of each site's budget). ``minutes`` bounds it."""
    from jetbridge.crawl.__main__ import log_path, spawn
    from jetbridge.crawl.crawler import crawler_status

    st = crawler_status()
    if st and st.get("alive"):
        return {"status": "already running", "crawler": st}
    return {"status": "started", "pid": spawn(minutes), "log": str(log_path())}


@server.tool()
def watch_routes(spec: SearchSpec, weight: float = 1.0) -> dict[str, Any]:
    """Add a route set (same shape as a search spec) to the crawler's watchlist. Searches are
    watched automatically until their last travel day."""
    from jetbridge.crawl.watch import add_watch, list_watches

    wid, created = add_watch(spec, _profile(), weight=weight)
    return {"watch_id": wid, "created": created, "watches": list_watches()}


@server.tool()
def alerts(limit: int = 30, unseen_only: bool = True, mark_seen: bool = True) -> dict[str, Any]:
    """Changes between consecutive snapshots of availability cells: new_space, gone,
    price_drop, price_rise, more_seats (each with before/after miles, seats, carriers)."""
    from jetbridge.store.availability import recent_alerts

    return {"alerts": recent_alerts(limit, unseen_only, mark_seen)}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
