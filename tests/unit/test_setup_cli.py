"""The installer's interactive steps, driven without a terminal: WhatsApp
risk acceptance, the QR code, and linking a phone against the mock provider."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from interlock.setup_cli import enable_whatsapp, link_whatsapp, render_qr

pytestmark = pytest.mark.unit

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Talk:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.asked: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt}")
        return self.answers.pop(0)

    def say(self, message: str) -> None:
        self.said.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.said)


class TestEnableWhatsApp:
    def _env(self, tmp_path: Path, body: str = "WHATSAPP_PROVIDER=mock\nOTHER=1\n") -> Path:
        env = tmp_path / ".env"
        env.write_text(body, encoding="utf-8")
        return env

    def test_an_explicit_yes_turns_it_on_and_nothing_else_changes(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)
        talk = Talk("YES")

        assert enable_whatsapp(env, ask=talk.ask, say=talk.say) is True

        text = env.read_text(encoding="utf-8")
        assert "WHATSAPP_PROVIDER=local_agent" in text
        assert "WHATSAPP_LOCAL_AGENT_TOS_ACK=true" in text
        assert "OTHER=1" in text

    def test_the_notice_names_the_risk_before_asking(self, tmp_path: Path) -> None:
        talk = Talk("YES")

        enable_whatsapp(self._env(tmp_path), ask=talk.ask, say=talk.say)

        assert "AGAINST WhatsApp's Terms of Service" in talk.text
        assert "ban" in talk.text

    @pytest.mark.parametrize("answer", ["", "yes", "y", "Yes", "no", " "])
    def test_anything_but_a_capital_yes_leaves_it_off(self, tmp_path: Path, answer: str) -> None:
        env = self._env(tmp_path)

        assert enable_whatsapp(env, ask=Talk(answer).ask, say=Talk().say) is False

        assert "WHATSAPP_PROVIDER=mock" in env.read_text(encoding="utf-8")
        assert "TOS_ACK" not in env.read_text(encoding="utf-8")

    def test_already_enabled_does_not_ask_again(self, tmp_path: Path) -> None:
        env = self._env(
            tmp_path, "WHATSAPP_PROVIDER=local_agent\nWHATSAPP_LOCAL_AGENT_TOS_ACK=true\n"
        )
        talk = Talk()  # any question would raise

        assert enable_whatsapp(env, ask=talk.ask, say=talk.say) is True
        assert "already enabled" in talk.text

    def test_the_provider_alone_is_not_enough_the_acknowledgement_is_asked_for(
        self, tmp_path: Path
    ) -> None:
        env = self._env(tmp_path, "WHATSAPP_PROVIDER=local_agent\n")

        assert enable_whatsapp(env, ask=Talk("").ask, say=Talk().say) is False

    def test_a_script_can_accept_the_risk_explicitly(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)

        assert enable_whatsapp(env, ask=Talk().ask, say=Talk().say, accept_risk=True) is True
        assert "TOS_ACK=true" in env.read_text(encoding="utf-8")

    def test_a_missing_env_file_is_created(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"

        assert enable_whatsapp(env, ask=Talk("YES").ask, say=Talk().say) is True
        assert "WHATSAPP_PROVIDER=local_agent" in env.read_text(encoding="utf-8")


class TestRenderQr:
    def test_it_is_a_block_of_text_with_explicit_colours(self) -> None:
        art = render_qr("2@abcdefghijklmnop,qrstuvwxyz,0123456789,1")

        lines = art.split("\n")
        assert len(lines) > 10
        assert all("\x1b[" in line and line.endswith("\x1b[0m") for line in lines)

    def test_every_line_is_the_same_width(self) -> None:
        widths = {len(ANSI.sub("", line)) for line in render_qr("hello world 123").split("\n")}

        assert len(widths) == 1

    def test_the_corners_are_white_so_the_code_sits_on_a_light_margin(self) -> None:
        lines = render_qr("hello world 123").split("\n")

        assert lines[0].startswith("\x1b[37;47m▀")  # white on white, whatever the theme
        assert lines[-1].replace("\x1b[0m", "").endswith("\x1b[37;47m▀")

    def test_it_uses_only_black_and_white(self) -> None:
        colours = set(re.findall(r"\x1b\[(3\d;4\d)m", render_qr("hello world 123")))

        assert colours <= {"30;40", "30;47", "37;40", "37;47"}
        assert "30;47" in colours or "37;40" in colours  # it really draws a code

    def test_a_different_payload_draws_a_different_code(self) -> None:
        assert render_qr("one") != render_qr("two")


class Phone:
    """A scripted phone: each ``sleep`` advances a fake clock and runs the next
    scripted event against the mock provider (a rotated QR, a scan, expiry)."""

    def __init__(self, whatsapp: MockWhatsAppProvider, *events: Callable[[], None]) -> None:
        self.whatsapp = whatsapp
        self.events = list(events)
        self.now = 0.0
        self.drawn: list[str] = []

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.events:
            self.events.pop(0)()

    def monotonic(self) -> float:
        return self.now


def _run(
    phone: Phone, talk: Talk, *, patience: float = 300.0, attempts: int = 3
) -> bool:
    return link_whatsapp(
        linking=phone.whatsapp,
        health=phone.whatsapp.health,
        say=talk.say,
        draw=phone.drawn.append,
        sleep=phone.sleep,
        monotonic=phone.monotonic,
        patience=patience,
        attempts=attempts,
    )


@pytest.fixture
def whatsapp() -> MockWhatsAppProvider:
    return MockWhatsAppProvider(FrozenClock(dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)))


class TestLinkWhatsApp:
    def test_scanning_the_code_links_the_phone(self, whatsapp: MockWhatsAppProvider) -> None:
        phone = Phone(whatsapp, lambda: whatsapp.complete_link("919876543210:5@s.whatsapp.net"))
        talk = Talk()

        assert _run(phone, talk) is True

        assert phone.drawn == ["mock-qr-1"]
        assert "WhatsApp connected successfully as +919876543210" in talk.text
        assert "Linked Devices" in talk.text  # told how to scan

    def test_a_rotating_code_is_redrawn_each_time_it_changes(
        self, whatsapp: MockWhatsAppProvider
    ) -> None:
        phone = Phone(
            whatsapp,
            whatsapp.advance_qr,
            whatsapp.advance_qr,
            lambda: whatsapp.complete_link("919876543210:5@s.whatsapp.net"),
        )

        assert _run(phone, Talk()) is True

        assert phone.drawn == ["mock-qr-1", "mock-qr-2", "mock-qr-3"]

    def test_the_same_code_is_not_drawn_twice(self, whatsapp: MockWhatsAppProvider) -> None:
        phone = Phone(
            whatsapp,
            lambda: None,
            lambda: None,
            lambda: whatsapp.complete_link("919876543210:5@s.whatsapp.net"),
        )

        _run(phone, Talk())

        assert phone.drawn == ["mock-qr-1"]

    def test_an_already_linked_phone_is_left_alone(self, whatsapp: MockWhatsAppProvider) -> None:
        whatsapp.complete_link("919876543210:5@s.whatsapp.net")
        phone = Phone(whatsapp)
        talk = Talk()

        assert _run(phone, talk) is True

        assert phone.drawn == []
        assert "already linked as +919876543210" in talk.text

    def test_the_service_not_running_is_explained(self, whatsapp: MockWhatsAppProvider) -> None:
        whatsapp.given_agent_offline()
        talk = Talk()

        assert _run(Phone(whatsapp), talk) is False

        assert "isn't running" in talk.text

    def test_an_expired_code_is_retried_with_a_fresh_one(
        self, whatsapp: MockWhatsAppProvider
    ) -> None:
        phone = Phone(
            whatsapp,
            whatsapp.expire_link,
            lambda: whatsapp.complete_link("919876543210:5@s.whatsapp.net"),
        )
        talk = Talk()

        assert _run(phone, talk) is True

        assert phone.drawn == ["mock-qr-1", "mock-qr-2"]
        assert "Trying again" in talk.text

    def test_nobody_scanning_gives_up_and_says_how_to_link_later(
        self, whatsapp: MockWhatsAppProvider
    ) -> None:
        talk = Talk()

        assert _run(Phone(whatsapp), talk, patience=5.0, attempts=1) is False

        assert "Nobody scanned" in talk.text
        assert "Settings > WhatsApp" in talk.text

    def test_it_stops_after_the_allowed_attempts(self, whatsapp: MockWhatsAppProvider) -> None:
        phone = Phone(whatsapp, whatsapp.expire_link, whatsapp.expire_link, whatsapp.expire_link)

        assert _run(phone, Talk(), attempts=3) is False

        assert phone.drawn == ["mock-qr-1", "mock-qr-2", "mock-qr-3"]

    def test_the_service_stopping_midway_is_reported(
        self, whatsapp: MockWhatsAppProvider
    ) -> None:
        phone = Phone(whatsapp, whatsapp.given_agent_offline)
        talk = Talk()

        assert _run(phone, talk) is False

        assert "stopped" in talk.text
