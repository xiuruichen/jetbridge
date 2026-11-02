# jetbridge

jetbridge watches award seats on one specific flight and emails you when they open up.

You tell it the flight, cabin and dates, for example *AA100 in business on June 14-16, 2027,
bookable with American miles*. It searches aa.com several times a day on your own PC, and
emails you the price and a booking link as soon as seats appear.

It searches as a guest with no login, and it doesn't book anything or move your miles.

One computer does the searching, and each watch can alert different people, so you can run it
for friends or family without them installing anything.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Adding a watch](#adding-a-watch)
- [Sending alerts to other people](#sending-alerts-to-other-people)
- [Checking on your watches](#checking-on-your-watches)
- [The emails you'll get](#the-emails-youll-get)
- [Customizing alert headlines](#customizing-alert-headlines)
- [When you get a SEATS OPEN email](#when-you-get-a-seats-open-email)
- [How checks work](#how-checks-work)
- [Limitations](#limitations)
- [Blocks and aa.com's terms](#blocks-and-aacoms-terms)
- [Using it through Claude](#using-it-through-claude)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)
- [Where your data lives](#where-your-data-lives)
- [Uninstalling](#uninstalling)
- [Development](#development)

## Requirements

- **Windows**, because automatic checks use Windows Task Scheduler
- **Google Chrome**, installed normally
- **[uv](https://docs.astral.sh/uv/)**, which installs Python 3.12 and the other dependencies
- **A Gmail account with 2-Step Verification**, to send the alerts. They can go to any address

## Quick start

### 1. Install

```powershell
git clone https://github.com/xiuruichen/jetbridge.git
cd jetbridge
```

If the project is in a synced folder such as OneDrive, keep its Python environment outside
that folder. Run this once, then open a new PowerShell window in the project folder:

```powershell
setx UV_PROJECT_ENVIRONMENT "$env:LOCALAPPDATA\jetbridge\venv"
```

Then install:

```powershell
uv sync
```

### 2. Set up email

Create an app password at <https://myaccount.google.com/apppasswords>, then run:

```powershell
uv run jetbridge notify setup
```

It asks for the Gmail address to send from, your own address for alerts and health warnings, and
the app password. Then it sends a test email. If nothing arrives, check your spam folder.

### 3. Add a flight

```powershell
uv run jetbridge watch add AA100 --from JFK --to LHR --dates 2027-06-14..2027-06-16 --cabin business
```

It asks who should get the seat alerts. Type one or more addresses separated by commas, or press
Enter to send them to yourself. It then prints a watch ID, such as `[1]`, which the other commands
use.

### 4. Try one check

```powershell
uv run jetbridge check 1
```

This takes a minute or two, because searches are spaced out on purpose. You should see a result
for each date:

```text
[1] AA100 JFK-LHR business Jun 14: not_offered (no business seats)
[1] AA100 JFK-LHR business Jun 15: not_offered (no business seats)
[1] AA100 JFK-LHR business Jun 16: not_offered (no business seats)
3 check(s), 0 email(s) sent.
```

### 5. Turn on automatic checks

```powershell
uv run jetbridge schedule install
```

### 6. Confirm everything works

```powershell
uv run jetbridge doctor
```

Every line should say `OK`. Anything marked `FAIL` tells you how to fix it.

You're done. You don't need to keep a window open, but the PC has to stay on, awake and logged
in. See [Limitations](#limitations).

## Adding a watch

```text
jetbridge watch add FLIGHT --from AAA --to BBB --dates DATES [options]
```

| Option | Default | What it does |
|---|---|---|
| `FLIGHT` | required | Flight number, such as `AA100` |
| `--from`, `--to` | required | The airports this flight actually flies between |
| `--dates` | required | Up to 14 dates: `2027-06-14..2027-06-16`, `2027-06-14,2027-06-18`, or a mix |
| `--cabin` | `business` | `economy`, `premium`, `business` or `first` |
| `--pax` | `1` | Number of passengers to search for |
| `--checks-per-day` | `6` | How often to check, from 1 to 24 |
| `--max-miles` | none | Treat seats above this per-person price as not available |
| `--notify` | asks you | Who gets seat alerts: `a@x.com,b@y.com`, or `me` for only you |
| `--program` | `american` | Only American is supported today |

### Getting these right

- **Route:** use the airports of that exact flight, because aa.com searches by route. A flight
  tracker will show them. With a wrong route, the flight simply never appears.
- **Dates:** use the local departure date at the origin airport.
- **Connections:** only the flight on its own counts. An itinerary that connects onto it from
  another city doesn't, so watch each flight separately if you need more than one.
- **Cabin:** every leg has to be in the cabin you asked for. If aa.com sells a "business" fare
  that's actually flown in another cabin, it doesn't count. A first-class seat doesn't count for
  a business watch either, so add a second watch if you'd take either.
- **Party size:** a watch for 2 only reports seats aa.com offers to a party of 2. If you could
  split up, add a separate watch for 1. Prices can also change with party size.
- **Checks per day:** this is per date, so 6 checks across 3 dates is about 18 searches a day.
  Be conservative: more searching makes a block more likely.

### Examples

```powershell
# Two passengers, only if 120,000 miles or less each
uv run jetbridge watch add AA100 --from JFK --to LHR --dates 2027-06-14,2027-06-16 --pax 2 --max-miles 120000

# First class, checked eight times a day
uv run jetbridge watch add AA100 --from JFK --to LHR --dates 2027-06-14 --cabin first --checks-per-day 8
```

### Managing watches

```powershell
uv run jetbridge watch list        # every watch
uv run jetbridge watch pause 1     # stop checking, keep the history
uv run jetbridge watch resume 1    # start again, with a check on the next run
uv run jetbridge watch rm 1        # delete the watch and its history
```

To change a watch's dates or settings, remove it and add it again. Watches finish on their own
once all their dates have passed.

## Sending alerts to other people

Each watch has its own list of up to 10 people. They don't need to install anything, and they don't
need a Gmail address.

```powershell
# Asks who to alert
uv run jetbridge watch add AA100 --from JFK --to LHR --dates 2027-06-14 --cabin business

# Or say it up front
uv run jetbridge watch add AA100 --from JFK --to LHR --dates 2027-06-14 --notify "ann@example.com,bo@example.org"

# Make sure an address works before relying on it
uv run jetbridge notify test --to ann@example.com

# See or replace who a watch alerts ("me" means only you)
uv run jetbridge watch recipients 1
uv run jetbridge watch recipients 1 ann@example.com,carol@example.com
uv run jetbridge watch recipients 1 me
```

Replacing the list takes effect from the next alert. To include yourself alongside other people, add
your own address to the list.

| Email | Who gets it |
|---|---|
| Seat alerts: open, unconfirmed, price drop, more seats, still open, gone | That watch's list, or you if the list is empty |
| Daily summary and warnings about the watcher | Only you |

A few things to know:

- **Privacy:** each person gets a separate email and can't see who else is on the list. Your Gmail
  address is the sender, and replies come to you.
- **Footer:** emails to other people say that someone added them to a flight watch, and to reply if
  they'd like to be removed. Only add people who want these, and remove anyone who asks.
- **Different people, different flights:** add a watch per person or group. Watches on the same
  route, date and party size share one search, so this doesn't add searches.
- **Party size:** each watch's `--pax` should match that traveler's group.
- **Bad addresses:** if Gmail refuses an address outright, everyone else still gets their alert and
  you're emailed what that person missed. If an address bounces later, the bounce notice comes to your
  Gmail instead, so test new addresses with `notify test --to`.
- **Spam filters:** ask each person to mark the first email as "not spam". A missed alert is the
  main risk, because other people don't get the daily summary and won't notice if it stops.

## Checking on your watches

```powershell
uv run jetbridge status       # the latest state of every date, and the next check
uv run jetbridge history 1    # past results for watch 1, newest first
uv run jetbridge doctor       # is everything working?
```

Each result is one of these:

| Result | Meaning |
|---|---|
| `available` | The flight has seats in your cabin, within any miles limit |
| `not_offered` | aa.com listed the flight, but with no qualifying seats |
| `not_listed` | aa.com answered, but this flight wasn't in the results |
| `unknown` | jetbridge couldn't tell, for example because of a block or an error |

**`unknown` never means "no seats".** It can't trigger a seats alert, and it can't mark an open
date as closed.

## The emails you'll get

| Email | When it's sent |
|---|---|
| **SEATS OPEN** | Seats appeared, and a live re-check 1-2 minutes later confirmed them |
| **SEATS MAY BE OPEN (unconfirmed)** | Seats appeared, but the re-check couldn't get a clear answer |
| **PRICE DROP** | An open date got cheaper |
| **MORE SEATS** | An open date gained seats (only when aa.com states a count) |
| **STILL OPEN** | One reminder a day while seats stay open |
| **Seats gone** | An open date closed |
| **jetbridge can't see...** | No clear answer on any date for 24 hours |
| **aa.com's page format may have changed** | Two checks in a row couldn't be read, so the parser may need fixing |
| **jetbridge daily** | A summary, on the first run after 8 AM |

If seats appear but are gone by the re-check, it's logged in `history` and no email is sent.

**If the daily email stops arriving, the watcher isn't running.** The PC can't send anything
while it's off, so that missing email is your warning. To turn the summary off, run
`uv run jetbridge notify digest off`, but you'd lose that signal.

If sending fails, for example when you're offline, the email stays queued and is retried on later
runs. Seat alerts wait until they can be sent, so check the `Observed` time. Daily summaries more
than a day old are dropped.

## Customizing alert headlines

You can reword the headline at the start of each seat alert's subject:

```powershell
uv run jetbridge notify headline                                      # show all of them
uv run jetbridge notify headline seats_open "DEAL FOUND, BOOK NOW"    # change one
uv run jetbridge notify headline seats_open --reset                   # back to the built-in text
```

The alerts are `seats_open`, `seats_open_unconfirmed`, `price_drop`, `more_seats`, `reminder` and
`seats_gone`. The flight, cabin, date and price still follow the headline. With the headline above,
a subject reads `DEAL FOUND, BOOK NOW: AA100 business on Jun 14, 2027 - 110,000 mi`.

This changes the wording only, not when alerts are sent. Headlines are saved in `alerts.json` in your
state folder, so they stay on your PC and survive `notify setup`. If that file ever can't be read,
alerts still go out with the built-in wording and `doctor` tells you.

## When you get a SEATS OPEN email

1. **Open the link in the email.** It goes to aa.com's results for that route, date and party
   size, without logging you in.
2. **Find your flight** and check the price, cabin and taxes. Award seats can disappear in minutes.
3. **Book it yourself.** If you need to transfer points first, confirm the seats are still there,
   because transfers generally can't be undone.

A few details in the email:

- **`Seats: at least 1`** means aa.com didn't state a count, which is common. The number is the
  party size searched, and the real count may be higher.
- **`Note: dynamic pricing`** comes from aa.com's data, and that price can change.
- **`Note: aa.com says this must be booked at the airport`** comes from aa.com too, so check it
  before planning around that fare.

## How checks work

1. Every 15 minutes, Windows runs `jetbridge tick`, which does whatever is due and exits.
2. Each watch's next check is set from its checks per day, with random variation of 30% either
   way. At 6 a day, that's roughly every 3-5 hours.
3. Chrome opens off-screen and searches aa.com, waiting 20-60 seconds between searches. Watches on
   the same route, date and party size share one search.
4. If seats have just appeared, it waits 1-2 minutes and searches again before alerting you.
5. Results are saved, emails are sent and Chrome closes.

After sleep or downtime it runs one catch-up check, never a burst of missed ones.

## Limitations

- **It only runs while this PC is on, awake and logged in.** Chrome needs a desktop session, so
  checks stop while the PC sleeps or is signed out. To catch seats overnight, set Windows sleep
  to **Never** in *Settings > System > Power*.
- **Short-lived seats can be missed.** Checks are hours apart, so seats that open and close between
  them are never seen. More checks per day narrows that gap but raises the risk of a block.
- **A flight that's `not_listed` isn't necessarily wrong.** aa.com may leave out flights with no
  award space. If a flight has never appeared after 6 clear checks on every date, the daily email
  suggests checking the flight number and route.
- **Codeshares:** jetbridge matches the flight number aa.com shows. If aa.com lists the flight under
  a different number, watch that number instead.
- **American only.** It shows what aa.com offers for American miles, including partner flights like
  Cathay Pacific. Other programs may see different space.
- **aa.com can change without notice.** If its pages change, checks turn `unknown`, and you get an
  email saying so, but jetbridge won't work again until the parser is updated.

## Blocks and aa.com's terms

Automated searching likely goes against aa.com's terms of use, so you're accepting that risk by
running this. jetbridge keeps the risk low: it never logs in, so your AAdvantage account isn't
involved, and it searches at low volume (never more than 10 requests per 10 minutes).

If aa.com shows a "confirm you're human" page or blocks a request, jetbridge:

1. Stops searching immediately and doesn't retry.
2. Rests for 10 minutes, doubling each time it happens again within 6 hours, up to 4 hours.
3. Records the checks it skipped as `unknown` and reschedules them for after the rest.

It never tries to solve the human check. `status` shows any rest in progress. If blocks keep
happening, lower `--checks-per-day`.

## Using it through Claude

Open [Claude Code](https://claude.com/claude-code) in this folder and approve the `jetbridge` MCP
server when asked. Then you can ask things like:

- "Watch AA100 from New York to London in business on June 14-16."
- "What's happened with my watches this week?"
- "Is the watcher healthy?"

It uses the same engine as the commands above, so results and alerts are the same. Claude is
optional: checks and emails never depend on it. It needs `uv` on your PATH.

## Command reference

Run these as `uv run jetbridge ...` from the project folder. Add `--help` to any command for details.

| Command | What it does |
|---|---|
| `watch add FLIGHT ...` | Start watching a flight |
| `watch list` | List every watch |
| `watch recipients ID [ADDRESSES]` | Show or replace who gets seat alerts |
| `watch pause ID` / `watch resume ID` | Stop or restart checking |
| `watch rm ID` | Delete a watch and its history |
| `status` | Current state of every active watch |
| `history ID [--limit N]` | Past results for a watch |
| `check ID` | Check one active watch now, still within the rate limit |
| `tick` | Run whatever is due (the scheduled task calls this) |
| `doctor` | Find out what's wrong |
| `notify setup` | Save Gmail details and send a test email |
| `notify test [--to ADDRESS]` | Send a test email to you or someone else |
| `notify digest on` / `off` | Turn the daily summary on or off |
| `notify headline [KIND] [TEXT]` | Show or change seat alert headlines |
| `schedule install` / `remove` | Add or remove the scheduled task |

## Troubleshooting

Start with `uv run jetbridge doctor`, which covers most problems.

**`uv` isn't recognized.** Close and reopen PowerShell. If it still fails, install uv and make sure
its folder is on your PATH.

**The test email fails with a login error.** Use an app password, not your normal Gmail password,
and make sure 2-Step Verification is on. Google Workspace accounts, including school ones, may have
app passwords turned off, so try a personal Gmail. Run `notify setup` again to replace the details.

**`watch add` says no recipient was given.** It couldn't ask you, for example because it was run
from a script. Add `--notify me` or `--notify a@x.com`.

**The test email never arrives.** Check spam, then mark it as "not spam" so alerts land in your inbox.

**A check says `blocked`, or `status` says aa.com is RESTING.** This is normal. Leave it alone and
checks resume after the rest. Running `check` during a rest won't get around it.

**Everything is `unknown` with a site error code.** aa.com returned an error that jetbridge can't
interpret, so it reports "couldn't tell". Check `uv run jetbridge history ID`. The raw response is
saved for diagnosis.

**A flight is always `not_listed`.** Check the flight number, route and date against a flight
tracker, and confirm the airline has opened its schedule for those dates. Then search the route
yourself on aa.com to see how the flight is listed.

**"another jetbridge check is already running".** Only one check can use its Chrome profile at a
time. Wait for it to finish and try again.

**No checks seem to be running.** Make sure you're logged in and `doctor` shows the task as installed,
then compare `Last tick` in `status` with the current time. Scheduled output is written to
`%LOCALAPPDATA%\jetbridge\jetbridge.log`. Runs with nothing due don't log anything.

**You see Chrome in the taskbar.** That's jetbridge running a check. Its window is off-screen and
closes by itself. It uses its own profile, so it doesn't touch your normal Chrome data. To watch
it work, run this and then do a `check`:

```powershell
$env:JETBRIDGE_SHOW_BROWSER = "1"
```

## Where your data lives

Everything is on your PC. Nothing goes anywhere except searches to aa.com and emails through Gmail.

| What | Where |
|---|---|
| Database, config, custom headlines and log | `%LOCALAPPDATA%\jetbridge\` |
| jetbridge's Chrome profile | `%LOCALAPPDATA%\jetbridge\profiles\` |
| Failed responses, for repairs (newest 50) | `%LOCALAPPDATA%\jetbridge\raw\` |
| Gmail app password | Windows Credential Manager, under `jetbridge-gmail` |

Set the `JETBRIDGE_HOME` environment variable to use a different folder.

## Uninstalling

```powershell
uv run jetbridge schedule remove
```

Then, to remove the rest:

1. Delete `%LOCALAPPDATA%\jetbridge`.
2. Remove the `jetbridge-gmail` entry from Windows Credential Manager, and revoke the app password
   in your Google account.
3. Delete the project folder.

## Development

```powershell
uv run pytest               # offline, using recorded aa.com responses
uv run ruff check .
uv run ruff format .
```

| Path | What's in it |
|---|---|
| `src/jetbridge/matcher.py` | Turns an aa.com answer into a result for one flight (pure) |
| `src/jetbridge/alerts.py` | Alert rules and email text (pure) |
| `src/jetbridge/scheduler.py` | What's due, timing and shared searches (pure) |
| `src/jetbridge/runner.py` | One full run: check, confirm, alert, report health |
| `src/jetbridge/store.py` | SQLite storage and the email queue |
| `src/jetbridge/acquire/american.py` | aa.com requests and response parsing |
| `src/jetbridge/acquire/detect.py` | Recognizes blocks and human checks |
| `src/jetbridge/acquire/governor.py` | Rate limit and rest periods |
| `src/jetbridge/acquire/broker.py`, `fetcher.py` | Chrome session and classified answers |
| `src/jetbridge/notify/email.py` | Gmail sending and queue flushing |
| `src/jetbridge/winsched.py` | The Windows scheduled task |
| `src/jetbridge/cli.py`, `mcp_server.py`, `service.py` | Commands, Claude tools and shared wiring |

Set `JETBRIDGE_KEEP_RAW=1` to save successful responses too, which is useful for recording test
fixtures. Rules that must not be broken, such as "only a recognized answer can mean no seats", are
listed in [`CLAUDE.md`](CLAUDE.md).
