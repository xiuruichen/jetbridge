# Source capability matrix (Phase 0 recon, 2026-10-31)

All sources are driven through installed Google Chrome (headful, off-screen) with
patchright and a persistent per-site profile. Headless Chromium is rejected by
Akamai (`net::ERR_HTTP2_PROTOCOL_ERROR` on united.com).

| Source | Status | Access | Endpoint | Tier | Seats | Price vs party size | Calendar |
|---|---|---|---|---|---|---|---|
| **United** (MileagePlus) | ✅ go | guest token | `POST /api/flight/FetchSSENestedFlights` (SSE), token from `GET /api/auth/anonymous-token` | T1 | fare-bucket counts per segment (`JN9`, cap 9) | **independent** (verified 1 vs 3 pax) | not yet used |
| **American** (AAdvantage) | ✅ go | guest | deep link → form `POST /booking/choose-flights/1`, data in `<script id="ng-state">` | T3 | `seatsRemaining` = 0 means *unknown* | **dynamic by party size** (352,500 ×1 vs 384,000 ×3) | 13-day lowest strip (cabin-agnostic) |
| **Delta** (SkyMiles) | ✅ go | `authorization: GUEST` | `POST offer-api-prd.delta.com/prd/rm-offer-gql` (GraphQL; requires `transactionId` header) | T1 | exact `availableSeatCnt` (cap 9) | unverified → treated as dependent | ±3-day flex (cabin-agnostic) |
| **Alaska/Hawaiian** (Atmos) | ✅ go | guest | `GET /search/results?...ShoppingMethod=onlineaward`, data in SvelteKit `resolve(1, () => [...])` literal | T1 + sandboxed eval | exact `seatsRemaining` | **independent** (`allPaxPoints = atmosPoints × pax`, verified) | `POST /search/api/shoulderDates` |
| **Google Flights** (cash) | ✅ go | guest | `tfs` protobuf URL → XHR `GetShoppingResults` (batchexecute) | T3 | none | price is **total for all pax** (verified $14,480 = 3 × $4,826.67) | — |
| **Aeroplan** | ⏸ login required | sign-in (Arkose CAPTCHA at login) | page redirects to `/clogin` before any search call | — | — | — | — |
| **Flying Blue** | ⏸ pending | deep link redirects to `/search/advanced`; UI-driven search not yet captured | GraphQL `wwws.airfrance.us/gql/v1` (search op not yet captured) | — | — | — | — |
| **Virgin Atlantic** | ⏸ pending | not yet probed successfully | — | — | — | — | — |

## Request budgets (rate governor, `acquire/governor.py`)

| Site | Budget | Basis |
|---|---|---|
| united | 110 / 5 min | 669 requests with no block at about 0.4 req/s |
| alaska | 140 / 5 min | no blocks at about 0.6 req/s |
| delta | 30 / 5 min (learned cap 36) | Akamai 444 after about 85 requests in 2 min (2026-10-31) |
| american | 10 / 10 min | interactive challenge after short bursts |
| google | 60 / 5 min | no blocks observed |

A learned cap is 60% of the smallest request volume that preceded a burst-induced block
in the last 14 days. A block opens a site cooldown (10 min, doubling, up to 4 h). The
crawler uses half of each budget.

## Known behaviours that matter for accuracy

- **aa.com error codes** (e.g. `309` for CVG→HKG) have unverified meaning. They are
  recorded as *failed* (coverage gap), never as "no flights".
- **aa.com interactive challenge** ("Challenge Validation" / "I'm not a robot") appears
  after bursts of searches. It is detected as *blocked*; the engine cools down (10 min)
  and rotates the session. With `--show-browser` you can solve it in the window and the
  engine resumes. jetbridge never solves CAPTCHAs itself.
- **United** shows a sign-in modal in its UI for award results; the guest-token API
  answers without it (owner's choice, 2026-10-31).
- **Delta** brand ids are prefixed B/C: MAIN/DCP → economy, DPPS → premium,
  D1 → business, FIRST → domestic first. Korean Air legs: KEEC → economy (award class X),
  KEPC → Prestige/business (award class O), seen 2026-11-01. Before that mapping, KE
  space was skipped; 69 stored KE offers (12 business) are now recovered. Unknown brands
  are skipped *with a warning*.
- **Alaska** marks Hawaiian-operated flights "Operated by Alaska as Hawaiian Airlines"
  → operator `HA`.

## Re-running recon
`src/jetbridge/acquire/recon.py` (`run_recon`) records every XHR/fetch JSON exchange
plus a screenshot to `var/recon/<site>/`. Recorded responses become fixtures in
`tests/fixtures/<site>/`.
