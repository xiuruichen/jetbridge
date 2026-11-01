# jetbridge

**Find award seats (and cash fares) across airlines, and get told which one to book.**

jetbridge is a personal, self-hosted award search tool. Think of it as a more technical,
free version of [seats.aero](https://seats.aero) that runs on your own computer. It also
goes a step further: it works out how to seat everyone with the miles and card points you
actually have, compares redemptions against real cash prices, and double-checks the best
options live before recommending one.

You can use it two ways:

- **Talk to it** through Claude Code: *"CVG to Hong Kong or Seoul, late May to early June,
  3 people, prefer business. Other Asia cities are fine. Most of my miles are American,
  then Delta and United."*
- **Type commands** in a terminal: `jetbridge avail CVG HKG,ICN 2027-05-24 2027-06-08 --cabin J`

---

## Contents

1. [How it compares to seats.aero](#how-it-compares-to-seatsaero)
2. [What you need](#what-you-need)
3. [Install](#install)
4. [Quick start: ask Claude](#quick-start-ask-claude)
5. [Quick start: the command line](#quick-start-the-command-line)
6. [How it works, in plain English](#how-it-works-in-plain-english)
7. [Everyday recipes](#everyday-recipes)
8. [Writing a search file](#writing-a-search-file)
9. [Reading the report](#reading-the-report)
10. [Programs and sources covered](#programs-and-sources-covered)
11. [When a site blocks you](#when-a-site-blocks-you)
12. [Troubleshooting and FAQ](#troubleshooting-and-faq)
13. [Using it responsibly](#using-it-responsibly)
14. [Command reference](#command-reference)
15. [For developers](#for-developers)

---

## How it compares to seats.aero

| | seats.aero | jetbridge |
|---|---|---|
| Cost | Free tier; paid subscription for the full feature set | Free; runs on your computer |
| Where the data comes from | Their servers crawl airlines constantly | Your computer searches the airlines' own websites |
| Instant cached search | Yes, very broad | Yes, for routes you have searched or watch |
| Freshness | Updated by their crawler | Your crawler; anything shortlisted is re-checked live |
| Alerts | Yes | Yes (`jetbridge alerts`; push notifications are on the roadmap) |
| Seats for the whole party | Shows seats per flight | **Solves** how to seat everyone: split bookings, mixed cabins, travel-together rules |
| Uses your balances and card points | No | Yes: picks the program and the transfer (Amex, Chase, Citi, Capital One, Bilt) |
| "Is this a good redemption?" | No | Yes: cents per mile against the real cash price, with a "just pay cash" verdict |
| Positioning flights | No | Yes: adds a cash flight to a gateway when that unlocks better space |
| Coverage | Many programs worldwide | United, American, Delta, Alaska/Hawaiian and Google Flights today (more coming) |
| Setup | None | About 10 minutes |

**Bottom line:** seats.aero is broader and needs no setup. jetbridge is narrower, but it
does the thinking part too. It turns "here is some space" into "book these 3 seats with
these miles, transfer these points first, and here is why".

---

## What you need

- **A Windows, macOS or Linux computer** that can stay on while a search runs (about 20
  minutes for a big search).
- **Google Chrome** installed normally. jetbridge drives a real Chrome window, kept off
  screen. Airlines reject "headless" bot browsers.
- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)**, a Python package manager.
- Optional: **[Claude Code](https://claude.com/claude-code)**, if you want to ask in plain
  English instead of typing commands.
- A normal home internet connection. No paid APIs or accounts are needed.

---

## Install

Open a terminal (PowerShell on Windows, Terminal on Mac) in the `jetbridge` folder:

```powershell
python -m pip install --user uv          # skip if you already have uv
uv sync                                  # installs jetbridge and its dependencies
uv run patchright install chromium       # browser driver (your own Chrome does the browsing)
uv run pytest                            # optional: offline self-test, should say "passed"
```

> If `uv` is "not recognized", use `python -m uv` instead of `uv` everywhere in this guide,
> for example `python -m uv run jetbridge avail ...`.

Then tell jetbridge what you have (amounts are examples):

```powershell
uv run jetbridge profile --balance american=400000 --balance delta=250000 --balance united=150000
uv run jetbridge profile --balance amex_mr=120000 --balance chase_ur=80000
```

Currency names: `american`, `delta`, `united`, `alaska`, `aeroplan`, `flyingblue`,
`virginatlantic`, plus the card currencies `amex_mr`, `chase_ur`, `citi_ty`, `capital_one`
and `bilt`. Running `uv run jetbridge profile` with no options shows what's saved.

Finally, check that the airline searches work from your computer. This makes one live
search on each site:

```powershell
uv run jetbridge probe CVG ICN 2027-05-28 --adapters united,delta,alaska
```

Each site should report `ok` (or `empty` if there are genuinely no flights).

---

## Quick start: ask Claude

1. Open the `jetbridge` folder in Claude Code. The included `.mcp.json` connects
   jetbridge automatically. Approve the "jetbridge" server when asked.
2. Ask in your own words, for example:

   > I want to fly CVG to Tokyo or Seoul, sometime May 24 – June 8 2027, 2 adults, business
   > if possible. We'd rather not have overnight layovers. I have 300k AA miles and
   > 100k Chase points.

3. Claude will:
   - ask a few clarifying questions (one-way or round trip? must you sit on the same flights?);
   - show what's already known **instantly** from the cache;
   - start a full search (it reports how much comes from the cache and the estimated time);
   - come back with a **recommendation**, the trade-offs, and a step-by-step booking plan.

Ask follow-ups like *"what if we flew separately?"*, *"is that a good use of miles?"*,
*"what if I value business at $200/hour?"* or *"keep an eye on this route for me"*.
Claude quotes only numbers that jetbridge actually found.

---

## Quick start: the command line

```powershell
# 1. What do we already know? (instant, from the cache)
uv run jetbridge avail CVG HKG,ICN 2027-05-24 2027-06-08 --cabin J --pax 3

# 2. Plan a full search (shows how many queries, how many come from the cache, ETA)
uv run jetbridge plan examples/cvg_asia_3pax.json

# 3. Run it (about 20 min max). It writes var/reports/<search_id>/report.html
uv run jetbridge search examples/cvg_asia_3pax.json

# 4. Read the decision summary in the terminal
uv run jetbridge brief <search_id>

# 5. Keep the route fresh in the background, then check for changes later
uv run jetbridge crawl --detach
uv run jetbridge alerts
```

Open `report.html` in any browser for the full report.

---

## How it works, in plain English

**Award space.** Airlines release a limited number of seats bookable with miles at
reasonable prices ("saver" space). It appears and disappears. jetbridge asks each airline's
own website what it would sell you right now, just as you would by hand, but systematically.

**Programs vs airlines.** You often book one airline with another's miles. For example,
Korean Air flights can be booked with Delta miles, and Japan Airlines with American or
Alaska miles. jetbridge knows these partnerships and which credit-card points transfer to
which programs.

**Cache first, like seats.aero.** Every answer is saved in an **availability store** on
your computer: program × route × date × cabin, with the lowest miles, taxes, seats left,
whether there is a nonstop, and which airlines fly it. Even "no seats" answers are saved.
When you search again, anything still fresh is reused instantly instead of asking the
airline again.

**Freshness.** Space far in the future changes slowly, so answers stay fresh longer:

| Departure is… | An answer is reused for up to… |
|---|---|
| more than 6 months away | 24 hours |
| 2–6 months away | 12 hours |
| 2 weeks to 2 months away | 6 hours |
| 3–14 days away | 2 hours |
| within 3 days | 45 minutes |

Cash fares go stale twice as fast. Whatever jetbridge is about to **recommend** is always
re-checked live first.

**Watches and the crawler.** Every search is automatically **watched** until its travel
dates pass. The **crawler** (`jetbridge crawl --detach`) quietly refreshes watched routes
in the background at a slow, steady pace. It also fills in anything a search didn't have
time for, so the next time you search that trip, most of it is instant. When it notices a
change (new space, a price drop, more seats, or space disappearing), it records an
**alert**.

**Politeness and blocks.** Airline websites use bot protection, which reacts to too many
requests too fast. jetbridge gives every site a request budget (for example, at most 30
Delta searches per 5 minutes) and learns from past blocks to stay under the limit. If a
site does show a "verify you're human" page, jetbridge never hammers it. That site rests
(10 minutes at first, longer if it happens again) and its searches are postponed to the
crawler instead of failing.

**It thinks, not just lists.** After collecting offers, jetbridge:
- prices your preferences: how much business class is worth to you per flown hour, plus
  your time, stops, overnight layovers and risk;
- finds the best way to seat everyone with your balances (including point transfers),
  using a real optimization solver;
- compares each redemption with the cash fare ("2.1¢ per mile: good" or "pay cash");
- shows the alternatives that are better on something you care about, and how robust the
  pick is ("this flips if you value Delta miles above 1.4¢");
- writes a booking plan: re-check → transfer points (irreversible!) → book the scarcest
  seats first → book positioning flights.

**Coverage is never hidden.** Every planned search ends in one of these states:

| State | Meaning |
|---|---|
| `ok` | Answered, with flights |
| `empty` | Answered: genuinely no flights or seats |
| `deferred` | Not run yet (time budget or a resting site); the crawler or `resume` will fill it |
| `blocked` | The site showed a human-verification page |
| `failed` | Error or unrecognized answer: a gap, never assumed to mean "no seats" |
| `skipped` | Needs sign-in you haven't enabled |

The report shows the coverage percentage and lists every gap.

---

## Everyday recipes

**"What's out there?" (instant)**
```powershell
uv run jetbridge avail CVG,DAY,CMH TYO,SEL 2027-05-20 2027-06-10 --cabin J,F --pax 2
uv run jetbridge avail ORD ASIA 2027-09-01 2027-09-30 --max-age-h 24   # only answers from the last day
```
Codes can be airports (`HKG`), metro areas (`TYO`, `OSA`, `SEL`, `SHA`, `BJS`, `NYC`,
`CHI`, `WAS`) or regions (`ASIA_EAST`, `ASIA_SOUTHEAST`, `ASIA_SOUTH`, `ASIA`). The
cached view only knows routes that were searched or watched before. Run a search or add a
watch to grow it.

**"Find me the best option" (full search)**
```powershell
uv run jetbridge search my_trip.json
uv run jetbridge status <search_id>          # progress and coverage while it runs
uv run jetbridge brief <search_id>           # the recommendation
uv run jetbridge explain <option_id>         # why this option, leg by leg
```

**"What if…" (offline, instant: re-solves the same data)**
```powershell
uv run jetbridge whatif <search_id> --balance chase_ur=200000      # more points
uv run jetbridge whatif <search_id> --business-per-hour 200        # business matters more
uv run jetbridge whatif <search_id> --risk-aversion 0.9            # avoid risky itineraries
```

**"Watch this trip for me"**
```powershell
uv run jetbridge watch add my_trip.json      # searches are watched automatically too
uv run jetbridge crawl --detach              # start the background crawler
uv run jetbridge crawl --status              # how much is fresh vs due
uv run jetbridge alerts --unseen --mark-seen # what changed since you last looked
uv run jetbridge watch list
uv run jetbridge watch rm <watch_id>
```
The crawler runs until you stop it (close it in Task Manager / Activity Monitor), or for a
fixed time with `--minutes 120`. Its log is `var/logs/crawler.log`.

**"A search didn't finish everything"**
```powershell
uv run jetbridge resume <search_id>          # retry pending/blocked/failed, re-report
uv run jetbridge resume <search_id> --deep   # also run everything that was deferred
```
Often easier: let the crawler run for a while, then run the same search again. It will be
mostly served from the cache.

**"Use my airline login" (optional)**
```powershell
uv run jetbridge login aeroplan              # a Chrome window opens; you sign in yourself
```
jetbridge never sees your password. It only reuses the signed-in browser profile stored
in `var/profiles/`. Searches are guest-only unless you do this.

---

## Writing a search file

A search is a small JSON file. Copy one from `examples/` and edit it:

```json
{
  "origins": ["CVG"],
  "nearby_origins": ["DAY", "CMH"],
  "destinations": [
    {"code": "HKG", "weight": 1.0},
    {"code": "ICN", "weight": 1.0},
    {"code": "ASIA_EAST", "weight": 0.8}
  ],
  "outbound": {"start": "2027-05-24", "end": "2027-06-08"},
  "inbound":  {"start": "2027-06-15", "end": "2027-06-25"},
  "passengers": 3,
  "preferred_cabin": "J",
  "min_cabin": "Y",
  "together": "same_flights"
}
```

| Field | What it means | Default |
|---|---|---|
| `origins` | Where you start (airports or metros) | required |
| `nearby_origins` | Other airports you'd drive to | none |
| `destinations` | Airports, metros or regions. `weight` 1.0 = what you really want, 0.6–0.8 = "also fine" | required |
| `outbound` / `inbound` | Date windows. Leave out `inbound` for one-way | one-way |
| `passengers` | Party size (1–9). Prices really do depend on this | required |
| `preferred_cabin` / `min_cabin` | `Y` economy, `W` premium economy, `J` business, `F` first | `J` / `Y` |
| `together` | `same_flights`, `same_day`, or `any` (fine to split up) | `same_flights` |
| `max_stops`, `max_duration_hours` | Hard limits | 3, 40 h |
| `include_cash` | Also compare Google Flights cash fares | true |
| `include_positioning` | Allow a separate cash flight to a better gateway | true |
| `programs` | Only use these programs, e.g. `["american","alaska"]` | all you can reach |
| `time_budget_minutes` | Target total run time; the rest is deferred, never dropped | 20 |
| `preferences` | Your trade-offs in USD per passenger (see below) | sensible defaults |

`preferences` (all optional):

| Field | Meaning | Default |
|---|---|---|
| `cabin_premium_usd_per_hour` | What each cabin is worth to you per flown hour | `{"Y":0,"W":45,"J":130,"F":170}` |
| `value_of_time_usd_per_hour` | Cost of each extra hour of travel | 30 |
| `stop_penalty_usd` | Dislike of each connection | 60 |
| `overnight_layover_penalty_usd` | Dislike of an overnight connection (incl. hotel) | 300 |
| `airport_change_penalty_usd` | Changing airports mid-trip (e.g. NRT → HND) | 150 |
| `separate_ticket_penalty_usd` | Risk of a missed connection on separate tickets | 120 |
| `risk_aversion` | 0 = don't care, 1 = strongly prefer safe options | 0.5 |

If you use Claude, you never need to write this file. Claude builds it from your words
and tells you what it set.

---

## Reading the report

`var/reports/<search_id>/report.html` (also `.md`) contains:

1. **Coverage line:** how many planned searches were answered, plus any gaps.
2. **Decision brief:** the recommended option (with an ID like `o_ab12cd`), what it costs
   in miles, cash and hours, its risk, and whether the live re-check **confirmed** it.
3. **Redemption value:** cents per mile achieved vs. the real cash fare, with a verdict.
4. **Alternatives:** options better than the pick on at least one thing (cheaper,
   faster, more business-class hours…), each with the exact trade-off.
5. **How robust is this pick?:** which valuations would change the answer. FRAGILE means
   a small change flips it.
6. **Action plan:** re-check → transfer points → book in order → positioning.
7. **All options** and the **coverage ledger** (every search, its result and why it ran).

Point transfers can't be undone. Only transfer after you've re-confirmed the seats on the
airline's site.

---

## Programs and sources covered

| Program / source | Status | What you get |
|---|---|---|
| United MileagePlus | ✅ live | Awards incl. Star Alliance partners; seats by fare bucket |
| American AAdvantage | ✅ live (strictest rate limit) | Awards incl. oneworld partners (JAL, Cathay, Qatar…) |
| Delta SkyMiles | ✅ live | Awards incl. Korean Air, Virgin Atlantic, Air France/KLM; exact seats |
| Alaska / Hawaiian (Atmos) | ✅ live | Awards incl. oneworld + partners; exact seats |
| Google Flights | ✅ live | Cash fares for comparison and positioning |
| Air Canada Aeroplan | ⏸ needs your login (`jetbridge login aeroplan`) | |
| Flying Blue, Virgin Atlantic | 🚧 planned | Reachable today via the transfer model, not yet searched |

Card points modeled: Amex Membership Rewards, Chase Ultimate Rewards, Citi ThankYou,
Capital One, Bilt (with transfer ratios to the programs above).

---

## When a site blocks you

Occasionally a site shows a "verify you are human" page. What happens:

1. jetbridge stops searching that site immediately. It never retries into the check.
2. The site **rests** for 10 minutes, then 20, 40… (up to 4 hours) if it keeps happening.
   Every jetbridge process respects this, including the crawler and new searches.
3. Searches planned for that site are marked **deferred** and filled later by the crawler.
4. After the rest, a single test request goes first before normal searching resumes.

See what's going on:
```powershell
uv run jetbridge health
```
This shows, per site: requests used / allowed in the current window, the limit learned
from past blocks, and any remaining rest time.

Want to clear it yourself? Run with a visible browser and tick the check box in the
Chrome window. jetbridge waits for you and continues:
```powershell
uv run jetbridge resume <search_id> --show-browser
```
jetbridge never solves these checks automatically.

---

## Troubleshooting and FAQ

**The cached view (`avail`) shows nothing.** It only knows routes you've searched or
watched. Run a search or `watch add`, then `crawl`.

**A search says lots of queries are "deferred".** That's the time budget or a site
resting, not an error. Start the crawler and re-run later, use `resume --deep`, or raise
`time_budget_minutes`.

**American is slow.** aa.com tolerates only about 10 searches per 10 minutes before
asking for human verification, so most AA searches go to the crawler. The crawler fills
them steadily in the background.

**Prices differ by party size?** Yes. United and American charge more per person for
larger parties on some flights, so jetbridge searches with your real party size. Delta
and Alaska were verified to charge the same per person, so one answer covers any party
size (limited by seats left).

**"failed" with an error code (e.g. aa.com `309`).** The site returned an answer we can't
interpret with certainty. It's reported as a gap, never as "no seats". The crawler backs
off from queries that keep failing.

**Is the cached price guaranteed?** No. It's what the airline showed at the listed age.
Anything jetbridge recommends is re-checked live, and you should still confirm on the
airline's site before transferring points.

**Where is my data?** Everything stays on your computer in `var/`: the database,
compressed raw responses, browser profiles and reports. Delete `var/` to reset entirely.
`jetbridge backfill` rebuilds the availability store from saved raw responses.

**`uv` not found.** Use `python -m uv ...`.

**Chrome windows appear on screen.** They should stay off screen. Pass `--show-browser`
only when you want to see them.

---

## Using it responsibly

jetbridge is for **personal trip planning**. It searches the same public pages you would
by hand, at a deliberately slow pace, from your own connection. It does not solve
CAPTCHAs, share data, or resell anything. Airline websites have their own terms of use;
keep volumes personal, and don't run it as a service for others. Logins are optional and
always done by you.

---

## Command reference

| Command | What it does |
|---|---|
| `jetbridge avail ORIG DEST START END [--cabin] [--pax] [--max-age-h]` | Instant cached availability |
| `jetbridge plan SPEC.json` | Show the search plan: queries, cache hits, deferrals, ETA |
| `jetbridge search SPEC.json [--show-browser]` | Full search and report |
| `jetbridge status ID` | Progress and coverage |
| `jetbridge brief ID` / `explain OPTION` | Recommendation / one option in detail |
| `jetbridge whatif ID [--balance] [--business-per-hour] [--risk-aversion] …` | Re-solve offline under new assumptions |
| `jetbridge resume ID [--deep] [--show-browser]` | Continue a search |
| `jetbridge report ID` / `reparse ID` | Re-render / re-parse stored data (no network) |
| `jetbridge crawl [--detach] [--minutes N] [--once] [--status]` | Background crawler |
| `jetbridge watch add SPEC.json` / `list` / `rm ID` | Manage watched trips |
| `jetbridge alerts [--unseen] [--mark-seen]` | Changes seen: new space, drops, gone |
| `jetbridge health` | Request budgets, learned limits, site rest times |
| `jetbridge profile [--balance x=n] [--cpp x=cents] [--clear]` | Balances and valuations |
| `jetbridge login SITE` | Optional signed-in session (you sign in) |
| `jetbridge probe ORIG DEST DATE [--adapters] [--pax]` | Live health check per site |
| `jetbridge backfill` | Rebuild the availability store from saved responses |
| `jetbridge kb` | Show the knowledge base (partners, transfers, freshness) |

Every command has `--help`.

---

## For developers

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):** pipeline, rate governor, cache and
  freshness rules, solver and advisor internals, accuracy guarantees.
- **[docs/sources/README.md](docs/sources/README.md):** per-site endpoints, quirks,
  request budgets and recon notes.
- **[CLAUDE.md](CLAUDE.md):** invariants that must not be broken, and how Claude uses the
  MCP tools.
- Tests: `uv run pytest` (offline: parsers on recorded responses, solver property tests,
  governor/cache/crawler tests, end-to-end pipeline).

### Roadmap

1. Aeroplan (signed in), Flying Blue and Virgin Atlantic adapters.
2. Push notifications for alerts (ntfy/email) and per-watch alert rules.
3. Calendar endpoints to cover more dates per request.
4. A small local web UI for the cached search.
