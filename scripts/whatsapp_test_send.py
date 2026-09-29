"""Manually enqueue a WhatsApp test_send through the local agent, and wait
for the result.

Use this right after first pairing the agent (see
docs/whatsapp-agent-setup.md) to confirm sends actually work, without
waiting for the weekly canary or a real approved report. Requires
WHATSAPP_PROVIDER=local_agent and a running agent process (apps/agent).

test_send is not part of the WhatsAppProvider port (see
domain/ports/whatsapp.py) -- it is an agent-internal op, the same one the
weekly canary uses -- so this script enqueues it directly through
WhatsAppAgentRepository rather than going through a provider.

Usage: python scripts/whatsapp_test_send.py
"""

from __future__ import annotations

import datetime as dt
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from interlock.adapters.persistence.base import make_engine, make_session_factory  # noqa: E402
from interlock.adapters.persistence.whatsapp_agent_repository import (  # noqa: E402
    WhatsAppAgentRepository,
)
from interlock.config import get_settings  # noqa: E402
from interlock.domain.common.ids import new_id  # noqa: E402

_TIMEOUT = dt.timedelta(seconds=30)
_POLL_INTERVAL = dt.timedelta(milliseconds=200)


def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    command_id = new_id()
    now = dt.datetime.now(dt.UTC)
    expires_at = now + _TIMEOUT

    session = session_factory()
    try:
        WhatsAppAgentRepository(session).enqueue(
            command_id=command_id,
            op="test_send",
            payload={"reason": "manual"},
            client_message_id=None,
            now=now,
            expires_at=expires_at,
        )
        session.commit()
    finally:
        session.close()

    print(f"enqueued test_send {command_id} -- waiting up to {_TIMEOUT} for the agent...")

    deadline = time.monotonic() + _TIMEOUT.total_seconds()
    while time.monotonic() < deadline:
        session = session_factory()
        try:
            record = WhatsAppAgentRepository(session).get_by_id(command_id)
        finally:
            session.close()

        if record is not None and record.status == "DONE":
            if record.result and record.result.get("accepted"):
                print(f"sent -- provider_message_id={record.result.get('provider_message_id')}")
                return
            print(f"failed -- {record.result}")
            sys.exit(1)

        time.sleep(_POLL_INTERVAL.total_seconds())

    print(
        "timed out waiting for the agent. Is it running? "
        "(cd apps/agent && npm start) See docs/whatsapp-agent-setup.md."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
