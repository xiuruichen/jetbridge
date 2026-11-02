# jetbridge

Watches award seats on one specific flight and emails you when they open up.

For example: CX805 in business through American miles on May 21-23, 2027, checked about six
times a day.

## Setup

You need Windows, Google Chrome and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
uv run jetbridge notify setup       # Gmail address and app password; sends a test email
uv run jetbridge watch add CX805 --from YYZ --to HKG --dates 2027-05-21..2027-05-23 --cabin business
uv run jetbridge check 1            # one live check, to confirm it works
uv run jetbridge schedule install   # run automatically from now on
uv run jetbridge doctor             # confirm everything is healthy
```

Use the flight's real departure and arrival airports. aa.com searches by route, so a wrong
route looks like a flight that never appears. CX805 is listed by flight trackers as Toronto to
Hong Kong; confirm your flight's route for your dates.

## What you'll get by email

| Email | When |
|---|---|
| **SEATS OPEN** | Seats appeared and a live re-check 1-2 minutes later confirmed them |
| **SEATS MAY BE OPEN (unconfirmed)** | Seats appeared, but the re-check couldn't tell |
| **PRICE DROP / MORE SEATS** | An open date got cheaper or gained seats |
| **STILL OPEN** | One reminder a day while seats stay open |
| **Seats gone** | An open date closed |
| **jetbridge can't see...** | No clear answer for 24 hours. Silence doesn't mean no seats |
| **page format may have changed** | aa.com responses can't be read. It needs a parser fix |
| **jetbridge daily** | A summary each morning |

**If the daily email stops arriving, the watcher isn't running.** The PC can't send anything
while it's off.

## Good to know

- **It only runs while this PC is on, awake and logged in.** Checks pause during sleep, and
  one catch-up check runs on waking. To catch seats overnight, stop the PC from sleeping.
- **Check results:** `available`, `not_offered` (listed with no qualifying seats), `not_listed`
  (not in the results) and `unknown` (couldn't tell, which never counts as no seats).
- **Flights aa.com doesn't show:** aa.com may leave out flights with no award space. If a
  flight never appears, the daily email suggests checking the flight number and route.
- **"At least N" seats:** aa.com often hides the count, so N is the party size searched.
- **Always confirm on aa.com before moving points.** Award space can vanish, and transfers
  can't be undone.

## Blocks

Automated searching likely goes against aa.com's terms. jetbridge searches as a guest with no
AAdvantage login, at low volume (at most 10 requests per 10 minutes, and usually far fewer).

If aa.com shows a human check, jetbridge stops immediately and rests for 10 minutes, doubling
up to 4 hours if it keeps happening. Those checks are recorded as `unknown`. It never tries to
solve the check.

## Commands

```text
jetbridge watch add FLIGHT --from AAA --to BBB --dates ... [--cabin business] [--pax 1]
                           [--checks-per-day 6] [--max-miles N]
jetbridge watch list | pause ID | resume ID | rm ID
jetbridge status                 current state of every watch
jetbridge history ID             past results for a watch
jetbridge check ID               check now
jetbridge doctor                 find out what's wrong
jetbridge notify setup | test | digest on|off
jetbridge schedule install | remove
```

Run them with `uv run` from this folder. Logs from scheduled runs are in
`%LOCALAPPDATA%\jetbridge\jetbridge.log`, next to the database and saved failed responses.

## Using it through Claude

In Claude Code in this folder, the `jetbridge` MCP server lets you say things like "watch CX805
in business on May 21-23" or "what's happened with my watch?". It uses the same engine as the
commands above.
