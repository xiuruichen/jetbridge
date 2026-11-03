# Changelog

## 0.4.0

- Check a new watch right away, and say plainly if the site can't see that flight.
- Show proof of watching: how many of the last 24 hours each flight stayed on schedule.
- Keep a record of each flight's past openings, how long they lasted and their prices, with a short
  line about it in seat alerts.
- "Show me my flights" draws a board of every watch inside the Claude Desktop chat.
- Show seats above your price limit, and other flights seen with seats on the same route, without
  any extra searches or alerts.
- Check more often in the last two weeks and just after a program can show a date, and less often
  far out. Dates a program can't show yet aren't searched.
- No site gets more than 150 requests in any 24 hours.
- Change a watch's dates, price limit or pace without losing its history.
- Send one welcome note to people you add to alerts.
- Optional phone alerts through ntfy.
- Offer to keep the PC awake while plugged in, and put the old settings back on undo or uninstall.
- `jetbridge update`, and a note in the daily summary when there's a new version. Claude Desktop no
  longer blocks updates after you reconnect it.
- Put the booking link at the top of seat alerts.
- Fix a false "no clear answer" warning on watches checked once or twice a day.

## 0.3.0

- Run everything by chatting in the Claude Desktop app: setup, finding flights, adding, pausing and
  removing watches, alert recipients, settings and troubleshooting.
- `jetbridge claude connect` connects Claude Desktop, and the installer offers to do it.
- The installer adds a `jetbridge` command that works from any PowerShell window.
- Add `jetbridge uninstall` and `jetbridge --version`.
- `watch add` now shows what it will save and waits for a yes. Scripts can use `--yes`.
- Fix Delta alert links that landed on an error page.

## 0.2.0

- Search Delta SkyMiles and United MileagePlus as well as American AAdvantage.
- Read partner fares on all three programs.
