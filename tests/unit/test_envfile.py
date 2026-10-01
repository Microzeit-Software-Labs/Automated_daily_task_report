"""Editing .env in place: only the asked-for keys change."""

from __future__ import annotations

from pathlib import Path

import pytest

from interlock.envfile import read_value, set_values, update_file

pytestmark = pytest.mark.unit

SAMPLE = (
    "# Interlock\n"
    "DATABASE_URL=postgresql://x\n"
    "\n"
    "# --- WhatsApp\n"
    "WHATSAPP_PROVIDER=mock\n"
    "# WHATSAPP_LOCAL_AGENT_TOS_ACK=true\n"
    "SHEETS_PROVIDER=mock\n"
)


class TestSetValues:
    def test_replaces_an_active_line_in_place(self) -> None:
        out = set_values(SAMPLE, {"WHATSAPP_PROVIDER": "local_agent"})

        assert "WHATSAPP_PROVIDER=local_agent\n" in out
        assert "WHATSAPP_PROVIDER=mock" not in out
        assert out.index("WHATSAPP_PROVIDER") < out.index("SHEETS_PROVIDER")

    def test_a_commented_example_becomes_the_active_line_where_it_stood(self) -> None:
        out = set_values(SAMPLE, {"WHATSAPP_LOCAL_AGENT_TOS_ACK": "true"})

        lines = out.split("\n")
        assert "WHATSAPP_LOCAL_AGENT_TOS_ACK=true" in lines
        assert "# WHATSAPP_LOCAL_AGENT_TOS_ACK=true" not in lines
        provider = lines.index("WHATSAPP_PROVIDER=mock")
        assert lines.index("WHATSAPP_LOCAL_AGENT_TOS_ACK=true") == provider + 1

    def test_a_missing_key_is_appended(self) -> None:
        out = set_values(SAMPLE, {"NEW_KEY": "1"})

        assert out.endswith("SHEETS_PROVIDER=mock\nNEW_KEY=1\n")

    def test_nothing_else_changes(self) -> None:
        out = set_values(
            SAMPLE,
            {"WHATSAPP_PROVIDER": "local_agent", "WHATSAPP_LOCAL_AGENT_TOS_ACK": "true"},
        )

        changed = ("WHATSAPP_PROVIDER", "# WHATSAPP_LOCAL")
        untouched = [line for line in SAMPLE.split("\n") if not line.startswith(changed)]
        assert all(line in out.split("\n") for line in untouched)

    def test_crlf_files_stay_crlf(self) -> None:
        out = set_values(SAMPLE.replace("\n", "\r\n"), {"WHATSAPP_PROVIDER": "local_agent"})

        assert "\r\n" in out
        assert "\n" not in out.replace("\r\n", "")

    def test_a_file_without_a_final_newline_keeps_that(self) -> None:
        assert not set_values("A=1", {"A": "2"}).endswith("\n")

    def test_an_empty_file_gets_the_value(self) -> None:
        assert set_values("", {"A": "1"}) == "A=1\n"

    def test_a_value_containing_equals_survives(self) -> None:
        assert set_values("A=1\n", {"A": "x=y"}) == "A=x=y\n"

    def test_it_is_idempotent(self) -> None:
        once = set_values(SAMPLE, {"WHATSAPP_PROVIDER": "local_agent"})

        assert set_values(once, {"WHATSAPP_PROVIDER": "local_agent"}) == once

    def test_a_key_that_only_starts_with_the_name_is_not_confused_for_it(self) -> None:
        out = set_values("WHATSAPP_PROVIDER_EXTRA=1\n", {"WHATSAPP_PROVIDER": "x"})

        assert "WHATSAPP_PROVIDER_EXTRA=1" in out
        assert "WHATSAPP_PROVIDER=x" in out


class TestReadValue:
    def test_reads_the_active_value(self) -> None:
        assert read_value(SAMPLE, "WHATSAPP_PROVIDER") == "mock"

    def test_ignores_a_commented_line(self) -> None:
        assert read_value(SAMPLE, "WHATSAPP_LOCAL_AGENT_TOS_ACK") is None

    def test_the_last_one_wins(self) -> None:
        assert read_value("A=1\nA=2\n", "A") == "2"

    def test_missing(self) -> None:
        assert read_value(SAMPLE, "NOPE") is None


class TestUpdateFile:
    def test_updates_a_file_and_keeps_a_byte_order_mark(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_bytes(b"\xef\xbb\xbf" + SAMPLE.replace("\n", "\r\n").encode())

        update_file(env, {"WHATSAPP_PROVIDER": "local_agent"})

        raw = env.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"WHATSAPP_PROVIDER=local_agent\r\n" in raw
        assert b"# Interlock" in raw

    def test_a_file_without_a_bom_gets_none(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(SAMPLE, encoding="utf-8")

        update_file(env, {"NEW": "1"})

        assert not env.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_creates_a_missing_file(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"

        update_file(env, {"A": "1"})

        assert env.read_text(encoding="utf-8") == "A=1\n"

    def test_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("A=1\n", encoding="utf-8")

        update_file(env, {"A": "2"})

        assert [p.name for p in tmp_path.iterdir()] == [".env"]
