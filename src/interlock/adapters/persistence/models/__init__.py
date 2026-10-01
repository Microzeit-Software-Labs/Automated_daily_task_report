"""Import every model module so Base.metadata is fully populated.

Alembic's autogenerate diffs against ``Base.metadata``; a model defined but
never imported here would be invisible to it.
"""

from interlock.adapters.persistence.models.approvals import (
    ApprovalRequestRow,
    ReportSnapshotRow,
    ShareJobRow,
    ShareRecipientRow,
)
from interlock.adapters.persistence.models.audit import AuditLogRow
from interlock.adapters.persistence.models.idempotency import IdempotencyKeyRow
from interlock.adapters.persistence.models.scheduler import ScheduledActionRow
from interlock.adapters.persistence.models.sheet_source import SheetSourceRow
from interlock.adapters.persistence.models.sync import SyncConflictRow, TaskSheetSyncRow
from interlock.adapters.persistence.models.tasks import TaskHistoryRow, TaskRow
from interlock.adapters.persistence.models.users import UserRow
from interlock.adapters.persistence.models.whatsapp import WhatsAppGroupRow
from interlock.adapters.persistence.models.whatsapp_agent import (
    WhatsAppAgentCommandRow,
    WhatsAppAgentStatusRow,
)

__all__ = [
    "ApprovalRequestRow",
    "AuditLogRow",
    "IdempotencyKeyRow",
    "ReportSnapshotRow",
    "ScheduledActionRow",
    "ShareJobRow",
    "ShareRecipientRow",
    "SheetSourceRow",
    "SyncConflictRow",
    "TaskHistoryRow",
    "TaskRow",
    "TaskSheetSyncRow",
    "UserRow",
    "WhatsAppAgentCommandRow",
    "WhatsAppAgentStatusRow",
    "WhatsAppGroupRow",
]
