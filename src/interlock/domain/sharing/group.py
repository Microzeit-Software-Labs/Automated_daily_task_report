"""A configured WhatsApp delivery target.

``external_jid`` is pinned permanently once a human confirms a candidate from
``WhatsAppProvider.resolve_group`` (see the port's docstring) -- it is never
re-resolved by name at send time, so a group rename or a second
similarly-named group cannot silently redirect a report.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from interlock.domain.common.clock import ensure_aware


@dataclasses.dataclass(frozen=True, slots=True)
class WhatsAppGroup:
    id: str
    display_name: str
    external_jid: str
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    description: str = ""
    default_morning: bool = False
    default_evening: bool = False
    last_used_at: dt.datetime | None = None
    resolved_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.created_at, field="whatsapp_group.created_at")
        ensure_aware(self.updated_at, field="whatsapp_group.updated_at")
