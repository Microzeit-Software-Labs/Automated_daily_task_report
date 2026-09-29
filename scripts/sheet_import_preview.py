"""Preview what the read-only sheet import would do -- without writing
anything, anywhere.

Fetches the sheet (SHEET_IMPORT_URL from .env, or a link passed as the first
argument), parses it exactly as the worker would, and prints what it found:
row count, statuses, rows it would hide (delegated) or skip, and any dates
it could not read. Run this before turning the import on, and whenever the
sheet's layout changes.

Usage:
    python scripts/sheet_import_preview.py
    python scripts/sheet_import_preview.py "https://docs.google.com/spreadsheets/d/<id>/edit#gid=0"
"""

from __future__ import annotations

import datetime as dt
import sys
from collections import Counter

from dotenv import load_dotenv

load_dotenv()

from interlock.adapters.sheets.csv_source import (  # noqa: E402
    export_url_from_sheet_link,
    fetch_csv,
)
from interlock.config import get_settings  # noqa: E402
from interlock.domain.common.clock import local_date  # noqa: E402
from interlock.domain.sync.sheet_import import parse_sheet  # noqa: E402


def main() -> None:
    settings = get_settings()
    link = sys.argv[1] if len(sys.argv) > 1 else settings.sheet_import_url
    if not link:
        sys.exit("No sheet link: set SHEET_IMPORT_URL in .env or pass the link as an argument.")

    url = export_url_from_sheet_link(link)
    print(f"fetching {url}")
    parsed = parse_sheet(fetch_csv(url), today=local_date(dt.datetime.now(dt.UTC), settings.tz))

    visible = [row for row in parsed.rows if row.delegated_to is None]
    delegated = [row for row in parsed.rows if row.delegated_to is not None]
    print(f"\n{len(parsed.rows)} rows would be mirrored ({len(visible)} shown in reports, "
          f"{len(delegated)} hidden as delegated), {len(parsed.skipped)} skipped.\n")

    statuses = Counter(row.status.value if row.status else "UNRECOGNISED" for row in visible)
    print("Status of rows shown in reports:")
    for status, count in statuses.most_common():
        print(f"  {status:<14} {count}")

    if delegated:
        by_person = Counter(row.delegated_to for row in delegated)
        print("\nHidden as delegated: " + ", ".join(f"{p} ({n})" for p, n in by_person.items()))

    unrecognised = [row for row in parsed.rows if row.status is None]
    if unrecognised:
        print("\nStatus not understood (kept as Pending for new rows):")
        for row in unrecognised:
            print(f"  row {row.line}  Sr No. {row.sr_no}: {row.raw_status!r}")

    if parsed.skipped:
        print("\nSkipped:")
        for skip in parsed.skipped:
            print(f"  row {skip.line}  {skip.reason.value}: {skip.detail}")

    open_rows = [r for r in visible if r.status is not None and r.status.value != "COMPLETED"]
    if open_rows:
        print("\nOpen tasks (what reports will list):")
        for row in open_rows:
            due = f"  due {row.due_date}" if row.due_date else ""
            print(f"  Sr No. {row.sr_no:<4} {row.status.value if row.status else '?':<12} "
                  f"{row.title[:60]}{due}")

    no_date = [row for row in parsed.rows if row.logged_on is None]
    if no_date:
        print(f"\n{len(no_date)} row(s) with no readable Date (will use import time): "
              + ", ".join(f"Sr No. {row.sr_no}" for row in no_date))

    print("\nNothing was written.")


if __name__ == "__main__":
    main()
