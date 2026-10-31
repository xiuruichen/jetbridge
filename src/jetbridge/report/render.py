"""Deterministic report rendering (Markdown + standalone HTML + JSON summary).

Every number in the report is taken from solver/observation data; nothing is
computed or paraphrased by an LLM. Claude, when narrating, quotes option IDs.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined
from sqlalchemy import select

from jetbridge.kb import KB
from jetbridge.models.core import AwardOffer
from jetbridge.models.spec import Profile, SearchSpec
from jetbridge.paths import reports_dir
from jetbridge.solve.allocator import Solution
from jetbridge.solve.eligibility import Candidate, FilterStats
from jetbridge.store.db import LedgerRow, SearchRow, session

_env = Environment(
    loader=PackageLoader("jetbridge.report", "templates"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _fmt_dt(d: datetime) -> str:
    return d.strftime("%a %b %d %H:%M") + f" ({d.strftime('%z')[:3]})"


def _dur(td) -> str:
    m = int(td.total_seconds() // 60)
    return f"{m // 60}h{m % 60:02d}m"


def option_context(sol: Solution, idx: int, oid: str, verified: dict, kb: KB) -> dict:
    v = verified.get(idx, {})
    legs = []
    for a in sorted(
        sol.assignments,
        key=lambda a: (a.candidate.direction, a.candidate.itinerary.departs_at),
    ):
        o = a.candidate.offer
        it = a.candidate.itinerary  # full door-to-door, incl. positioning legs
        ver = v.get(o.offer_id)
        legs.append(
            {
                "direction": "Outbound" if a.candidate.direction == "out" else "Return",
                "pax": a.pax,
                "program": o.program if isinstance(o, AwardOffer) else o.source,
                "program_name": kb.programs[o.program].name
                if isinstance(o, AwardOffer)
                else o.source,
                "award": isinstance(o, AwardOffer),
                "miles": getattr(o, "miles", None),
                "miles_total": getattr(o, "miles", 0) * a.pax,
                "taxes": str(o.taxes if isinstance(o, AwardOffer) else o.price),
                "taxes_total": f"{(o.taxes.amount if isinstance(o, AwardOffer) else o.price.amount) * a.pax:,.2f}",
                "cabin": o.itinerary.main_cabin.name.title(),
                "route": f"{it.origin} → {it.destination}",
                "positioning": [
                    f"{x.itinerary.origin}→{x.itinerary.destination} "
                    f"{' '.join(sg.flight_code for sg in x.itinerary.segments)} cash {x.price}/person"
                    for x in a.candidate.positioning
                ],
                "risk": f"{a.candidate.risk:.0%}",
                "risk_reasons": a.candidate.risk_reasons,
                "stops": it.stops,
                "duration": _dur(it.duration),
                "segments": [
                    {
                        "flight": s.flight_code,
                        "operator": s.operator,
                        "from": s.origin,
                        "to": s.destination,
                        "dep": _fmt_dt(s.departs_at),
                        "arr": _fmt_dt(s.arrives_at),
                        "cabin": s.cabin.name.title(),
                        "equipment": s.equipment or "",
                        "ticket": tk,
                    }
                    for part, tk in (
                        (
                            p_,
                            "positioning (cash)"
                            if p_ is not o
                            else ("award" if isinstance(o, AwardOffer) else "cash"),
                        )
                        for p_ in a.candidate.offers
                    )
                    for s in part.itinerary.segments
                ],
                "seats": f"{'≥' if o.seats and o.seats.kind == 'lower_bound' else ''}{o.seats.count if o.seats else '?'}",
                "observed_at": o.provenance.observed_at.astimezone(UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                ),
                "fetch_id": o.provenance.fetch_id,
                "searched_pax": o.provenance.request_pax,
                "booking_url": o.booking_url or "",
                "flags": a.candidate.flags,
                "notes": list(getattr(o, "notes", ())),
                "verification": ver.status if ver else "not re-checked",
                "verification_detail": ver.detail if ver else {},
            }
        )
    steps = []
    for t in sol.transfers:
        conf = next(
            (x.confidence for x in kb.transfers if x.source == t.source and x.program == t.program),
            "?",
        )
        steps.append(
            f"Transfer {t.points:,} {kb.currencies[t.source]} → {t.miles:,} "
            f"{kb.programs[t.program].name} miles (irreversible; ratio confidence: {conf})."
        )
    for leg in legs:
        if leg["award"]:
            steps.append(
                f"Book {leg['direction'].lower()} {leg['route']} for {leg['pax']} passenger(s) with "
                f"{leg['program_name']}: {leg['miles']:,} miles + {leg['taxes']} each."
            )
    statuses = {leg["verification"] for leg in legs}
    status = (
        "confirmed"
        if statuses == {"confirmed"}
        else "gone"
        if "gone" in statuses
        else "changed"
        if "changed" in statuses
        else "not re-checked"
        if statuses == {"not re-checked"}
        else "partially verified"
    )
    return {
        "id": oid,
        "rank": idx + 1,
        "label": sol.label,
        "status": status,
        "miles_by_program": {kb.programs[p].name: m for p, m in sol.miles_by_program.items()},
        "miles_total": sum(sol.miles_by_program.values()),
        "cash_total": f"{sol.cash_usd:,.2f}",
        "value_usd": f"{sol.value_cents / 100:,.0f}",
        "legs": legs,
        "steps": steps,
        "warnings": sol.warnings,
        "summary": _summary(sol),
    }


def _summary(sol: Solution) -> str:
    """'CVG→HKG May 28 · 3×Business (5.6h in lower cabins; same flights, 2 stops, overnight)'."""
    parts = []
    for direction in ("out", "in"):
        legs = [a for a in sol.assignments if a.candidate.direction == direction]
        if not legs:
            continue
        groups: dict[str, list] = {}
        for a in legs:
            groups.setdefault(a.candidate.group, []).append(a)
        first = legs[0].candidate.itinerary  # door to door, incl. positioning
        mix: dict[str, int] = {}
        for a in legs:
            name = a.candidate.offer.itinerary.main_cabin.name.title()
            mix[name] = mix.get(name, 0) + a.pax
        cab = " + ".join(f"{n}×{c}" for c, n in sorted(mix.items(), key=lambda x: -x[1]))
        if all(not isinstance(a.candidate.offer, AwardOffer) for a in legs):
            cab += " cash"
        notes = []
        lower = max(
            sum(
                h
                for c_, h in (
                    a.candidate.quality.hours_by_cabin.items() if a.candidate.quality else []
                )
                if c_ < a.candidate.offer.itinerary.main_cabin
            )
            for a in legs
        )
        if lower >= 0.25:
            notes.append(f"{lower:.1f}h in lower cabins")
        notes.append("same flights" if len(groups) == 1 else f"{len(groups)} different itineraries")
        stops = max(a.candidate.itinerary.stops for a in legs)
        notes.append(f"{stops} stop{'s' if stops != 1 else ''}")
        if any(a.candidate.composed for a in legs):
            notes.append("positioning on separate ticket")
        if any(
            lv.overnight
            for a in legs
            for lv in (a.candidate.quality.layovers if a.candidate.quality else [])
        ):
            notes.append("overnight layover")
        tag = "" if direction == "out" else "Return "
        parts.append(
            f"{tag}{first.origin}→{first.destination} {first.departs_at.strftime('%b %d')} · {cab} "
            f"({', '.join(notes)})"
        )
    return "; ".join(parts)


def write_report(
    sid: str,
    spec: SearchSpec,
    profile: Profile,
    kb: KB,
    sols: list[Solution],
    verified: dict,
    option_ids: list[str],
    cands: list[Candidate],
    stats: FilterStats,
    brief=None,
) -> Path:
    with session() as s:
        row = s.get(SearchRow, sid)
        ledger = list(s.scalars(select(LedgerRow).where(LedgerRow.search_id == sid)))
    assert row is not None
    plan = row.plan or {}
    cov: dict[str, dict[str, int]] = {}
    problems = []
    for r in ledger:
        cov.setdefault(r.adapter, {}).setdefault(r.status, 0)
        cov[r.adapter][r.status] += 1
        if r.status in ("blocked", "failed"):
            q = r.query["query"]
            problems.append(
                f"{r.adapter} {q['origin']}→{q['destination']} {q['date']} pax={q['pax']}: "
                f"{r.status} — {(r.reason or '')[:160]}"
            )
    total = len(ledger)
    done = sum(1 for r in ledger if r.status in ("ok", "empty"))
    options = [option_context(s_, i, option_ids[i], verified, kb) for i, s_ in enumerate(sols)]

    # Per destination/cabin cheapest per-pax award seen (searchable appendix)
    cheapest: dict[tuple[str, str, str], dict] = {}
    for c in cands:
        o = c.offer
        if not isinstance(o, AwardOffer):
            continue
        it = o.itinerary
        end = it.destination if c.direction == "out" else it.origin
        k = (c.direction, end, it.main_cabin.name.title())
        if k not in cheapest or o.miles < cheapest[k]["miles"]:
            cheapest[k] = {
                "direction": "Outbound" if c.direction == "out" else "Return",
                "airport": end,
                "cabin": it.main_cabin.name.title(),
                "miles": o.miles,
                "program": o.program,
                "date": it.departs_at.date().isoformat(),
                "flights": " ".join(s.flight_code for s in it.segments),
                "searched_pax": o.provenance.request_pax,
                "seats": f"{'≥' if o.seats.kind == 'lower_bound' else ''}{o.seats.count}",
            }
    ctx = {
        "sid": sid,
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "spec": spec,
        "spec_json": json.dumps(spec.model_dump(mode="json"), indent=1),
        "balances": profile.balances,
        "plan": plan,
        "options": options,
        "coverage": cov,
        "coverage_pct": round(100 * done / total, 1) if total else 0.0,
        "ledger_total": total,
        "ledger_done": done,
        "problems": problems[:60],
        "problems_more": max(0, len(problems) - 60),
        "stats": stats,
        "cheapest": sorted(
            cheapest.values(), key=lambda r: (r["direction"], r["cabin"], r["miles"])
        ),
        "kb_verified": {k: str(v) for k, v in kb.last_verified.items()},
    }
    ctx["brief"] = _brief_ctx(brief, kb) if brief is not None else None
    md = _env.get_template("report.md.j2").render(**ctx)
    out_dir = reports_dir() / sid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    (out_dir / "report.html").write_text(_html(md, sid), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "search_id": sid,
                "coverage": cov,
                "coverage_pct": ctx["coverage_pct"],
                "options": options,
                "filter_stats": {
                    "seen": stats.seen,
                    "kept": stats.kept,
                    "rejected": stats.rejected,
                },
                "plan": plan,
                "brief": brief.to_dict() if brief is not None else None,
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    return out_dir / "report.md"


def _html(md: str, sid: str) -> str:
    """Minimal, dependency-free Markdown->HTML for our own controlled template."""
    lines = md.splitlines()
    out = []
    in_table = False
    in_list = False
    for ln in lines:
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if ln.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(ln[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if ln.startswith("#"):
            n = len(ln) - len(ln.lstrip("#"))
            out.append(f"<h{n}>{_inline(ln[n:].strip())}</h{n}>")
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
    if in_table:
        out.append("</table>")
    if in_list:
        out.append("</ul>")
    css = """body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1d2433;background:#fff}
