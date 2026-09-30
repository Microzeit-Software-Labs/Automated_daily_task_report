"""The table-image report: what goes in the table (pure) and that it draws."""

from __future__ import annotations

import datetime as dt
import io

from PIL import Image

from interlock.adapters.rendering.table_image import WIDTH, PillowTableRenderer
from interlock.adapters.whatsapp.local_agent import _send_payload
from interlock.domain.sharing.table import (
    ReportTable,
    build_report_table,
    render_caption,
    row_number,
)
from interlock.domain.tasks.entities import TaskStatus
from interlock.domain.tasks.summary import summarize
from tests.conftest import BRIEF_EVENING, IST, make_task

TODAY = BRIEF_EVENING.astimezone(IST).date()


def table_for(tasks: list, **kwargs: object) -> ReportTable:
    summary = summarize(tasks, today=TODAY, tz=IST)
    return build_report_table(
        tasks,
        summary,
        kind="EVENING",
        now=BRIEF_EVENING,
        tz=IST,
        **kwargs,  # type: ignore[arg-type]
    )


class TestContent:
    def test_uses_the_sheets_own_row_number(self) -> None:
        imported = make_task(external_row_ref="sr:77")
        local = make_task()
        assert row_number(imported) == "77"
        assert row_number(local) == local.display_id

    def test_optional_columns_appear_only_when_used(self) -> None:
        bare = table_for([make_task(title="A")])
        assert bare.columns == ("No.", "Task", "Status")

        full = table_for([make_task(title="A", due_date=TODAY, remarks="waiting on client")])
        assert full.columns == ("No.", "Task", "Status", "Deadline", "Note")
        assert full.wide_columns == frozenset({1, 4})

    def test_overdue_is_stated_in_words(self) -> None:
        late = make_task(title="Late", due_date=TODAY - dt.timedelta(days=5))
        on_time = make_task(title="Fine", due_date=TODAY + dt.timedelta(days=5))
        rows = {row[1]: row for row in table_for([late, on_time]).rows}
        assert rows["Late"][3].endswith("(overdue)")
        assert "overdue" not in rows["Fine"][3]

    def test_lists_open_and_completed_today_only(self) -> None:
        done_today = make_task(
            title="Today", status=TaskStatus.COMPLETED, completed_at=BRIEF_EVENING
        )
        done_before = make_task(
            title="Old",
            status=TaskStatus.COMPLETED,
            completed_at=BRIEF_EVENING - dt.timedelta(days=4),
        )
        open_task = make_task(title="Open", status=TaskStatus.IN_PROGRESS)
        titles = [row[1] for row in table_for([done_today, done_before, open_task]).rows]
        assert titles == ["Open", "Today"]

    def test_caps_rows_and_says_so(self) -> None:
        table = table_for([make_task(title=f"T{i}") for i in range(5)], max_rows=3)
        assert len(table.rows) == 3
        assert table.footer.endswith("2 more not shown")

    def test_caption_is_plain_and_counts_overdue(self) -> None:
        tasks = [make_task(due_date=TODAY - dt.timedelta(days=1))]
        summary = summarize(tasks, today=TODAY, tz=IST)
        caption = render_caption(summary, kind="EVENING", now=BRIEF_EVENING, tz=IST)
        assert caption.startswith("*End of Day Task Update* — 15 Sep 2026, 17:04")
        assert "Overdue 1" in caption
        assert all(ord(ch) < 0x2600 for ch in caption), "no emoji"


class TestRenderer:
    def test_draws_a_png_of_the_table(self) -> None:
        long_word = "x" * 290  # wider than any cell: must be broken, not overflow
        table = table_for([make_task(title="Short"), make_task(title=long_word, remarks="n")])
        png = PillowTableRenderer().render(table)

        image = Image.open(io.BytesIO(png))
        assert image.format == "PNG"
        assert image.width == WIDTH
        assert image.height > 300

    def test_empty_table_still_renders(self) -> None:
        png = PillowTableRenderer().render(table_for([]))
        assert Image.open(io.BytesIO(png)).format == "PNG"

    def test_same_table_same_bytes(self) -> None:
        table = table_for([make_task(title="A")])
        renderer = PillowTableRenderer()
        assert renderer.render(table) == renderer.render(table)


def test_agent_payload_carries_the_image_only_when_there_is_one() -> None:
    assert _send_payload("g@g.us", "hi", None) == {"external_jid": "g@g.us", "body": "hi"}
    with_image = _send_payload("g@g.us", "caption", b"\x89PNG")
    assert with_image["image_b64"] == "iVBORw=="
    assert with_image["body"] == "caption"
