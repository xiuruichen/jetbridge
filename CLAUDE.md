# jetbridge

Watches award availability for one specific flight, cabin and program (American, Delta or
United) on chosen dates, and emails when seats open. The deterministic engine does all checking and alerting. Claude is
an optional layer through the `jetbridge` MCP server and is never needed for a check to run.

## Dev
- `uv run pytest` runs offline on recorded fixtures.
- `uv run jetbridge check ID` makes live requests. Keep live testing low-volume.
- State lives in `%LOCALAPPDATA%\jetbridge\` (database, Chrome profile, saved responses,
  custom headlines), never in the project folder, which may be in a synced folder. If it is,
  point `UV_PROJECT_ENVIRONMENT` at a venv outside it.

## Layout
`models.py` types · `matcher.py` response to result (pure) · `alerts.py` alert rules and text
(pure) · `scheduler.py` due, jitter, grouping (pure) · `store.py` SQLite · `runner.py` one tick ·
`acquire/` `sites.py` (registry), `american.py`, `delta.py`, `united.py` (fetch and pure parse),
`pagefetch.py`, `detect.py`, `governor.py` (per-site budgets), `broker.py`
(Chrome), `fetcher.py` · `notify/email.py` Gmail and outbox · `winsched.py` scheduled task ·
`draft.py` a watch being entered, friendly input · `wizard.py` `jetbridge start` questions ·
`headlines.py` custom alert wording · `service.py` shared wiring · `cli.py`, `mcp_server.py` ·
`claude_desktop.py` connects Claude Desktop · `doctor.py` health checks · `uninstall.py` removal ·
`platforms.py`, `autorun.py`, `launchd.py` what differs on a Mac ·
`insights.py` what a watch's record shows (pure) · `power.py` sleep settings · `report.py` problem reports · `updater.py` new
versions · `notify/push.py` phone alerts · `board.py` the in-chat page.

## Invariants (do not break)
- Only a positively recognized answer can mean "no seats". Blocks, errors, site error codes,
  unexpected pages and unreadable results that might be the flight are all `UNKNOWN`.
- `UNKNOWN` never sends a seats alert and never changes a date's open or closed status.
- `AVAILABLE` requires the exact flight between the watched airports on that date, with every
  leg in the requested cabin. An `any` watch is chosen explicitly in the review, judges each nonstop
  by those same rules, and never counts a connection. Another flight opening on one gets its own
  live re-check and alert, tracked in `open_flights`.
- Searches use the watch's real party size, because award prices can change with it.
- Every live request goes through `Governor.acquire`, including the browser's first page load,
  United's guest token, each Delta result page and the navigation fallback. A site's `fetch` calls
  its `gate` before every request. A block is never retried: it opens a persisted cooldown for
  that site. Don't add retries or ways around this. Challenges are never solved automatically.
- Sites are independent: a block, rest or format warning on one never stops or clears another.
- A seat's cabin comes from what's flown on each leg, not the product name. Partner fares need no
  per-airline code: American and United state the leg's cabin, and Delta's partner brands are
  worked out from its cabin columns by `delta.learn_brands`. It only trusts a brand heading its
  column, lets the response override the usual column meanings, and gives up on conflicting or
  missing evidence. Don't loosen that: a wrong cabin sends a false alert.
- An unrecognized fare (`UnknownFare`) never hides readable seats on that flight, but it makes "no
  seats" `UNKNOWN`, as do a cut-off United stream and a missing Delta page.
- Alerts come from recorded state changes. `Store.commit_check` writes the observation, state
  and alerts in one transaction, and email goes only through the outbox.
- An opening is re-checked live before its alert goes out.
- Seat alerts go only to the watch's recipients (the owner if none), one email per person so
  addresses are never shared. Health warnings and digests go only to the owner.
- Custom headlines change wording only, and a bad headlines file falls back to the built-in text.
- Every way of adding a watch shows the draft review and saves only after a yes. Never guess a
  flight number or an address. The flight picker's search goes through `Governor.acquire` and isn't
  retried, and a flight missing from its list doesn't mean the flight doesn't exist.
- Connecting Claude Desktop changes only the `jetbridge` entry in its config. A file that can't be
  understood is left alone, and a copy is kept before any change.
- Everything the commands can do is available through the MCP tools, except that the Gmail app
  password is never typed into a chat: `open_email_setup` opens a private window for it.
- In chat, as in `jetbridge start`, nothing is searched or saved until the notice is accepted, and
  removing a watch shows what will be deleted and waits for a yes.
- MCP tool calls return within about 50 seconds, because Claude Desktop gives up at about 60. Longer
  work finishes on its own thread and is collected with another call.
- Claude Desktop can run two copies of the MCP server and restart them, so anything a later tool
  call needs, such as a review waiting for a yes, is saved in the store, not in memory.
- Adding someone to a watch's alerts never drops anyone: `set_recipients` adds, removes or replaces.
- Tests never reach anything real. `conftest.py` makes opening Chrome, sending email and running
  powercfg fail, so anything new that calls `service.live_tick` needs a fake in its tests.
- A date isn't searched until `scheduler.is_searchable`, and an unopened date never counts toward
  the "no clear answer" warning. Insights report what was seen and never predict.
- Pacing decides which dates to search on a tick and never changes what a result means. A manual
  check isn't paced. No site gets more than `governor.DAILY_LIMIT` requests in 24 hours, and
  `plan_searches` stops planning at `Governor.room_today`, except for dates past their minimum pace.
  Bursts spend from that budget and never add to it, or get around a rest. Nothing adaptive may be
  described as a prediction.
- Seats above a price limit are recorded with their price but stay `NOT_OFFERED`. Other flights in
  an answer are only mentioned in the daily summary and chat, never as alerts, and never trigger a
  search. Editing a watch can't change its flight, airports, cabin, program or party size, because
  its history would stop applying.
- Phone alerts are off until turned on, and each seat alert still goes by email. The channel name
  never goes in a chat. Phone and email delivery can't hold each other up.
- An update only fast-forwards and refuses a folder with local changes. Claude Desktop runs the
  server through python, so it doesn't lock a program the update has to replace.
- The in-chat board only shows things. It has no outside scripts, puts everything on the page as
  plain text, and anywhere that can't draw it still gets the same facts as data.
- The Mac version is experimental. Don't claim anything about it that hasn't been seen on a real
  Mac, and keep the notice in the README, the guide's requirements, the installer and `doctor` until `docs/mac-testing.md` has
  been done. Anything that differs by system goes through `platforms.py`, `autorun.py` or `power.py`.
- A problem report sends nothing, and holds only the version, check names and results, and reply
  structure with every value removed. Never add watches, addresses or error text to it.
- Sleep settings only change after a yes, only the plugged-in ones, and are restored on undo or
  uninstall.
- Someone added to alerts gets one welcome note ever, through the outbox, naming nobody else.
- Personal details (addresses, watches, passwords) stay in the state folder, never in this repo.
- The Gmail app password stays in Windows Credential Manager, never in files or logs.
