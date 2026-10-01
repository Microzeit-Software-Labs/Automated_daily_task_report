"""The interactive set-up steps the installer runs (and that you can run by hand).

    python -m interlock.setup_cli sheet              connect the Google Sheet
    python -m interlock.setup_cli whatsapp-enable    accept the risk, turn WhatsApp on
    python -m interlock.setup_cli whatsapp-link      scan a QR code to link a phone
    python -m interlock.setup_cli db-status          can the app and the agent reach the database?

None of this is a second implementation: the sheet step is the same
``SheetSourceService`` the Settings page uses (check, then save), and the
WhatsApp step drives the same ``LocalAgentProvider`` linking the Settings page
does, so the terminal and the browser can never disagree about what "linked"
means. Each step is a small function taking its prompts and output as arguments,
so it is tested without a terminal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.base import make_engine, make_session_factory
from interlock.adapters.persistence.sheet_source_repository import SheetSourceRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.sheets.factory import build_sheet_reader
from interlock.adapters.whatsapp.factory import build_whatsapp_provider
from interlock.config import Settings, WhatsAppProviderName, get_settings
from interlock.domain.common.actor import Actor, ActorKind
from interlock.domain.common.clock import SystemClock
from interlock.domain.common.errors import ProviderUnavailableError
from interlock.domain.ports.whatsapp import (
    ConnectionState,
    PairingState,
    ProviderStatus,
    WhatsAppLinking,
    phone_from_jid,
)
from interlock.envfile import read_value, update_file
from interlock.services.sheet_source_service import SheetSourceService

ROOT = Path(__file__).resolve().parents[2]

Say = Callable[[str], None]
Ask = Callable[[str], str]

RISK_NOTICE = """
Before WhatsApp can be connected, please read this.

  Interlock reaches your existing WhatsApp groups by acting as a linked device
  (like WhatsApp Web), using an unofficial library called Baileys.

  * This is AGAINST WhatsApp's Terms of Service.
  * WhatsApp can ban the number you link. Use a number you can afford to lose,
    and know that it is your own account that carries the risk.
  * Interlock sends only reports you have approved, a few messages a day, which
    keeps the footprint small. That reduces the risk; it does not remove it.

  Details: docs/whatsapp-agent-setup.md
