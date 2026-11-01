"""Availability store: seats.aero-style cells + change alerts.

Every genuine award answer (search, crawler or verification) is folded into one
cell group per (adapter, O&D, date, searched pax): one row per cabin with the
lowest miles, its taxes, the most seats seen, whether a nonstop exists, and the
operating carriers. Genuine empty answers are stored too (cabin ``*``, status
``none``): knowing a route has no space is as useful as knowing it has some.

Replacing a group compares old and new snapshots and writes alerts
(``new_space``, ``gone``, ``price_drop``, ``price_rise``, ``more_seats``): the
substrate of the monitor feature.

Cells are keyed by the *queried* O&D (an engine that expands a metro, e.g.
united.com HND -> TYO, stores under the queried airport).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, delete, or_, select

from jetbridge.acquire.types import ParseResult, Query
from jetbridge.models.core import AwardOffer
from jetbridge.store.db import AlertRow, AvailabilityRow, session

PRICE_MOVE = 0.10  # relative change that counts as a price alert


@dataclass(frozen=True)
class Cell:
    cabin: str
    status: str
    min_miles: int | None = None
    taxes: float | None = None
    currency: str | None = None
    max_seats: int | None = None
    seats_kind: str | None = None
    direct: bool = False
    carriers: str = ""
    n_offers: int = 0

    def brief(self) -> dict:
        return {
            "miles": self.min_miles,
            "taxes": self.taxes,
            "seats": self.max_seats,
            "seats_kind": self.seats_kind,
            "direct": self.direct,
            "carriers": self.carriers,
        }


def cells_from(parsed: ParseResult, empty: bool) -> list[Cell]:
    if empty or not parsed.awards:
        return [Cell(cabin="*", status="none")] if empty else []
    by_cabin: dict[str, list[AwardOffer]] = {}
    for o in parsed.awards:
        by_cabin.setdefault(o.itinerary.main_cabin.value, []).append(o)
    out = []
    for cabin, offers in sorted(by_cabin.items()):
        best = min(offers, key=lambda o: (o.miles, o.taxes.amount))
        seats = [o.seats for o in offers if o.seats is not None]
        top = max(seats, key=lambda s: (s.count, s.kind == "exact"), default=None)
        carriers = sorted({seg.operator for o in offers for seg in o.itinerary.segments})
        out.append(
            Cell(
                cabin=cabin,
                status="available",
                min_miles=best.miles,
                taxes=float(best.taxes.amount),
                currency=best.taxes.currency,
                max_seats=top.count if top else None,
                seats_kind=top.kind if top else None,
                direct=any(o.itinerary.stops == 0 for o in offers),
                carriers=",".join(carriers),
                n_offers=len(offers),
            )
        )
    return out


def ingest(
    q: Query,
    program: str,
    parsed: ParseResult,
    *,
    empty: bool,
    fetch_id: str,
    observed_at: datetime,
) -> list[AlertRow]:
    """Replace the cell group for ``q`` with this answer; returns new alerts.

    Older answers never overwrite newer ones (out-of-order cache replays)."""
    new = cells_from(parsed, empty)
    if not new:
        return []
    key = and_(
        AvailabilityRow.adapter == q.adapter,
        AvailabilityRow.origin == q.origin,
        AvailabilityRow.destination == q.destination,
        AvailabilityRow.depart_date == q.date.isoformat(),
        AvailabilityRow.request_pax == q.pax,
    )
    with session() as s:
        old_rows = list(s.scalars(select(AvailabilityRow).where(key)))
        if old_rows and max(_aware(r.observed_at) for r in old_rows) >= _aware(observed_at):
            return []
        old = {r.cabin: r for r in old_rows}
        alerts = _diff(q, program, old, {c.cabin: c for c in new}, fetch_id) if old_rows else []
        s.execute(delete(AvailabilityRow).where(key))
        for c in new:
            s.add(
                AvailabilityRow(
                    adapter=q.adapter,
                    program=program,
                    origin=q.origin,
                    destination=q.destination,
                    depart_date=q.date.isoformat(),
                    request_pax=q.pax,
                    cabin=c.cabin,
                    status=c.status,
                    min_miles=c.min_miles,
                    taxes=c.taxes,
                    currency=c.currency,
                    max_seats=c.max_seats,
                    seats_kind=c.seats_kind,
                    direct=int(c.direct),
                    carriers=c.carriers,
                    n_offers=c.n_offers,
                    observed_at=observed_at,
                    fetch_id=fetch_id,
                )
            )
        s.add_all(alerts)
    return alerts


def _diff(
    q: Query, program: str, old: dict[str, AvailabilityRow], new: dict[str, Cell], fid: str
) -> list[AlertRow]:
    out: list[AlertRow] = []

    def alert(kind: str, cabin: str, before: dict | None, after: dict | None) -> None:
        out.append(
            AlertRow(
                kind=kind,
                adapter=q.adapter,
                program=program,
                origin=q.origin,
                destination=q.destination,
                depart_date=q.date.isoformat(),
                cabin=cabin,
                before=before,
                after=after,
                fetch_id=fid,
            )
        )

    for cabin in sorted((set(old) | set(new)) - {"*"}):
        o, n = old.get(cabin), new.get(cabin)
        if o is None and n is not None:
            alert("new_space", cabin, None, n.brief())
        elif o is not None and n is None:
            alert("gone", cabin, _row_brief(o), None)
        elif o is not None and n is not None and o.min_miles and n.min_miles:
            if n.min_miles <= o.min_miles * (1 - PRICE_MOVE):
                alert("price_drop", cabin, _row_brief(o), n.brief())
            elif n.min_miles >= o.min_miles * (1 + PRICE_MOVE):
                alert("price_rise", cabin, _row_brief(o), n.brief())
            elif (n.max_seats or 0) > (o.max_seats or 0) and (o.max_seats or 0) < q.pax + 2:
                alert("more_seats", cabin, _row_brief(o), n.brief())
    return out


def _row_brief(r: AvailabilityRow) -> dict:
    return {
        "miles": r.min_miles,
        "taxes": r.taxes,
        "seats": r.max_seats,
        "seats_kind": r.seats_kind,
        "direct": bool(r.direct),
        "carriers": r.carriers,
    }


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ---------------------------------------------------------------------- queries
def search(
    origins: list[str],
    destinations: list[str],
    start: date,
    end: date,
    *,
    cabins: list[str] | None = None,
    pax: int = 1,
    max_age: timedelta | None = None,
    programs: list[str] | None = None,
) -> list[dict]:
    """Instant cached search, seats.aero style: the best known cell per
    (program, O&D, date, cabin) whose seats can cover ``pax``.

    A cell from a k-pax search counts for ``pax`` when k == pax, or when
    k <= pax and its seat count is exact and >= pax (Delta/Alaska verified
    price-independent; see freshness.py). Rows carry their age; nothing here is
    verified live."""
    from jetbridge.acquire.adapters import registry

    adapters = registry()
    conds = [
        AvailabilityRow.origin.in_(origins),
        AvailabilityRow.destination.in_(destinations),
        AvailabilityRow.depart_date >= start.isoformat(),
        AvailabilityRow.depart_date <= end.isoformat(),
        AvailabilityRow.status == "available",
    ]
    if cabins:
        conds.append(AvailabilityRow.cabin.in_(cabins))
    if programs:
        conds.append(AvailabilityRow.program.in_(programs))
    if max_age is not None:
        conds.append(AvailabilityRow.observed_at >= datetime.now(UTC) - max_age)
    conds.append(or_(AvailabilityRow.request_pax == pax, AvailabilityRow.request_pax < pax))
    with session() as s:
        rows = list(s.scalars(select(AvailabilityRow).where(*conds)))
    best: dict[tuple, AvailabilityRow] = {}
    for r in rows:
        if r.request_pax != pax:
            ad = adapters.get(r.adapter)
            indep = ad is not None and ad.price_independent_of_pax
            if not (indep and r.seats_kind == "exact" and (r.max_seats or 0) >= pax):
                continue
        k = (r.program, r.origin, r.destination, r.depart_date, r.cabin)
        cur = best.get(k)
        if cur is None or _aware(r.observed_at) > _aware(cur.observed_at):
            best[k] = r
    now = datetime.now(UTC)
    out = [
        {
            "program": r.program,
            "origin": r.origin,
            "destination": r.destination,
            "date": r.depart_date,
            "cabin": r.cabin,
            "miles": r.min_miles,
            "taxes": r.taxes,
            "currency": r.currency,
            "seats": r.max_seats,
            "seats_kind": r.seats_kind,
            "direct": bool(r.direct),
            "carriers": r.carriers,
            "offers": r.n_offers,
            "searched_pax": r.request_pax,
            "age_hours": round((now - _aware(r.observed_at)).total_seconds() / 3600, 1),
            "fetch_id": r.fetch_id,
        }
        for r in best.values()
    ]
    out.sort(key=lambda d: (d["date"], d["origin"], d["destination"], d["cabin"], d["miles"] or 0))
    return out


def recent_alerts(
    limit: int = 50, unseen_only: bool = False, mark_seen: bool = False
) -> list[dict]:
    with session() as s:
        stmt = select(AlertRow).order_by(AlertRow.created_at.desc()).limit(limit)
        if unseen_only:
            stmt = stmt.where(AlertRow.seen == 0)
        rows = list(s.scalars(stmt))
        out = [
            {
                "id": r.id,
                "at": _aware(r.created_at).isoformat(timespec="minutes"),
                "kind": r.kind,
                "program": r.program,
                "route": f"{r.origin}-{r.destination}",
                "date": r.depart_date,
                "cabin": r.cabin,
                "before": r.before,
                "after": r.after,
            }
            for r in rows
        ]
        if mark_seen:
            for r in rows:
                r.seen = 1
    return out
