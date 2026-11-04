# Changelog

## Unreleased

- Split a party across any nonstops, alerting only when enough are open to seat everyone.
- Watch a flight you've already booked, and get alerted only when it costs less than you paid.
- Say in every seat alert what to do if plans change, stating a program's rule only once it's been
  confirmed on the airline's own page.
- Lead the front page with the live re-check, and add a dated comparison with seats.aero.
- Send the daily summary to everyone on a watch, not only the owner. Each person's copy covers only
  their own watches and doesn't show anyone else's address.

## 0.5.1

- A clearer front page, with the full reference moved to [the guide](docs/guide.md).
- Name the miles program properly in alerts and `watch list`.
- A tidier board in chat, with a shorter next check time and an arrow in the route.
- Fix the setup window crashing on a Mac, and minimize Chrome there, because macOS moves off-screen
  windows back onto the screen.

## 0.5.0

- Move searches to where the signals point, within the same limits. Seats vanishing, a price change,
  another flight opening on the route or a nearby date opening brings a date's next checks forward.
  A date also gets one check just after its first bookable midnight.
- More checks overall: far-out dates now get 5 of every 6 checks, rather than half. A simulator of
  made-up patterns set this, and it's labeled as such. Its results don't predict real catch rates.
- Each site has a planned budget under the hard limit, the most deserving dates go first, and a block
  lowers it for a while.
- Say that a watch matters most, so it goes first and gets checked more often.
- Watch any nonstop on a route, or several flights and cabins from one review. These add no searches.
- An email the day before a date should become bookable, using no searches.
- `jetbridge report` and "report this problem" prepare an issue to read and post, with nothing
  personal in it.
- An experimental Mac version, which hasn't been tried on a real Mac yet.

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