"""


# -- the Google Sheet ---------------------------------------------------------


def connect_sheet(
    *,
    service: SheetSourceService,
    commit: Callable[[], None],
    ask: Ask,
    say: Say,
    now: Callable[[], dt.datetime],
    actor: Actor,
    url: str | None = None,
    keep_existing: bool = False,
    attempts: int = 3,
) -> bool:
    """Connect (or keep) the Google Sheet. Returns whether one is connected.

    A link that can't be used is explained and asked for again, up to
    ``attempts`` times; a blank answer skips the step (a sheet can be connected
    later from Settings)."""
    current = service.current(now=now())
    commit()  # keeps a first-time seed from .env
    if current is not None and current.configured:
        say(f"A Google Sheet is already connected: {current.url}")
        if url is None and (
            keep_existing or ask("Keep it? [Y/n] ").strip().lower() not in ("n", "no")
        ):
            return True

    candidate = url
    for _ in range(attempts):
        if candidate is None:
            candidate = ask(
                "Paste the link to your Google Sheet "
                "(Enter to skip, connect it later in Settings): "
            ).strip()
        if not candidate:
            say("Skipped. You can connect a sheet any time in Settings > Google Sheet.")
            return False

        check = service.check(candidate, now=now())
        if not check.ok:
            problem = check.problem
            say(f"That sheet can't be used: {problem.message if problem else 'unknown problem'}")
            candidate = None
            continue

        say(f"Found {check.task_count} task(s). Most recent: " + "; ".join(check.sample_titles))
        if check.skipped:
            say(f"  ({check.skipped} row(s) skipped: no Sr No. or task, or a repeated Sr No.)")
        if check.will_hide:
            say(
                f"  {check.will_hide} task(s) from the current sheet will be hidden from reports "
                "(not deleted: switching back restores them)."
            )
        service.apply(candidate, actor=actor, now=now())
        commit()
        say(f"OK: Google Sheet connected successfully - {check.task_count} tasks found.")
        return True

    say("Giving up on the sheet for now. Connect it later in Settings > Google Sheet.")
    return False


# -- WhatsApp -------------------------------------------------------------------


def enable_whatsapp(
    env_path: Path, *, ask: Ask, say: Say, accept_risk: bool = False
) -> bool:
    """Turn the local WhatsApp agent on in ``.env``, after an explicit YES to the
    terms-of-service notice. Returns whether it is now enabled."""
    text = env_path.read_text(encoding="utf-8-sig") if env_path.exists() else ""
    already = (
        read_value(text, "WHATSAPP_PROVIDER") == "local_agent"
        and (read_value(text, "WHATSAPP_LOCAL_AGENT_TOS_ACK") or "").lower() == "true"
    )
    if already:
        say("WhatsApp is already enabled.")
        return True

    say(RISK_NOTICE)
    if not accept_risk:
        answer = ask("Type YES (in capitals) to accept this and continue, or press Enter to skip: ")
        if answer.strip() != "YES":
            say("Skipped. WhatsApp stays off; run this again when you decide.")
            return False

    update_file(
        env_path,
        {"WHATSAPP_PROVIDER": "local_agent", "WHATSAPP_LOCAL_AGENT_TOS_ACK": "true"},
    )
    say("OK: WhatsApp enabled in .env.")
    return True


def render_qr(payload: str, *, border: int = 3) -> str:
    """The QR code as terminal text: black and white by explicit colour, two rows
    of the code per line of text (upper-half blocks). Colours are set outright,
    never left to the terminal theme: a dark theme would otherwise invert the
    code, and phones often won't scan an inverted one."""
    import segno

    matrix = [[bool(cell) for cell in row] for row in segno.make(payload, error="l").matrix]
    size = len(matrix)

    def dark(row: int, col: int) -> bool:
        return 0 <= row < size and 0 <= col < size and matrix[row][col]

    lines = []
    for row in range(-border, size + border, 2):
        cells = [
            f"\x1b[3{0 if dark(row, col) else 7};4{0 if dark(row + 1, col) else 7}m▀"
            for col in range(-border, size + border)
        ]
        lines.append("".join(cells) + "\x1b[0m")
    return "\n".join(lines)


def link_whatsapp(
    *,
    linking: WhatsAppLinking,
    health: Callable[[], ProviderStatus],
    say: Say,
    draw: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    patience: float = 300.0,
    attempts: int = 3,
) -> bool:
    """Link a phone by QR code, in the terminal. Returns whether it is linked."""
    status = health()
    if status.state is ConnectionState.CONNECTED and status.account_jid:
        say(f"WhatsApp is already linked as {phone_from_jid(status.account_jid) or 'a phone'}.")
        return True

    for attempt in range(1, attempts + 1):
        try:
            linking.start_link()
        except ProviderUnavailableError as error:
            say(f"The WhatsApp service isn't running ({error.message}). Start Interlock first.")
            return False

        deadline = monotonic() + patience
        shown: str | None = None
        announced_scan = False
        while True:
            link = linking.link_status()
            if not link.agent_online:
                say("The WhatsApp service stopped. Start Interlock and try again.")
                return False
            if link.state is PairingState.WAITING_FOR_SCAN and link.qr and link.qr != shown:
                if shown is None:
                    say(
                        "On your phone: WhatsApp > Settings > Linked Devices > Link a Device, "
                        "then scan this code (it refreshes itself every ~20 seconds):"
                    )
                draw(link.qr)
                shown = link.qr
            elif link.state is PairingState.SCANNED and not announced_scan:
                say("Scanned. Finishing the link...")
                announced_scan = True
            elif link.state is PairingState.SUCCEEDED:
                phone = _account_phone(health, sleep)
                say(f"OK: WhatsApp connected successfully{f' as {phone}' if phone else ''}.")
                return True
            elif link.state in (PairingState.EXPIRED, PairingState.FAILED, PairingState.CANCELLED):
                say(f"That attempt ended ({link.state.value.lower()}). {link.detail}".rstrip())
                break
            if monotonic() > deadline:
                linking.cancel_link()
                say("Nobody scanned the code in time.")
                break
            sleep(1.0)

        if attempt < attempts:
            say(f"Trying again ({attempt + 1} of {attempts})...")
    say("WhatsApp is not linked yet. You can link it any time in Settings > WhatsApp.")
    return False