table{border-collapse:collapse;margin:8px 0 16px;width:100%;font-size:13px}th,td{border:1px solid #d6dbe4;padding:4px 8px;text-align:left}
th{background:#f2f4f8}code{background:#f2f4f8;padding:1px 4px;border-radius:3px}h2{border-bottom:2px solid #1d2433;padding-bottom:4px;margin-top:32px}
@media(prefers-color-scheme:dark){body{background:#12151c;color:#e3e7ef}th{background:#1f2430}td,th{border-color:#333a48}code{background:#1f2430}h2{border-color:#e3e7ef}}"""
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>jetbridge {html.escape(sid)}</title><style>{css}</style></head><body>{''.join(out)}</body></html>"
    )


def _inline(s: str) -> str:
    import re

    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r"<a href='\2'>\1</a>", s)
    return s


def _brief_ctx(brief, kb: KB) -> dict:
    def row(o):
        pts = " + ".join(
            f"{n:,} {kb.currencies.get(c, c)}" for c, n in o.points_by_currency.items()
        )
        return {
            "id": o.option_id,
            "summary": _summary(o.solution),
            "points": pts or "—",
            "points_usd": f"{o.points_usd:,.0f}",
            "cash": f"{o.cash_usd:,.0f}",
            "hours": f"{o.hours:.1f}",
            "comfort": f"{o.comfort_usd:,.0f}",
            "risk": f"{o.risk:.0%}",
            "risk_reasons": o.risk_reasons,
            "verification": o.verification,
            "utility": f"{o.utility_usd:,.0f}",
            "redemptions": o.redemptions,
            "dominated_by": o.dominated_by,
            "equivalents": o.equivalents,
        }

    return {
        "rec": row(brief.recommendation) if brief.recommendation else None,
        "frontier": [row(o) for o in brief.frontier[:10]],
        "alternatives": [{"role": r, "o": row(o), "tradeoff": t} for r, o, t in brief.alternatives],
        "dominated": len(brief.dominated),
        "gone": [o.option_id for o in brief.gone],
        "sensitivity": brief.sensitivity,
        "actions": brief.actions,
        "insights": brief.insights,
    }
