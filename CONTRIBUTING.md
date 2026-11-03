# Contributing

Bug reports and pull requests are welcome. jetbridge runs on Windows, with an experimental Mac version, using Python 3.12 and
[uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/xiuruichen/jetbridge.git
cd jetbridge
uv sync
uv run pytest
uv run ruff check .
uv run ruff format .
```

The tests run offline on recorded airline responses, and they need to pass before a change is merged.

## Before you change anything

Read the rules in [`CLAUDE.md`](CLAUDE.md). The most important ones are:

- Only a recognized answer from an airline can mean "no seats". Anything else is `unknown`.
- Every live request goes through the rate governor, and a block is never retried or worked around.
- Nothing is saved until the person has seen a review and said yes.
- Personal details and passwords never go in this repository, a log, or a chat.

## Testing against the airlines

Keep live testing rare and low-volume, because too many searches can get a site to block you. Don't
add retries or ways around a block or a human check.

## Sharing logs and responses

Saved responses and logs can contain email addresses, flights and dates. Remove anything personal
before you post them, and never post your Gmail app password.
