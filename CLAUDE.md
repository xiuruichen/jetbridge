# jetbridge

Watches award availability for one specific flight, cabin and program on chosen dates, and
emails when seats open. The deterministic engine does all checking and alerting. Claude is
an optional layer through the `jetbridge` MCP server and is never needed for a check to run.

## Dev
- `uv run pytest` runs offline on recorded fixtures.
- `uv run jetbridge check ID` makes live requests. Keep live testing low-volume.
- State lives in `%LOCALAPPDATA%\jetbridge\` (database, Chrome profile, saved responses,
  venv), never in the project folder, which is synced by OneDrive. `UV_PROJECT_ENVIRONMENT`
  points uv at that venv.

## Layout
`models.py` types · `matcher.py` response to result (pure) · `alerts.py` alert rules and text
(pure) · `scheduler.py` due, jitter, grouping (pure) · `store.py` SQLite · `runner.py` one tick ·
`acquire/` `american.py` (fetch and pure parse), `detect.py`, `governor.py`, `broker.py`
(Chrome), `fetcher.py` · `notify/email.py` Gmail and outbox · `winsched.py` scheduled task ·
`service.py` shared wiring · `cli.py`, `mcp_server.py`.

## Invariants (do not break)
- Only a positively recognized answer can mean "no seats". Blocks, errors, site error codes,
  unexpected pages and unreadable results that might be the flight are all `UNKNOWN`.
- `UNKNOWN` never sends a seats alert and never changes a date's open or closed status.
- `AVAILABLE` requires the exact flight between the watched airports on that date, with every
  leg in the requested cabin.
- Searches use the watch's real party size, because aa.com prices change with it.
- Every live request goes through `Governor.acquire`, including the browser's first page load
  and the navigation fallback. A block is never retried: it opens a persisted cooldown. Don't
  add retries or ways around this. Challenges are never solved automatically.
- Alerts come from recorded state changes. `Store.commit_check` writes the observation, state
  and alerts in one transaction, and email goes only through the outbox.
- An opening is re-checked live before its alert goes out.
- The Gmail app password stays in Windows Credential Manager, never in files or logs.
