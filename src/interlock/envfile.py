"""Changing a few values in ``.env`` without disturbing the rest of it.

The installer needs to turn WhatsApp on (``WHATSAPP_PROVIDER`` and the
terms-of-service acknowledgement) in a file the user may have edited by hand.
So this only ever touches the keys it is asked to set: every other line,
comment and blank, keeps its place, and the file's own conventions (a byte-order
mark, CRLF line endings, which Windows PowerShell writes) are preserved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def set_values(text: str, updates: Mapping[str, str]) -> str:
    """``text`` with each ``KEY=value`` in ``updates`` set.

    An existing active line is replaced in place. Failing that, a commented-out
    example for the key (``# KEY=...``) is replaced by the active line, so it
    stays where the file's own documentation puts it. Failing that, the line is
    appended. Values are written as given: no quoting, which is what the
    existing file does.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    trailing_newline = bool(lines) and lines[-1] == ""
    if trailing_newline:
        lines.pop()

    for key, value in updates.items():
        rendered = f"{key}={value}"
        active = next((i for i, line in enumerate(lines) if _match(_KEY, line) == key), None)
        if active is not None:
            lines[active] = rendered
            continue
        commented = next(
            (i for i, line in enumerate(lines) if _match(_COMMENTED_KEY, line) == key), None
        )
        if commented is not None:
            lines[commented] = rendered
        else:
            lines.append(rendered)

    return newline.join(lines) + (newline if trailing_newline or not text else "")


def read_value(text: str, key: str) -> str | None:
    """The active value of ``key`` (the last one wins, as in dotenv), or None."""
    found: str | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if _match(_KEY, line) == key:
            found = line.split("=", 1)[1].strip()
    return found


def update_file(path: Path, updates: Mapping[str, str]) -> None:
    """Apply ``set_values`` to the file at ``path``, creating it if need be.
    Written atomically (a temp file, then a rename) so a crash can't leave a
    half-written ``.env``."""
    raw = path.read_bytes() if path.exists() else b""
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    updated = set_values(text, updates)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_bytes((b"\xef\xbb\xbf" if has_bom else b"") + updated.encode("utf-8"))
    scratch.replace(path)


def _match(pattern: re.Pattern[str], line: str) -> str | None:
    found = pattern.match(line)
    return found.group(1) if found else None
