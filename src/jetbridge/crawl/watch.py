"""Watchlist: route sets the crawler keeps fresh.

Every search registers its spec as a watch (until its last travel day), so the
crawler fills that search's deferred queries in the background and keeps the
answers fresh for a re-run. Watches can also be added by hand
(``jetbridge watch add spec.json``)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select

from jetbridge.models.spec import Profile, SearchSpec
from jetbridge.store.db import WatchRow, session

_IGNORED = {"time_budget_minutes", "refine", "refine_budget", "preferences", "together"}


def spec_fingerprint(spec: SearchSpec) -> str:
    d = {k: v for k, v in spec.model_dump(mode="json").items() if k not in _IGNORED}
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]


def _label(spec: SearchSpec) -> str:
    dests = ",".join(d.code for d in spec.destinations)
    out = f"{'/'.join(spec.origins)}->{dests} {spec.outbound.start}..{spec.outbound.end}"
    if spec.inbound:
        out += f" ret {spec.inbound.start}..{spec.inbound.end}"
    return f"{out} x{spec.passengers}"


def _expires(spec: SearchSpec) -> datetime:
    last = spec.inbound.end if spec.inbound else spec.outbound.end
    return datetime.combine(last + timedelta(days=1), time(0), tzinfo=UTC)


def add_watch(
    spec: SearchSpec,
    profile: Profile,
    *,
    source: str = "manual",
    weight: float = 1.0,
    label: str | None = None,
) -> tuple[str, bool]:
    """Register ``spec``; returns (watch id, created). An active watch with the
    same route fingerprint is reused (its weight raised, expiry extended)."""
    fp = spec_fingerprint(spec)
    with session() as s:
        for w in s.scalars(select(WatchRow).where(WatchRow.active == 1)):
            if spec_fingerprint(SearchSpec.model_validate(w.spec)) == fp:
                w.weight = max(w.weight, weight)
                w.profile = profile.model_dump(mode="json")
                return w.id, False
        wid = f"w_{uuid.uuid4().hex[:8]}"
        s.add(
            WatchRow(
                id=wid,
                label=label or _label(spec),
                spec=spec.model_dump(mode="json"),
                profile=profile.model_dump(mode="json"),
                source=source,
                weight=weight,
                expires_at=_expires(spec),
            )
        )
    return wid, True


def watch_from_search(sid: str, spec: SearchSpec, profile: Profile) -> str | None:
    if os.environ.get("JETBRIDGE_NO_AUTOWATCH"):
        return None
    wid, _ = add_watch(spec, profile, source=f"search:{sid}")
    return wid


def active_watches() -> list[WatchRow]:
    now = datetime.now(UTC)
    with session() as s:
        rows = list(s.scalars(select(WatchRow).where(WatchRow.active == 1)))
        out = []
        for w in rows:
            exp = w.expires_at
            if exp is not None and (exp if exp.tzinfo else exp.replace(tzinfo=UTC)) < now:
                w.active = 0  # trip is over
                continue
            out.append(w)
    return out


def list_watches(include_inactive: bool = False) -> list[dict]:
    with session() as s:
        stmt = select(WatchRow).order_by(WatchRow.created_at.desc())
        if not include_inactive:
            stmt = stmt.where(WatchRow.active == 1)
        return [
            {
                "id": w.id,
                "label": w.label,
                "source": w.source,
                "weight": w.weight,
                "active": bool(w.active),
                "expires": w.expires_at.date().isoformat() if w.expires_at else None,
            }
            for w in s.scalars(stmt)
        ]


def set_active(wid: str, active: bool) -> bool:
    with session() as s:
        w = s.get(WatchRow, wid)
        if w is None:
            return False
        w.active = int(active)
        return True
