"""jetbridge command-line interface."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Comprehensive award + cash flight search.", no_args_is_help=True)
console = Console()


@app.command()
def probe(
    origin: str,
    destination: str,
    day: str = typer.Argument(..., help="YYYY-MM-DD"),
    adapters: str = typer.Option("united,american,delta", help="comma-separated adapter ids"),
    pax: str = typer.Option("1", help="comma-separated passenger counts to try"),
    show_browser: bool = typer.Option(False, help="show Chrome windows on screen"),
    cabin: str = typer.Option("", help="cabin hint for cash search: Y/W/J/F"),
) -> None:
    """Run live day searches and print the cheapest offer per cabin (adapter health check)."""
    from jetbridge.acquire.adapters import registry
    from jetbridge.acquire.broker import SessionBroker
    from jetbridge.acquire.engine import Engine
    from jetbridge.acquire.types import Query
    from jetbridge.models.core import Cabin

    cab = Cabin(cabin) if cabin else None
    paxes = [int(p) for p in pax.split(",")]
    ids = [a.strip() for a in adapters.split(",")]

    async def main() -> None:
        async with SessionBroker(offscreen=not show_browser) as br:
            eng = Engine(registry(), br, cache_ttl=timedelta(0))

            async def one(a: str) -> list[str]:
                lines = []
                for n in paxes:
                    q = Query(
                        a,
                        "day",
                        origin.upper(),
                        destination.upper(),
                        date.fromisoformat(day),
                        n,
                        cab,
                    )
                    r = await eng.run(q, use_cache=False)
                    aw = r.parsed.awards
                    for c in sorted(r.parsed.cash, key=lambda c: c.price.amount)[:4]:
                        segs = " ".join(
                            f"{s.flight_code}:{s.origin}-{s.destination}"
                            for s in c.itinerary.segments
                        )
                        lines.append(f"    cash: {c.price}/pax  {segs}")
                    lines.append(
                        f"[bold]{a}[/] pax={n}: {r.status} attempts={r.attempts} offers={len(aw) + len(r.parsed.cash)}"
                        + (f" reason={r.reason}" if r.reason else "")
                        + (f" warnings={r.parsed.warnings[:2]}" if r.parsed.warnings else "")
                    )
                    best: dict[str, object] = {}
                    for x in aw:
                        c = x.itinerary.main_cabin.value
                        if c not in best or x.miles < best[c].miles:  # type: ignore[attr-defined]
                            best[c] = x
                    for c, x in sorted(best.items()):
                        segs = " ".join(
                            f"{s.flight_code}:{s.origin}-{s.destination}/{s.cabin.value}"
                            for s in x.itinerary.segments  # type: ignore[attr-defined]
                        )
                        lines.append(
                            f"    {c}: {x.miles:,} + {x.taxes}  seats={x.seats.kind}:{x.seats.count}  {segs}"  # type: ignore[attr-defined]
                        )
                return lines

            for block in await asyncio.gather(*(one(a) for a in ids)):
                for line in block:
                    console.print(line)

    asyncio.run(main())


@app.command()
def kb() -> None:
    """Validate and summarize the knowledge base."""
    from jetbridge.kb import load_kb

    k = load_kb()
    t = Table("program", "adapter", "bookable (high)", "bookable (verify)")
    for p in k.programs.values():
        t.add_row(
            p.id,
            p.adapter,
            " ".join(p.bookable_carriers.high),
            " ".join(p.bookable_carriers.verify),
        )
    console.print(t)
    console.print(
        f"transfers: {len(k.transfers)}  last_verified: {json.dumps(k.last_verified, default=str)}"
    )


def _load_spec(path: Path) -> object:
    from jetbridge.models.spec import SearchSpec

    return SearchSpec.model_validate_json(path.read_text(encoding="utf-8"))


def _profile():
    from jetbridge.models.spec import Profile
    from jetbridge.store.db import kv_get

    return Profile.model_validate(kv_get("profile", {}))


@app.command()
def plan(spec: Path, budget: int = typer.Option(2000, help="hard max queries per adapter")) -> None:
    """Show the search plan (queries, coverage, estimated runtime) without running it."""
    from jetbridge.acquire.adapters import registry
    from jetbridge.kb import load_kb
    from jetbridge.plan.planner import build_plan

    p = build_plan(
        _load_spec(spec), _profile(), load_kb(), registry(), max_queries_per_adapter=budget
    )  # type: ignore[arg-type]
    console.print_json(json.dumps(p.summary(), default=str))


@app.command()
def search(
    spec: Path,
    budget: int = typer.Option(2000, help="hard max queries per adapter"),
    show_browser: bool = typer.Option(False, help="show Chrome windows on screen"),
) -> None:
    """Plan and run a full search, then write the report."""
    from jetbridge.runner import create_search, run_search

    sid, p = create_search(_load_spec(spec), _profile(), max_queries_per_adapter=budget)  # type: ignore[arg-type]
    console.print(
        f"search [bold]{sid}[/]: {p.summary()['queries_runnable']} queries, ~{p.summary()['estimated_minutes']} min"
    )
    path = asyncio.run(run_search(sid, offscreen=not show_browser))
    console.print(f"report: {path}")


@app.command()
def resume(
    search_id: str,
    show_browser: bool = typer.Option(False, help="visible Chrome, to complete human checks"),
    retry_blocked: bool = typer.Option(True, help="also retry blocked/failed queries"),
    deep: bool = typer.Option(False, help="also run queries deferred by the time budget"),
) -> None:
    """Continue a search (pending + by default blocked/failed rows) and regenerate its report."""
    from jetbridge.runner import run_search

    path = asyncio.run(
        run_search(search_id, offscreen=not show_browser, retry_blocked=retry_blocked, deep=deep)
    )
    console.print(f"report: {path}")


@app.command()
def report(
    search_id: str,
    use_current_profile: bool = typer.Option(False, help="re-rank with today's profile"),
) -> None:
    """Re-solve and re-render a search from stored observations (offline, no fetching)."""
    from jetbridge.runner import rereport

    console.print(f"report: {rereport(search_id, _profile() if use_current_profile else None)}")


@app.command()
def reparse(search_id: str) -> None:
    """Re-run current parsers over a search's stored responses (no network), then re-report."""
    from jetbridge.runner import reparse as _reparse
    from jetbridge.runner import rereport

    console.print(f"ledger changes: {_reparse(search_id)}")
    console.print(f"report: {rereport(search_id)}")


