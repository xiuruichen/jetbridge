# jetbridge architecture

This is the technical companion to the [README](../README.md). It explains how the engine
works and why it is built this way. Per-source details (endpoints, quirks, request
budgets) live in [sources/README.md](sources/README.md). Rules that must not be broken
are in [CLAUDE.md](../CLAUDE.md).

## Pipeline

```
request (Claude or a spec file)
  └─► planner ── cache-aware, time-budgeted query list (coverage ledger)
        └─► engine ── rate governor ─► session broker (Chrome, patchright) ─► adapter.fetch
              │           └─ blocks open a site cooldown; nothing is retried into a challenge
              ├─► raw response saved (zstd, var/blobs) ─► adapter.parse (pure function)
              ├─► observations (append-only history) + availability cells (+ alerts)
        └─► refinement (split completion, gateway long-haul, cash positioning)
        └─► candidates ─► CP-SAT allocator (k-best) ─► advisor (Pareto, sensitivity, risk)
        └─► live re-verification of the shortlist ─► report (Markdown + HTML)
background: crawler ── watches ─► the same engine (role "crawl", half of each budget)
```

## Code map

| Path | What it does |
|---|---|
| `models/` | Strict Pydantic types: Segment, Itinerary, AwardOffer, CashOffer, SearchSpec, Profile |
| `kb/data/*.yaml` | Partners, transfer ratios, award charts, geography (each file has `last_verified`) |
| `acquire/broker.py` | Persistent Chrome contexts per site (off-screen, headful), tab pool, in-page fetch |
| `acquire/governor.py` | Cross-process request budgets, learned caps, cooldowns, probe |
| `acquire/scheduler.py` | In-process burst shaping (AIMD concurrency and spacing) |
| `acquire/freshness.py` | Freshness windows; which stored answers can serve a query |
| `acquire/engine.py` | Runs one query: cache, governor, fetch, classify, parse, store |
| `acquire/adapters/` | One adapter per source: `fetch()` plus a pure `parse()` |
| `store/availability.py` | Availability cells, change alerts, instant cached search |
| `crawl/` | Watchlist, crawler loop, backfill from stored responses |
| `plan/planner.py`, `plan/refine.py` | Query planning, then evidence-driven follow-ups |
| `solve/` | Quality scoring, eligibility, valuation, CP-SAT allocator, advisor |
| `runner.py` | The search loop: stage deadlines, verification, what-if, re-report |
| `report/` | Markdown and HTML report |
| `mcp_server.py`, `cli.py`, `worker.py` | Interfaces |

## Architecture: a store plus a slow crawler, not bursts of searches

Our own fetch log showed why sites blocked us. Delta answered about 85 requests in
2 minutes, then Akamai returned 444 for 50 minutes, because each blocked query was retried
3 times and every new run started probing again. Seats.aero answers instantly because
searches read a store that a crawler fills steadily. jetbridge now works the same way:

```
search / crawler ──► rate governor (all processes) ──► engine ──► availability store ──► alerts
        ▲                    │ window budget · learned caps ·          (route·date·cabin cells,
        │                    │ cooldown · probe                         "no space" included)
        └── cache first: a fresh stored answer costs no request ◄──────────────┘
```

- **Rate governor** (`acquire/governor.py`). Each site has a request budget over a
  sliding window, counted in SQLite, so it holds across concurrent processes and runs.
  The cap is learned from past blocks: 60% of the smallest burst that triggered one.
  A block is **never retried**. It puts the whole site into an escalating, persisted
  cooldown (10 min, doubling per episode, up to 4 h). After the cooldown, a single probe
  request goes first. Queries that would run during a cooldown are deferred, not fired
  into the challenge.
- **Availability store** (`store/availability.py`). Every genuine answer from a search,
  the crawler or a verification becomes cells per program, route, date, cabin and
  searched party size. Each cell holds the lowest miles, its taxes, the seats, whether a
  nonstop exists, and the carriers. Genuine "no flights" answers are stored too.
  Consecutive snapshots are compared and produce **alerts** (`new_space`, `gone`,
  `price_drop`, `price_rise`, `more_seats`).
- **Cache-first search.** An answer stays fresh for a time that depends on how far away
  departure is: 24 h beyond 6 months out, down to 45 min in the last 3 days. A fresh
  answer is reused instead of fetched. For Delta and Alaska (prices verified independent
  of party size, with exact seat counts), a 1-passenger answer serves any party size.
  The planner spends live budget only on queries without a fresh answer.
- **Crawler** (`crawl/`). Every search becomes a **watch** until its last travel day.
  The crawler expands each watch with the planner, picks queries that are due (never
  fetched, or past 80% of their freshness window), and fetches them steadily. It uses
  at most half of each site's budget, leaving headroom for searches, and backs off
  queries that keep failing. Run `jetbridge crawl --detach`. Deferred queries from a
  search get filled in the background, so a later re-run is mostly served from cache.
- **Verification stays live.** Shortlisted options are always re-fetched before
  they're recommended. Cached data shapes the search; it never becomes a booking
  recommendation without a fresh check.

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

## How it stays fast (20-minute budget by default)

- **Hard time budget.** `SearchSpec.time_budget_minutes` defaults to 20. The planner
  estimates each site's throughput from measured latency and its concurrency policy, then
  ranks queries: primary destinations first, then the rest by preference weight and by
  whether they have nonstop service from North America. Whatever doesn't fit is marked
  **deferred**, never silently dropped. At runtime, each stage (acquisition, refinement,
  verification) has a hard deadline, every fetch has a hard timeout, and requests still
  running at the deadline are cancelled and recorded as deferred. `resume --deep` runs
  everything that was deferred.
- **Real concurrency.** In-page fetch requests run in parallel inside one warmed tab;
  sites that navigate get a pool of tabs. Spacing applies between request *starts*.
- **Adaptive rate control (AIMD).** Each site gets one more concurrent request after a
  clean streak; on any block it halves concurrency and doubles spacing. Each site
  settles at the fastest rate it tolerates.
- **Block memory.** The governor's cooldown is persisted, so every process honors it.
  Refinement skips sites that are cooling down. `resume --show-browser` ignores the
  cooldown so you can complete the check yourself.
- **No wasted queries.** Searches use the real party size (no blanket 1-passenger
  pass); split probes are driven by evidence. United's HND search already covers NRT, so
  NRT isn't searched separately. Google Flights stops collecting results once they stop
  arriving.
- **Recover without re-fetching.** `jetbridge reparse <id>` re-runs fixed parsers over
  stored raw responses.

## How it stays accurate

- **Real party size.** Searches use your actual passenger count, because aa.com charges
  more per person for 3 than for 1. A 1-passenger "split" search finds space that only
  works for mixed bookings, and those offers carry 1 passenger unless the airline's
  prices are verified not to depend on party size.
- **No silent gaps.** Every planned query ends as `ok`, `empty`, `blocked`, `failed`,
  `deferred` or `skipped` in the coverage ledger. Rows served from the cache say how old
  their answer is. "Empty" requires positive recognition of a
  genuine "no flights" answer. Block pages and unknown error codes count as failures.
- **Provenance.** Every price carries the source, the observation time and a fetch ID,
  and the raw response is stored compressed in `var/blobs/`.
- **Live re-check.** The top options are fetched again just before the report and
  marked `confirmed`, `changed` or `gone`.
- **Sanity checks.** Plausibility bands, the AA partner chart, and KB partner lists.
  Anomalies are flagged, never dropped.
- **Solver.** CP-SAT seats everyone under your balances, including card-point
  transfers shared across programs, with the travel-together policy as a hard constraint.