def _account_phone(
    health: Callable[[], ProviderStatus], sleep: Callable[[float], None]
) -> str | None:
    """The number just linked. The status row catches up a moment after the link
    succeeds, so give it a few seconds."""
    for _ in range(15):
        phone = phone_from_jid(health().account_jid)
        if phone:
            return phone
        sleep(1.0)
    return None


# -- is the database reachable? ------------------------------------------------------


def database_status(settings: Settings, agent_env: Path) -> dict[str, str]:
    """``{"app": ..., "agent": ...}``, each ``ok``, ``missing`` or ``failed: <why>``."""
    from sqlalchemy import create_engine, text

    def probe(url: str) -> str:
        engine = create_engine(url, connect_args={"connect_timeout": 5})
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return "ok"
        except Exception as error:
            return f"failed: {type(error).__name__}"
        finally:
            engine.dispose()

    result = {"app": probe(settings.database_url)}
    agent_url = (
        read_value(agent_env.read_text(encoding="utf-8-sig"), "DATABASE_URL")
        if agent_env.exists()
        else None
    )
    result["agent"] = (
        "missing"
        if not agent_url
        else probe(agent_url.replace("postgresql://", "postgresql+psycopg://", 1))
    )
    return result


# -- command line ----------------------------------------------------------------------


def _prepare_console() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.platform == "win32":
        os.system("")  # switches the Windows console to understanding colour codes


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # no terminal to ask on: treat as "skip"
        return ""


def _say(message: str) -> None:
    print(message, flush=True)


def _draw(payload: str) -> None:
    print(render_qr(payload), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="interlock.setup_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    sheet = commands.add_parser("sheet", help="connect the Google Sheet")
    sheet.add_argument("--url", help="the sheet link (otherwise you are asked)")
    sheet.add_argument(
        "--keep-existing", action="store_true", help="don't ask about a connected sheet"
    )
    enable = commands.add_parser("whatsapp-enable", help="accept the risk and turn WhatsApp on")
    enable.add_argument(
        "--accept-risk", action="store_true", help="skip the typed YES (scripts only)"
    )
    commands.add_parser("whatsapp-link", help="link a phone by scanning a QR code")
    commands.add_parser("db-status", help="can the app and the agent reach the database?")
    args = parser.parse_args(argv)

    _prepare_console()
    env_path = ROOT / ".env"

    if args.command == "whatsapp-enable":
        enabled = enable_whatsapp(env_path, ask=_ask, say=_say, accept_risk=args.accept_risk)
        return 0 if enabled else 1

    settings = get_settings()
    if args.command == "db-status":
        result = database_status(settings, ROOT / "apps" / "agent" / ".env")
        for name, state in result.items():
            _say(f"{name}: {state}")
        return 0 if all(state == "ok" for state in result.values()) else 1

    clock = SystemClock()
    engine = make_engine(settings.database_url)
    try:
        if args.command == "sheet":
            session = make_session_factory(engine)()
            try:
                service = SheetSourceService(
                    repo=SheetSourceRepository(session),
                    task_repo=PostgresTaskRepository(session),
                    audit=PostgresAuditSink(session),
                    tz=settings.tz,
                    reader=build_sheet_reader(settings),
                    seed_url=settings.sheet_import_url,
                )
                connected = connect_sheet(
                    service=service,
                    commit=session.commit,
                    ask=_ask,
                    say=_say,
                    now=clock.now,
                    actor=Actor(kind=ActorKind.USER, id="installer", label="installer"),
                    url=args.url,
                    keep_existing=args.keep_existing,
                )
            finally:
                session.close()
            return 0 if connected else 1

        # whatsapp-link
        if settings.whatsapp_provider is not WhatsAppProviderName.LOCAL_AGENT:
            _say("WhatsApp isn't enabled. Run: python -m interlock.setup_cli whatsapp-enable")
            return 1
        provider = build_whatsapp_provider(
            settings, clock=clock, session_factory=make_session_factory(engine)
        )
        if not isinstance(provider, WhatsAppLinking):
            _say("This WhatsApp provider can't be linked by QR code.")
            return 1
        return 0 if link_whatsapp(
            linking=provider, health=provider.health, say=_say, draw=_draw
        ) else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