@app.command()
def status(search_id: str) -> None:
    """Search status and coverage ledger counts."""
    from jetbridge.runner import ledger_stats
    from jetbridge.store.db import SearchRow, session

    with session() as s:
        row = s.get(SearchRow, search_id)
        if row is None:
            raise typer.BadParameter(f"unknown search {search_id}")
        console.print(
            f"{row.id}: {row.status} stage={row.stage} report={row.report_path} error={row.error}"
        )
    console.print_json(json.dumps(ledger_stats(search_id)))


@app.command()
def profile(
    set_balance: list[str] = typer.Option(
        [], "--balance", help="currency=amount, e.g. american=250000"
    ),
    set_cpp: list[str] = typer.Option([], "--cpp", help="currency=cents, e.g. delta=1.2"),
    login: list[str] = typer.Option(
        [], "--login", help="adapter id allowed to use a signed-in session"
    ),
    clear: bool = typer.Option(False, help="reset the profile"),
) -> None:
    """Show or edit balances, valuations and login opt-ins."""
    from jetbridge.kb import load_kb
    from jetbridge.models.spec import Profile
    from jetbridge.store.db import kv_set

    kb = load_kb()
    prof = Profile() if clear else _profile()
    data = prof.model_dump(mode="json")
    for kv in set_balance:
        k, v = kv.split("=")
        if k not in kb.currencies:
            raise typer.BadParameter(f"unknown currency {k}; known: {', '.join(kb.currencies)}")
        data["balances"][k] = int(v.replace(",", "").replace("_", ""))
    for kv in set_cpp:
        k, v = kv.split("=")
        data["cpp_overrides"][k] = v
    for a in login:
        if a not in data["logins_enabled"]:
            data["logins_enabled"].append(a)
    prof = Profile.model_validate(data)
    kv_set("profile", prof.model_dump(mode="json"))
    console.print_json(prof.model_dump_json())


LOGIN_URLS = {
    "aeroplan": "https://www.aircanada.com/clogin/pages/login",
    "united": "https://www.united.com/en/us/account/signin",
    "american": "https://www.aa.com/loyalty/login",
    "delta": "https://www.delta.com/login/loginPage",
    "alaska": "https://www.alaskaair.com/www2/ssl/myalaskaair/MyAlaskaAir.aspx",
    "flyingblue": "https://wwws.airfrance.us/profile",
    "virginatlantic": "https://www.virginatlantic.com/flying-club/login",
}


