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

**Daily start, one command:** `.\scripts\start-interlock.ps1`. It opens the WhatsApp agent, the API and the worker, each in its own window, then opens the browser. Use `-NoAgent` if the agent is already running. There is no `npm start` in `apps/web`, because the API serves the UI.

Or by hand:

1. Start the API: `.\.venv\Scripts\python.exe -m uvicorn interlock.api.main:app`
2. Open http://127.0.0.1:8000/ (it redirects to `/app/`; if the UI isn't built
   yet it redirects to `/docs` instead).
3. **Groups** → type the WhatsApp group name → **Find** → pick the exact match
   → **Pin**. This needs the WhatsApp agent running and paired. Tick "Preselect
   for …" to have the group selected by default on those reports.
4. **Reports** lists today's reports. The worker opens one at 09:00 and 17:00
   on working days; **Start a report now** opens a manual one at any time.
5. On a report: check the numbers and the message, tick the groups, choose
   when, then press **Share to N groups**. **Don't share this one** closes it
   without sending.
6. The same page then shows delivery per group. It refreshes by itself while
   sending, and has **Retry** on any group that failed.

**Desktop alerts:** the dashboard can raise a desktop notification when the
09:00 or 17:00 report opens, but only while its tab is open. There is no push
delivery yet (HANDOVER §8).

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
