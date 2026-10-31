# jetbridge

Comprehensive award (miles) + cash flight search for people who want *every*
option checked, not a quick guess. Tell Claude something like:

> CVG to HKG or ICN, end of May to early June, 3 people, prefer business. Other Asia
> locations are acceptable. I have most miles in American, then Delta and United.

jetbridge searches the airlines' own award engines directly (no paid APIs),
solves how to seat everyone given your balances, re-checks the best options live,
and writes a report with a coverage ledger showing exactly what was and was not searched.

## Setup (Windows)

```powershell
python -m pip install --user uv
python -m uv sync
python -m uv run patchright install chromium   # Google Chrome itself is used for searches
python -m uv run pytest                         # offline test suite
```

Open this folder in Claude Code. `.mcp.json` registers the `jetbridge` MCP server,
so you can just ask for flights in plain English.

## CLI

```powershell
uv run jetbridge profile --balance american=400000 --balance delta=250000 --balance united=150000
uv run jetbridge plan examples/cvg_asia_3pax.json      # queries + estimated runtime
uv run jetbridge search examples/cvg_asia_3pax.json    # full run -> var/reports/<id>/report.{md,html}
uv run jetbridge status <search_id>
uv run jetbridge resume <search_id>                    # continue after an interruption
uv run jetbridge probe CVG ICN 2027-05-28 --pax 1,3    # live health check per source
uv run jetbridge login aeroplan                        # opt-in: sign in yourself in a Chrome window
```

`--show-browser` makes the Chrome windows visible, so you can solve a human-verification
challenge if a site shows one. Searches pause for up to 5 minutes and then continue.

## How it decides (not just summarizes)

1. **Your utility model.** `SearchSpec.preferences` holds your trade-offs in USD per
   passenger. Cabin value is **per flown hour**: business on a 14-hour flight counts about
   10× more than on a 90-minute hop. There are also values for your time, stops, overnight
   layovers, airport changes, separate tickets and risk aversion.
2. **Agent loop.** A first pass finds the gaps, then follow-up searches target them:
   - *split completion*: when only 2 of 3 business seats exist, price an exact
     2-passenger booking;
   - *gateway long-haul*: when no business seats exist from home, search that program's
     long-haul gateways directly (the domestic connection is often the bottleneck);
   - *positioning*: buy cash positioning flights to those gateways, same day or the
     evening before, and compose separate-ticket itineraries that respect a 3-hour buffer.
3. **Optimization.** CP-SAT seats every passenger under your balances, with card-point
   transfers shared across programs, travel-together as a hard rule, and quality and
   risk priced in.
4. **Advisor.** Each solution is scored on points value, cash, hours, comfort gap and
   risk (after live re-verification). The advisor then:
   - filters to the **Pareto frontier** with tolerances and collapses equivalent options;
   - **recommends** the best risk-adjusted option and states every alternative as an
     exact trade-off;
   - computes **cents per point achieved against real cash fares** ("pay cash instead"
     when a redemption is poor);
   - runs an **exact break-even sensitivity** on each point valuation and on the
     business premium per hour, marking the pick FRAGILE when a small change flips it;
   - writes an **action plan**: verify, then transfer (irreversible), then book the
     scarcest seats first, then positioning.
5. **Claude as the expert.** MCP tools `get_brief`, `explain_option`, `compare_options`,
   `what_if` (re-optimize offline under new balances, valuations or constraints) and
   `targeted_search` (bounded live follow-ups) let Claude interrogate results and close
   gaps before recommending.

```powershell
uv run jetbridge brief <search_id>
uv run jetbridge whatif <search_id> --balance united=150000 --balance chase_ur=650000
uv run jetbridge whatif <search_id> --business-per-hour 200 --risk-aversion 0.8
uv run jetbridge explain <option_id>
```

## How it stays accurate

- **Real party size.** Searches use your actual passenger count, because aa.com charges
  more per person for 3 than for 1. A 1-passenger "split" search finds space that only
  works for mixed bookings, and those offers carry 1 passenger unless the airline's
  prices are verified not to depend on party size.
- **No silent gaps.** Every planned query ends as `ok`, `empty`, `blocked`, `failed`
  or `skipped` in the coverage ledger. "Empty" requires positive recognition of a
  genuine "no flights" answer. Block pages and unknown error codes count as failures.
- **Provenance.** Every price carries the source, the observation time and a fetch ID,
  and the raw response is stored compressed in `var/blobs/`.
- **Live re-check.** The top options are fetched again just before the report and
  marked `confirmed`, `changed` or `gone`.
- **Sanity checks.** Plausibility bands, the AA partner chart, and KB partner lists.
  Anomalies are flagged, never dropped.
- **Solver.** CP-SAT seats everyone under your balances, including card-point
  transfers shared across programs, with the travel-together policy as a hard constraint.

## Sources

See [docs/sources/README.md](docs/sources/README.md). Live today: United, American,
Delta, Alaska/Hawaiian (awards) and Google Flights (cash). Aeroplan needs an opt-in
login. Flying Blue and Virgin Atlantic are pending.

## Roadmap

1. Aeroplan (signed-in), Flying Blue, Virgin Atlantic adapters.
2. Separate-ticket gateway composition (positioning flight + long-haul award).
3. Sound calendar pruning (skip days a cabin-agnostic calendar proves have zero space).
4. **Monitor:** saved specs re-run on a schedule, diffed against `observations`, with
   notifications. The schema already supports this.
