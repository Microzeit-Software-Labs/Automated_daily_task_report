"""Export the API's OpenAPI schema to docs/openapi.json.

Run after any change to a router or schema, so the committed contract stays
in sync with what the API actually serves. This is what lets frontend work
start against a stable contract before the frontend itself exists (Phase 2).

Usage: python scripts/export_openapi.py
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from interlock.api.main import create_app  # noqa: E402 -- after load_dotenv() on purpose


def main() -> None:
    app = create_app()
    schema = app.openapi()

    repo_root = Path(__file__).resolve().parent.parent
    out_path = repo_root / "docs" / "openapi.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {out_path.relative_to(repo_root)} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
