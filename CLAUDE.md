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
`headlines.py` custom alert wording · `service.py` shared wiring · `cli.py`, `mcp_server.py`.

## Invariants (do not break)
- Only a positively recognized answer can mean "no seats". Blocks, errors, site error codes,
  unexpected pages and unreadable results that might be the flight are all `UNKNOWN`.
- `UNKNOWN` never sends a seats alert and never changes a date's open or closed status.
- `AVAILABLE` requires the exact flight between the watched airports on that date, with every
  leg in the requested cabin.
- Searches use the watch's real party size, because award prices can change with it.
- Every live request goes through `Governor.acquire`, including the browser's first page load,
  United's guest token, each Delta result page and the navigation fallback. A site's `fetch` calls
  its `gate` before every request. A block is never retried: it opens a persisted cooldown for
  that site. Don't add retries or ways around this. Challenges are never solved automatically.
- Sites are independent: a block, rest or format warning on one never stops or clears another.
- A seat's cabin comes from what's flown on each leg, not the product name. An unknown Delta
  brand, a cut-off United stream or a missing Delta page is `UNKNOWN`, never "no seats".
- Alerts come from recorded state changes. `Store.commit_check` writes the observation, state
  and alerts in one transaction, and email goes only through the outbox.
- An opening is re-checked live before its alert goes out.
- Seat alerts go only to the watch's recipients (the owner if none), one email per person so
  addresses are never shared. Health warnings and digests go only to the owner.
- Custom headlines change wording only, and a bad headlines file falls back to the built-in text.
- Every way of adding a watch shows the draft review and saves only after a yes. Never guess a
  flight number or an address. The flight picker's search goes through `Governor.acquire` and isn't
  retried, and a flight missing from its list doesn't mean the flight doesn't exist.
- Personal details (addresses, watches, passwords) stay in the state folder, never in this repo.
- The Gmail app password stays in Windows Credential Manager, never in files or logs.