@app.command()
def login(site: str) -> None:
    """Open a visible Chrome window on SITE's sign-in page using jetbridge's persistent
    profile for that site. Sign in yourself (jetbridge never sees your password), then
    press Enter here. Also enables the site in profile.logins_enabled."""
    from patchright.async_api import async_playwright

    from jetbridge.paths import profile_dir
    from jetbridge.store.db import kv_set

    if site not in LOGIN_URLS:
        raise typer.BadParameter(f"known sites: {', '.join(LOGIN_URLS)}")

    async def main() -> None:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(profile_dir(site)), channel="chrome", headless=False, no_viewport=True
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(LOGIN_URLS[site], wait_until="domcontentloaded", timeout=90_000)
            console.print(
                f"Sign in to [bold]{site}[/] in the Chrome window, then press Enter here."
            )
            await asyncio.get_running_loop().run_in_executor(None, input)
            await ctx.close()

    asyncio.run(main())
    prof = _profile()
    data = prof.model_dump(mode="json")
    if site not in data["logins_enabled"]:
        data["logins_enabled"].append(site)
    kv_set("profile", data)
    console.print(f"{site}: session saved in its profile; logins_enabled={data['logins_enabled']}")


@app.command()
def brief(search_id: str) -> None:
    """Print the decision brief: recommendation, trade-offs, sensitivity, action plan."""
    from jetbridge.mcp_server import get_brief

    b = get_brief(search_id)["brief"] or {}
    rec = b.get("recommendation")
    if not rec:
        console.print("no recommendation (no feasible option)")
        return
    console.print(
        f"[bold]Recommendation {rec['option_id']}[/] {rec['cabins']} · utility ${rec['utility_usd']:,} · "
        f"risk {rec['risk']:.0%} · {rec['verification']}"
    )
    for r in rec["redemptions"]:
        console.print(
            f"  redemption {r['program']}: {r['cpp_achieved']}¢ vs {r['cpp_valuation']}¢ — {r['verdict']}"
        )
    console.print("[bold]Alternatives[/]")
    for a in b["alternatives"]:
        console.print(
            f"  {a['role']}: {a['option']['option_id']} {a['option']['cabins']} — {a['tradeoff']}"
        )
    console.print("[bold]Sensitivity[/]")
    for line in b["sensitivity"] or ["robust to valuation changes"]:
        console.print(f"  {line}")
    console.print("[bold]Action plan[/]")
    for i, step in enumerate(b["actions"], 1):
        console.print(f"  {i}. {step}")


@app.command()
def explain(option_id: str) -> None:
    """Full detail for one option (segments, layovers, risk, redemption value)."""
    from jetbridge.mcp_server import explain_option

    console.print_json(json.dumps(explain_option(option_id), default=str))


@app.command()
def whatif(
    search_id: str,
    cpp: list[str] = typer.Option([], help="currency=cents, e.g. united=1.5"),
    balance: list[str] = typer.Option([], help="currency=amount"),
    business_per_hour: float | None = typer.Option(None, help="$ per flown hour business is worth"),
    risk_aversion: float | None = typer.Option(None),
    together: str | None = typer.Option(None, help="same_flights|same_day|any"),
) -> None:
    """Re-optimize a finished search under hypothetical inputs (no re-search, no overwrite)."""
    from jetbridge.mcp_server import what_if

    prefs: dict = {}
    if business_per_hour is not None:
        prefs["cabin_premium_usd_per_hour"] = {
            "Y": 0,
            "W": 45,
            "J": business_per_hour,
            "F": max(170, business_per_hour),
        }
    if risk_aversion is not None:
        prefs["risk_aversion"] = risk_aversion
    out = what_if(
        search_id,
        balances={k: int(v) for k, v in (x.split("=") for x in balance)} or None,
        cpp_overrides={k: float(v) for k, v in (x.split("=") for x in cpp)} or None,
        preferences=prefs or None,
        together=together,
    )
    rec = out.get("recommendation")
    console.print(
        f"[bold]Under this scenario:[/] {rec['cabins'] if rec else 'no feasible option'}"
        + (
            f" · points {rec['points_by_currency']} · cash ${rec['cash_usd']:,} · utility ${rec['utility_usd']:,}"
            if rec
            else ""
        )
    )
    for line in out.get("sensitivity", [])[:3]:
        console.print(f"  {line}")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )


@app.callback()
def _root(verbose: bool = typer.Option(False, "-v")) -> None:
    import sys

    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    _setup_logging(verbose)


if __name__ == "__main__":
    app()
