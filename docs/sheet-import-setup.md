# Read-only sheet import — setup

Interlock can build its reports from a task sheet you already keep by hand, without
changing how you work. Every minute the worker reads the sheet through its public link
and mirrors it into Interlock's task list. It **never writes to the sheet**, and needs no
Google Cloud project or credentials.

This is the alternative to the two-way sync in `docs/google-sheets-setup.md`. The two
cannot be on at the same time.

## 1. Share the sheet

**Share → General access → Anyone with the link → Viewer.** That's all the access
Interlock needs. Be aware that anyone holding the link can then read the sheet. If you
ever turn link sharing off, the import stops safely: it refuses to treat the sign-in page
Google returns as an empty sheet, and it changes nothing.

## 2. Connect it from the browser

Open **Settings → Google Sheet → Connect a sheet** (or **Change sheet** once one is
connected), paste the link exactly as your browser shows it (the tab comes from `#gid=`;
no `#gid=` means the first tab), and press **Check**. Checking reads the sheet and shows
what it found, and **saves nothing**:

- how many tasks it holds and the most recent few, so you know it's the right sheet;
- if it can't be used, why and what to do: *not shared* (it says how to share it), *no such
  sheet*, *can't reach Google*, *missing the columns Interlock needs* (it names them), or *no
  tasks yet*.

Then **Use this sheet**. The Interlock service reads it within about half a minute, and the
dialog waits and tells you how many tasks were imported. Until the new sheet has been read
successfully, the one you had keeps working: a failed attempt changes nothing.

**Switching to a different sheet** hides the old sheet's tasks from reports (the dialog says
how many first). They aren't deleted: tasks belong to the sheet they came from, so the
same `Sr No.` in two sheets is two different tasks, and switching back restores the first
sheet's tasks exactly as they were.

**If the sheet stops being readable** (say link sharing is turned off), a banner appears on
every page with a **Fix the sheet link** button, and Settings shows why and when it was last
read. Reports keep using the tasks from the last good read.

The link is stored in Interlock's database. `SHEET_IMPORT_URL` in `.env` is only used once, to
fill it in the first time Interlock runs; after that the saved link wins, so changing it in
the browser can't be undone by an old `.env`. (If the two-way sync, `SHEETS_PROVIDER=google`,
is turned on, it owns the sheet and the link can't be changed here.)

## 3. Optional: preview from the command line (writes nothing)

```powershell
.\.venv\Scripts\python.exe scripts\sheet_import_preview.py "<your sheet link>"
```

This prints how many rows it will mirror, their statuses, which rows are hidden as
delegated, which rows it will skip and why, and the open tasks your report will list.

## Your columns

Columns are matched by header text; case, spacing and dots don't matter.

| Column | Required | Becomes |
|---|---|---|
| `Sr No.` | yes | The row's permanent identity. Don't renumber existing rows. |
| `Task` | yes | Title |
| `Status` | yes | See below |
| `Date` | no | When the task was logged. Used so old rows don't count as "added today". |
| `Note` | no | Remarks |
| `Deadline` | no | Due date. Drives "overdue". |

| Status text | Result |
|---|---|
| `Closed`, `Done`, `Completed` | Completed |
| `Inprogress`, `In progress` | In Progress |
| blank, `Pending` | Pending |
| `<Name>'s Task`, `<Name> Task` | **Hidden from reports** (delegated). Reappears if the status changes back. |
| anything else | New row: imported as Pending. Existing task: keeps its status. Logged either way. |

Dates are read month-first (`09-28-2026`). If the first number can't be a month, they
are read day-first (`29-04-2026`). Two-digit years mean 20xx, and a missing year means
this year. A date that can't be read is left empty, never guessed.

## Rules worth knowing

- **Edit tasks in the sheet, not in Interlock.** Imported tasks are read-only in
  Interlock; editing one returns an error pointing back to the sheet. Tasks you create
  in Interlock itself stay editable.
- **A row without a `Sr No.` is skipped** and reported in the worker log. Give it a
  number and it's picked up on the next tick. **Duplicate `Sr No.` values skip every
  copy** rather than guess which one is real.
- **Deleting a row does not delete the task.** A vanished row is left alone, never
  treated as a deletion.
- **Reports list open tasks plus anything completed today.** Tasks closed on earlier
  days are counted but never listed.
