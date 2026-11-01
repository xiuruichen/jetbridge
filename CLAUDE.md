# jetbridge

Award + cash flight search engine. Claude is the conversational layer (via the
`jetbridge` MCP server in `.mcp.json`); all prices and availability come from the
deterministic engine.

## Using it for a flight request
Follow the MCP server instructions (an advisor playbook):
1. Frame the problem: elicit constraints and **preferences** (business $/flown hour, risk
   aversion), and balances including card points.
2. `plan_search` → `start_search` → poll `search_status`.
3. Reason with `get_brief`. Drill in with `explain_option` / `compare_options`, stress-test
   FRAGILE picks with `what_if`, and close evidence-backed gaps with `targeted_search`.
4. Recommend, with the action plan.
Quote numbers only from tool output and cite option IDs. Label your own judgement. Always
report coverage %, gaps and verification status, and that transfers are irreversible.

## Dev
- `uv run pytest` (offline: parsers on recorded fixtures, CP-SAT property tests, e2e).
- `uv run jetbridge probe CVG ICN 2027-05-28 --pax 1,3` — live adapter health check.
- `uv run jetbridge plan examples/cvg_asia_3pax.json` / `search` / `status` / `profile`.
- `uv run jetbridge avail ...` (instant cached cells), `crawl [--detach|--status]`, `watch`,
  `alerts`, `health` (rate governor), `backfill` (rebuild the store from stored blobs).
- Recon a new/drifted site: `src/jetbridge/acquire/recon.py` (records XHR/JSON to `var/recon/`).
- Live searches drive the installed Google Chrome headful (off-screen) via patchright with
  persistent per-site profiles in `var/profiles/`; headless is rejected by Akamai.

## Layout
`models/` strict types · `kb/data/*.yaml` partners/transfers/charts (with last_verified) ·
`acquire/` broker, scheduler (in-process AIMD), governor (cross-process budgets and
cooldowns), freshness (TTL and party-size reuse), cache, detect, engine, adapters ·
`store/availability.py` cells and alerts · `crawl/` watchlist, crawler, backfill · `plan/` planner ·
`solve/` quality (layovers, comfort) · eligibility (candidates, risk, composition) ·
valuation (cpp vs cash) · CP-SAT allocator · advisor (Pareto, sensitivity, actions) ·
`plan/refine.py` adaptive follow-ups · `runner.py` agent loop + verification + what-if ·
`report/` Markdown/HTML · `mcp_server.py`, `worker.py`, `cli.py`. Mutable state: `var/`.

## Invariants (do not break)
- Searches use the real party size (aa.com prices change with pax count).
- An offer seen in a k-pax search can carry exactly k pax unless the adapter is verified
  `price_independent_of_pax`.
- A response is "empty" only if the adapter positively recognizes a genuine no-flights answer.
- Every planned query lands in the coverage ledger with a final status, and follow-up
  queries record *why* they ran.
- Solver candidate IDs include direction and searched party size (regression: a shared
  offer_id once aliased two variables and double-counted passengers).
- Party-size price validity (`price_independent_of_pax`) is set only from data:
  Delta and Alaska are seat-count consistent (796/796, 41/41); United and AA are dynamic
  by party size (United: 653 fares that showed 9+ seats still cost more for 3).
- The time budget is enforced at runtime (stage deadlines, a hard timeout per fetch,
  cancelling requests in flight past the deadline). Work that doesn't fit is `deferred`,
  never dropped.
- Every live request goes through the rate governor. A block is never retried
  (offscreen): it opens a persisted site cooldown that every process honors. Do not add
  retry loops or bypasses around `Governor.acquire`.
- A cached answer is reused only within `freshness.ttl_for`, and only across party sizes
  for adapters with `price_independent_of_pax` and exact seats. Verification always goes
  live (`use_cache=False`).
- The availability store is derived data, rebuildable with `jetbridge backfill`. An older
  answer never overwrites a newer cell.
- Advisor numbers are exact functions of solver output; sensitivity break-evens are
  tested to actually flip the recommendation.
