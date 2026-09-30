"""Turning a report table into an image -- a port, because it needs fonts
(file I/O) and an imaging library, neither of which the domain may import."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from interlock.domain.sharing.table import ReportTable


@runtime_checkable
class ReportImageRenderer(Protocol):
    def render(self, table: ReportTable) -> bytes:
        """PNG bytes. Deterministic for the same table and fonts."""
        ...
