# Review / approve UI

The browser UI for daily use: see today's reports, read the exact WhatsApp
text, pick groups, and approve. It lives in `apps/web` (Vite + React 19 +
TypeScript + TanStack Query) and is served by the API itself at `/app/`, so
nothing extra has to run.

## First-time build (and after every UI change)

```powershell
cd apps\web
npm install
npm run build       # type-checks, then writes apps\web\dist
```

`dist/` is gitignored, so a fresh checkout needs this once. The API only looks
for `dist\index.html` when it starts, so restart uvicorn after the first build.

## Using it

**Daily start, one command:** `.\scripts\start-interlock.ps1`. It starts the WhatsApp agent, the API and the worker **in the background** (no windows) under one supervisor that restarts anything that stops, then opens the browser. Running it again when Interlock is already up just opens the browser. Other options: `-Status` (what's running, is WhatsApp connected), `-Stop`, `-Restart` (do this after changing backend code), `-Console` (run in the window with live logs, for debugging) and `-NoAgent`. Logs are in `logs\` (`api.log`, `worker.log`, `agent.log`, `supervisor.log`).

**Start at login:** `.\scripts\register-autostart.ps1` registers a Scheduled Task for your account (no administrator needed; `-Remove` undoes it). There is no `npm start` in `apps/web`, because the API serves the UI.

Or by hand:

1. Start the API: `.\.venv\Scripts\python.exe -m uvicorn interlock.api.main:app`
2. Open http://127.0.0.1:8000/ (it redirects to `/app/`; if the UI isn't built
   yet it redirects to `/docs` instead).
3. **Groups** → type the WhatsApp group name → **Find** → pick the exact match
   → **Pin**. This needs the WhatsApp agent running and paired. Tick "Preselect
   for …" to have the group selected by default on those reports.
4. **Reports** is the home page (see below). The worker opens a report at 09:00
   and 17:00 on working days; **Start a report now** opens a manual one at any time.
5. On a report: check the numbers and the message (click the table to see it
   large), tick the groups, choose when, then press **Share to N groups**.
   **Don't share this one** closes it without sending, after asking you to confirm.
6. The same page then shows delivery per group. It refreshes by itself while
   sending, and has **Retry** on any group that failed.

**Desktop alerts:** the Reports page can raise a desktop notification when the
09:00 or 17:00 report opens, but only while its tab is open. There is no push
delivery yet (HANDOVER §8).

## The pages

- **Reports.** A strip at the top shows the WhatsApp state, the next report time ("Evening report at 17:00, in 2 h") and **Start a report now**. Reports waiting for you appear as **Needs your approval** cards with **Review** and **Close**. Below, **All reports** is a table (Report, Time, Result, Groups) for **Today / 7 days / 30 days**, filtered by **All, Needs approval, Sent, Problems, Not shared**. The Result column says what actually happened, in words: *Sent to 3 groups*, *Partly sent 2/3*, *Failed*, *Held back*, *Scheduled Fri 2 Oct 09:00*, *Waiting for WhatsApp*, *Not shared*. On a phone each row becomes a card.
- **A report.** A status banner at the top, the message on the left (what will be sent, or what was sent), and on the right the decision: counts, who receives it, when, **Share**. After approval the right side shows delivery per group.
- **Groups.** Pin and enable the WhatsApp groups reports go to.
- **Settings.** WhatsApp (connected number, reconnect or link a different phone), the Google Sheet (a read-only link) and the schedule (times and days, read-only: they are set in `.env`).

## The 09:00 / 17:00 popup

When the worker opens a morning or evening report, a popup asks what to do with it:

- **Send now** sends it right away.
- **Send after 5 minutes** and **Custom time…** (any date and time within 7 days) schedule it. The worker sends it at that time, even if you close the browser.
- **Snooze 30 minutes** (or the ×) hides the popup and brings it back 30 minutes later.
- **Skip this report** closes the report without sharing it; **Open full report** goes to the full page.

The popup appears within about a minute of the alert time, on whichever Interlock tab is open. It never shows on that report's own page, and if the tab is in the background you also get a desktop notification, once you've turned desktop alerts on from the Reports page.

It's driven by the report waiting in the database, not by a timer in the page, so a restart or refresh can't lose it or show it twice, and a snooze survives a restart.

If WhatsApp is disconnected when a report comes due, the report waits (no failed attempts) and goes out when WhatsApp reconnects, as long as it's still the same day and within the grace window. Otherwise it is held back for you, with the reason shown.

A custom time on a later day sends the report as it was when you approved it, so it carries that day's data, not the later day's.

## What protects you

- **The text you read is the text that's sent.** The preview carries a
  fingerprint of the task data. If the sheet import changes a task between
  your reading and pressing Share, the API refuses with
  `DATASET_VERSION_CONFLICT`. The page then shows the updated text and asks you
  to read it again.
- **A double click sends once.** Every Share uses the same `action_version`
  for a review, plus a fresh `Idempotency-Key`.
- Tasks are read-only here because your Google Sheet owns them. The dashboard
  links to the sheet.

## Developing

```powershell
cd apps\web
npm run dev         # hot reload on http://localhost:5173/app/, proxies the API on :8000
npm test            # tsc + vitest (pure logic: timezones, send-at, defaults, formatting)
npm run gen:api     # regenerate src/api-types.ts after any API schema change
```

After a backend schema change, run `scripts/export_openapi.py`, then
`npm run gen:api`. A mismatch then shows up as a compile error.

**Pinned for Node 22.11:** Vite 6, plugin-react 4, and TypeScript 5.9. Vite 8
needs Node ≥ 22.12, and openapi-typescript needs TypeScript 5. Upgrading Node
would also affect the WhatsApp agent, so check that first.
