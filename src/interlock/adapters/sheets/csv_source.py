"""Reading a Google Sheet through its public CSV export -- no Google Cloud
project, no credentials, read-only by construction.

Works only while the sheet is shared as "anyone with the link can view".
The dangerous failure mode is a sheet that stops being public: Google then
answers **200 with an HTML sign-in page**, not an error. Parsed naively that
is a sheet with zero rows, so it is rejected here explicitly -- a fetch that
cannot prove it read the real sheet must never look like an empty one.
"""

from __future__ import annotations

import re
import urllib.request

_SPREADSHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID = re.compile(r"[#?&]gid=(\d+)")


class SheetNotReadableError(RuntimeError):
    pass


def export_url_from_sheet_link(link: str) -> str:
    """The normal link you copy from the browser (``.../d/<id>/edit#gid=0``)
    to its CSV export URL. The tab comes from ``gid``; no ``gid`` means the
    first tab."""
    match = _SPREADSHEET_ID.search(link)
    if match is None:
        raise ValueError(
            "SHEET_IMPORT_URL does not look like a Google Sheets link "
            "(expected .../spreadsheets/d/<id>/...)."
        )
    gid = _GID.search(link)
    return (
        f"https://docs.google.com/spreadsheets/d/{match.group(1)}"
        f"/export?format=csv&gid={gid.group(1) if gid else '0'}"
    )


def fetch_csv(url: str, *, timeout: float = 15.0) -> str:
    """GET the export, following Google's redirect to googleusercontent.com
    (urllib follows 307 for GET). Raises rather than returning anything that
    is not demonstrably CSV."""
    request = urllib.request.Request(url, headers={"User-Agent": "interlock-sheet-import"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type", ""))
        body = bytes(response.read()).decode("utf-8-sig")

    if "text/csv" not in content_type or body.lstrip().startswith("<"):
        raise SheetNotReadableError(
            "The sheet did not come back as CSV (got "
            f"{content_type or 'no content type'}). It is probably no longer shared as "
            "'anyone with the link can view'. Nothing was imported."
        )
    return body
