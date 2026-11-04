"""The page Claude Desktop can show inside a chat: every watch at a glance.

Read-only and self-contained, with no outside scripts. Everything comes from the tool's result and
goes on the page as plain text, so nothing a site returns can run as code. Anywhere that can't show
this page still gets the same facts as an ordinary tool result.
"""

from __future__ import annotations

URI = "ui://jetbridge/board"
MIME_TYPE = "text/html;profile=mcp-app"

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your flights</title>
<style>
  :root {
    --bg: transparent;
    --card: var(--color-background-secondary, rgba(127, 127, 127, .08));
    --ink: var(--color-text-primary, CanvasText);
    --soft: var(--color-text-secondary, GrayText);
    --line: var(--color-border-primary, rgba(127, 127, 127, .28));
    --open: #1a8f4e;
    --near: #b7791f;
    --unsure: #8a8a8a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2px; background: var(--bg); color: var(--ink);
    font: 14px/1.45 var(--font-sans, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif);
  }
  .watch { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 14px 16px; margin-bottom: 10px; }
  .top { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  h2 { margin: 0; font-size: 16px; font-weight: 600; }
  .sub, .proof, .note { color: var(--soft); font-size: 12.5px; }
  .proof { margin-top: 2px; }
  .state { font-size: 12.5px; color: var(--soft); white-space: nowrap; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; }
  td { padding: 7px 0; border-top: 1px solid var(--line); vertical-align: top; }
  td.day { width: 110px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 7px; background: var(--unsure); }
  .open .dot { background: var(--open); }
  .open b { color: var(--open); }
  .near { color: var(--near); }
  .closed .dot { background: transparent; border: 1.5px solid var(--soft); }
  .note { margin-top: 3px; }
  button { font: inherit; font-size: 12.5px; margin-top: 5px; padding: 3px 10px; cursor: pointer;
    color: var(--ink); background: transparent; border: 1px solid var(--line); border-radius: 7px; }
  button:hover { border-color: var(--ink); }
  .empty { color: var(--soft); padding: 12px 2px; }
</style>
</head>
<body>
<div id="board" aria-live="polite"><div class="empty">Loading your flights…</div></div>
<script>
  let nextId = 1;
  const send = (method, params) => {
    const id = nextId++;
    window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    return id;
  };
  const notify = (method, params) =>
    window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;  // always text, never markup
    return node;
  };
  const miles = (n) => Number(n).toLocaleString("en-US") + " miles";
  const shortDay = (iso) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString("en-US",
      { weekday: "short", month: "short", day: "numeric" });
  };

  function dayRow(day) {
    const row = el("tr");
    row.append(el("td", "day", shortDay(day.date)));
    const cell = el("td");
    const line = el("div");
    const open = day.status === "available";
    const known = ["available", "not_offered", "not_listed"].includes(day.status);
    line.className = open ? "open" : known ? "closed" : "";
    line.append(el("span", "dot"));
    if (open) {
      const where = day.open_flights ? " on " + day.open_flights.join(", ") : "";
      line.append(el("b", "", "Seats open" + where + (day.miles ? ": " + miles(day.miles) : "")));
    }
    else if (day.split_so_far) {
      line.append(day.split_so_far[0].toUpperCase() + day.split_so_far.slice(1));
    }
    else if (day.status === "not_offered") line.append("No seats");
    else if (day.status === "not_listed") line.append("Flight not listed");
    else if (day.searching_starts) line.append("Searching starts " + day.searching_starts);
    else if (day.status === "unknown") line.append("Couldn't tell at the last check");
    else line.append("No clear answer yet");
    if (day.seats_above_price_limit_at_miles) {
      line.append(" · ");
      line.append(el("span", "near",
        "seats at " + miles(day.seats_above_price_limit_at_miles) + ", above your limit"));
    }
    cell.append(line);
    for (const past of day.openings || []) {
      const price = past.lowest_miles ? ", " + miles(past.lowest_miles) : "";
      cell.append(el("div", "note", "Seen " + past.first_seen + ": " + past.how_long + price));
    }
    if (day.also_seen) {
      const names = day.also_seen.flights.map((f) =>
        f.miles ? f.flight + " (" + miles(f.miles) + ")" : f.flight).join(", ");
      cell.append(el("div", "note", "Also seen, not watched: " + names));
    }
    const link = open ? day.book_at : null;
    if (link) {
      const button = el("button", "", "Check and book");
      button.addEventListener("click", () => send("ui/open-link", { url: link }));
      cell.append(button);
    }
    row.append(cell);
    return row;
  }

  function render(watches) {
    const board = document.getElementById("board");
    board.replaceChildren();
    if (!watches.length) {
      board.append(el("div", "empty", "You're not watching any flights yet."));
    }
    for (const watch of watches) {
      const card = el("section", "watch");
      const top = el("div", "top");
      const title = el("div");
      const name = watch.flight === "any nonstop" ? "Any nonstop" : watch.flight;
      title.append(el("h2", "", name + " · " + watch.route.replace("-", " → ")));
      const party = watch.whole_party
        ? watch.whole_party + " people, on separate bookings of " + watch.pax
        : watch.pax + (watch.pax === 1 ? " passenger" : " passengers");
      const paid = watch.booked_at_miles ? " · you paid " + miles(watch.booked_at_miles) : "";
      title.append(el("div", "sub", watch.cabin + " · " + watch.miles_program + " · " +
        party + paid));
      top.append(title);
      top.append(el("div", "state", watch.status === "active"
        ? (watch.next_check_short ? "Next check " + watch.next_check_short : "") : watch.status));
      card.append(top);
      if (watch.proof_of_watching) {
        const proof = watch.proof_of_watching;
        card.append(el("div", "proof", proof[0].toUpperCase() + proof.slice(1)));
      }
      const table = el("table");
      for (const day of watch.dates) {
        if (!day.date_has_passed) table.append(dayRow(day));
      }
      card.append(table);
      board.append(card);
    }
    reportSize();
  }

  function reportSize() {
    const box = document.documentElement.getBoundingClientRect();
    notify("ui/notifications/size-changed", { width: box.width, height: box.height });
  }

  function applyHost(context) {
    // The page only matches the chat behind it once it knows which theme that chat is using.
    if (context && context.theme) document.documentElement.style.colorScheme = context.theme;
    const variables = context && context.styles && context.styles.variables;
    for (const [name, value] of Object.entries(variables || {})) {
      if (value) document.documentElement.style.setProperty(name, value);
    }
  }

  const initId = send("ui/initialize", {
    protocolVersion: "2026-01-26",
    appInfo: { name: "jetbridge board", version: "1" },
    appCapabilities: {},
  });

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data || {};
    if (message.id === initId && message.result) {
      applyHost(message.result.hostContext);
      notify("ui/notifications/initialized", {});
    } else if (message.method === "ui/notifications/host-context-changed") {
      applyHost(message.params);
    } else if (message.method === "ui/notifications/tool-result") {
      const data = message.params && message.params.structuredContent;
      if (data && Array.isArray(data.watches)) render(data.watches);
    }
  });
  new ResizeObserver(reportSize).observe(document.body);
</script>
</body>
</html>
"""
