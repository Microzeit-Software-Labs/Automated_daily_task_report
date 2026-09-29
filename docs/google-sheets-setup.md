# Google Sheets sync — setup

Interlock keeps a Google Sheet in step with the `tasks` table. PostgreSQL is always
the source of truth; the sheet is a peer that people can edit directly. A background
tick compares both sides and either copies changes across or, when both sides changed
since they last agreed, parks a **conflict** for a human to resolve. It never guesses.

Out of the box `SHEETS_PROVIDER=mock` runs an in-memory fake, so the sync tick is a
harmless no-op until you follow this guide.

## 1. Create a service account

1. In [Google Cloud Console](https://console.cloud.google.com/), create (or pick) a
   project.
2. **APIs & Services → Library → Google Sheets API → Enable.** No other API is needed;
   the adapter requests only the `spreadsheets` scope, not Drive.
3. **IAM & Admin → Service Accounts → Create service account.** No project roles are
   required.
4. Open the new account → **Keys → Add key → Create new key → JSON.** Download the file.
5. Note the account's email, `client_email` in the JSON
   (`something@your-project.iam.gserviceaccount.com`).

Store the key file **outside the repository** and never commit it.

## 2. Prepare the spreadsheet

1. Create a spreadsheet and **share it with the service account's email as Editor.**
   This share — not OAuth — is what grants access.
2. Name the tab `Tasks` (or choose another name and set `GOOGLE_SHEETS_SHEET_NAME`).
3. Start with an **empty tab containing only the header row** below, in row 1.

| A | B | C | D | E | F | G | H | I | J | K |
|---|---|---|---|---|---|---|---|---|---|---|
| `_sys` | `display_id` | `title` | `description` | `project_id` | `owner_id` | `priority` | `status` | `due_date` | `remarks` | `tags` |

**Keep these columns in exactly this order, A through K.** Reads look each column up by
its header name, but writes are positional (a row is written left to right starting at
column A), so reordering, inserting or deleting a column among A–K makes values land in
the wrong cells. Columns to the **right of K** are safe: they are ignored on read and
never written, so you can keep your own notes there. The sheet is read from columns `A:Z`.

Recommended:

- **Format the `due_date` column as Plain text** (Format → Number → Plain text). Otherwise
  Sheets reinterprets `2026-09-20` as a date and shows it in your locale's format, which
  Interlock cannot parse.
- **Hide or protect the `_sys` column.** It holds `task-id|version|hash`, which is how a
  row is matched to its task.
- Treat `display_id` (`TSK-00042`) as read-only. It is written for your reference and
  never read back.

### Cell values

| Column | Accepted values |
|---|---|
| `priority` | `LOW`, `MEDIUM`, `HIGH`, `URGENT` |
| `status` | `PENDING`, `IN_PROGRESS`, `COMPLETED`, `BLOCKED`, `DEFERRED` |
| `due_date` | `YYYY-MM-DD`, or blank to clear |
| `tags` | comma-separated, e.g. `urgent, infra` |
| `project_id`, `owner_id` | free text, or blank to clear |

A cell that does not parse (`priority = BANANA`, `due_date = 20/9/2026`) is **ignored for
that tick** and the row is re-stamped with the database's value, so it heals itself. A
blank `title` is ignored the same way — a task cannot have an empty title.

## 3. Configure Interlock

In `.env`:

```dotenv
SHEETS_PROVIDER=google
GOOGLE_SERVICE_ACCOUNT_PATH=C:\secrets\interlock-sheets.json
GOOGLE_SHEETS_SPREADSHEET_ID=<the long id between /d/ and /edit in the sheet's URL>
GOOGLE_SHEETS_SHEET_NAME=Tasks
SHEET_SYNC_INTERVAL_SECONDS=60
```

Both the **worker** and the **API** build the provider from these settings, so both
processes need them. If `SHEETS_PROVIDER=google` is set without the path and id, startup
fails immediately with a message pointing here.

Each tick makes one read and at most two writes (one batch update, one append) — never a
call per row — so the default 60-second interval is far below Sheets API quotas.

## 4. Run and verify

```powershell
.\.venv\Scripts\python.exe -m interlock.workers.loop
```

Expect `worker.started` with `sheets_provider="google"`, then `worker.sheet_sync_tick`
whenever something moved (`pushed` / `pulled` / `created` / `conflicts` / `vanished`).
Create a task through the API; it should appear in the sheet within one interval. Type a
new row by hand with `_sys` blank; it becomes a task on the next tick and gets its `_sys`
and `display_id` filled in.

## What happens on each tick

| Situation | Result |
|---|---|
| Task with no row yet | Row appended, stamped |
| Only the task changed | Row overwritten in place |
| Only the row changed | Change applied to the task, audited as a `SYNC` actor |
| **Both** changed | Nothing touched; a **conflict** is parked |
| Row typed by hand (blank `_sys`) | New task created |
| Row deleted from the sheet | A conflict is parked; the task is **never** deleted |

A row's identity is its `_sys` stamp, not its position, so sorting or inserting rows is
safe. The stamp doubles as a fallback baseline, so restoring the database (or switching
from the mock provider to a sheet that already holds synced rows) reconciles normally
instead of flagging every row.

## Resolving conflicts

```
GET  /sync/conflicts                       # open ones; add ?open_only=false for history
POST /sync/conflicts/{id}/resolve          # {"resolution": "kept_db" | "kept_sheet"}
```

Each conflict carries both versions (`db_value`, `external_value`). `external_value` is
`null` when the row vanished, in which case only `kept_db` is possible — it writes the
task back to the sheet. Choosing a side applies it to both places and clears the
conflict; a task has at most one open conflict at a time.

## Things to know

- **Do not clear the `_sys` cell on an existing row.** A blank `_sys` means "a human typed
  this row", so the next tick creates a *new, duplicate* task from it.
- **A soft-deleted task keeps its sheet row.** Nothing infers deletion in either
  direction; the row is simply left behind. (The app has no delete endpoint yet.)
- Point it at an **empty tab** the first time. Any pre-existing row without a `_sys`
  stamp is imported as a new task.
