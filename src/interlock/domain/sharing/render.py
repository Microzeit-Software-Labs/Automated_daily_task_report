"""Rendering the WhatsApp report.

Templates are user-editable, which makes them untrusted input even though the
only user is the owner of the system. They run in a Jinja2 sandbox with:

* no attribute access beyond the whitelisted context,
* strict undefined -- a typo'd variable is an error at save time, not a silently
  blank line in a message already on its way to the Management group,
* a hard output cap.

WhatsApp formatting is deliberately plain text: ``*bold*``, ``_italic_``. There
is no markup layer to escape into, so the sandbox is about template safety
rather than output injection.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from interlock.domain.common.clock import ensure_aware
from interlock.domain.common.errors import MessageTooLongError, TemplateInvalidError
from interlock.domain.tasks.derivations import is_overdue, sort_key_for_report
from interlock.domain.tasks.entities import Task, TaskStatus
from interlock.domain.tasks.summary import TaskSummary

# Everything a template may reference. Anything else raises at validation time.
ALLOWED_VARIABLES: frozenset[str] = frozenset(
    {
        "title",
        "date",
        "time",
        "day_name",
        "completed",
        "in_progress",
        "pending",
        "blocked",
        "deferred",
        "overdue",
        "due_today",
        "total",
        "remaining",
        "newly_added",
        "modified_today",
        "tasks",
    }
)

STATUS_GLYPHS: dict[TaskStatus, str] = {
    TaskStatus.COMPLETED: "✅",  # white heavy check mark
    TaskStatus.IN_PROGRESS: "\U0001f7e1",  # yellow circle
    TaskStatus.PENDING: "\U0001f534",  # red circle
    TaskStatus.BLOCKED: "⛔",  # no entry
    TaskStatus.DEFERRED: "⏸️",  # pause
}

OVERDUE_GLYPH = "⚠️"

STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.COMPLETED: "Completed",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.PENDING: "Pending",
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.DEFERRED: "Deferred",
}

MORNING_TEMPLATE_ID = "morning.default"
EVENING_TEMPLATE_ID = "evening.default"

DEFAULT_MORNING_TEMPLATE = """*Morning Task Update*

\U0001f4c5 {{ date }}
⏰ {{ time }}

✅ Completed: {{ completed }}
\U0001f7e1 In Progress: {{ in_progress }}
\U0001f534 Pending: {{ pending }}
⚠️ Overdue: {{ overdue }}

*Today's Focus*
{% for task in tasks %}
{{ loop.index }}. {{ task.title }}
   {{ task.glyph }} {{ task.status_label }}{% if task.overdue %} — overdue{% endif %}
{% endfor %}

*Summary*
Remaining: {{ remaining }}
Overdue: {{ overdue }}
"""

DEFAULT_EVENING_TEMPLATE = """*End of Day Task Update*

\U0001f4c5 {{ date }}
⏰ {{ time }}

✅ Completed: {{ completed }}
\U0001f7e1 In Progress: {{ in_progress }}
\U0001f534 Pending: {{ pending }}
⚠️ Overdue: {{ overdue }}
⛔ Blocked: {{ blocked }}

*Priority Tasks*
{% for task in tasks %}
{{ loop.index }}. {{ task.title }}
   {{ task.glyph }} {{ task.status_label }}{% if task.overdue %} — overdue{% endif %}
{% endfor %}

*Summary*
Completed: {{ completed }}
Remaining: {{ remaining }}
Overdue: {{ overdue }}
"""

REVIEW_TITLES: dict[str, str] = {
    "MORNING": "Morning Task Review",
    "EVENING": "End of Day Task Review",
    "MANUAL": "Task Update",
}


def default_template_for(kind: str) -> tuple[str, str]:
    """``(template_id, template_source)`` for a review kind.

    A MANUAL ("share the list right now") review uses the evening template's
    shape -- a general status snapshot reads more naturally than "today's
    focus" when it might be triggered at any hour.
    """
    if kind == "MORNING":
        return MORNING_TEMPLATE_ID, DEFAULT_MORNING_TEMPLATE
    return EVENING_TEMPLATE_ID, DEFAULT_EVENING_TEMPLATE


def _environment() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # plain text destination; there is no markup to escape
    )
    env.globals.clear()  # no range(), no dict(), no lipsum()
    return env


def build_context(
    tasks: Sequence[Task],
    summary: TaskSummary,
    *,
    now: dt.datetime,
    tz: ZoneInfo,
    title: str = "",
    max_tasks: int = 15,
) -> dict[str, Any]:
    """The whitelisted variables a template may use.

    Tasks are pre-sorted by report urgency and pre-flattened to plain dicts, so
    a template cannot reach into the entity and a template author cannot depend
    on internals like ``version``.
    """
    ensure_aware(now, field="now")
    local = now.astimezone(tz)
    today = local.date()

    ordered = sorted(
        (task for task in tasks if not task.is_deleted),
        key=lambda task: sort_key_for_report(task, today),
    )

    rendered_tasks = [
        {
            "display_id": task.display_id,
            "title": task.title,
            "status": task.status.value,
            "status_label": STATUS_LABELS[task.status],
            "glyph": STATUS_GLYPHS[task.status],
            "priority": task.priority.value,
            "overdue": is_overdue(task.due_date, task.status, today),
            "due_date": task.due_date.isoformat() if task.due_date else "",
            "owner": task.owner_id or "",
            "remarks": task.remarks,
        }
        for task in ordered[:max_tasks]
    ]

    # Widened deliberately: the summary contributes ints, the rest contributes
    # strings and the task list. Mutating the dict[str, int] in place would be a
    # type error that only shows up as a confusing failure much later.
    context: dict[str, Any] = dict(summary.as_dict())
    context.update(
        {
            "title": title,
            "date": f"{local:%d %B %Y}",
            # %-I is glibc-only and raises on Windows, so strip the pad instead.
            "time": f"{local:%I:%M %p}".lstrip("0"),
            "day_name": f"{local:%A}",
            "tasks": rendered_tasks,
        }
    )
    return context


def validate_template(source: str) -> None:
    """Check a template compiles and references only allowed variables.

    Called when a template is saved, so a broken template is rejected at edit
    time rather than discovered at 17:00 when a report fails to render.
    """
    env = _environment()
    try:
        parsed = env.parse(source)
    except TemplateError as exc:
        raise TemplateInvalidError(f"Template does not compile: {exc}") from exc

    from jinja2 import meta

    referenced = meta.find_undeclared_variables(parsed)
    unknown = referenced - ALLOWED_VARIABLES
    if unknown:
        raise TemplateInvalidError(
            f"Template uses unknown variables: {', '.join(sorted(unknown))}.",
            unknown=sorted(unknown),
            allowed=sorted(ALLOWED_VARIABLES),
        )


def render_report(
    source: str,
    context: dict[str, Any],
    *,
    max_length: int,
) -> str:
    """Render a validated template against a built context.

    Raises rather than truncating on overflow: a silently cut-off report is
    worse than a report that fails loudly before anyone approves it.
    """
    env = _environment()
    try:
        body = env.from_string(source).render(**context)
    except TemplateError as exc:
        raise TemplateInvalidError(f"Template failed to render: {exc}") from exc

    body = _tidy(body)

    if len(body) > max_length:
        raise MessageTooLongError(
            f"Rendered report is {len(body)} characters; the limit is {max_length}. "
            "Reduce the number of tasks included or shorten the template.",
            length=len(body),
            limit=max_length,
        )
    return body


def _tidy(body: str) -> str:
    """Collapse the blank-line noise Jinja loops leave behind."""
    lines = [line.rstrip() for line in body.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()
