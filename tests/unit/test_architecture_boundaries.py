"""Exit criterion 5: domain/ imports no adapter and no I/O library.

This is the one architectural rule everything else in this codebase depends
on staying true, and "we were careful" is not a proof -- this walks every
module under ``interlock.domain`` and parses its actual import statements,
so a violation fails a test in CI rather than being caught (or missed) in
review. Domain code may only import the standard library and other domain
modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DOMAIN_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "interlock" / "domain"

# Anything not starting with "interlock." is assumed to be a third-party or
# stdlib import; stdlib is fine, third-party is checked against this list of
# packages the domain must never depend on -- every one of them is an
# adapter-layer or I/O concern (a database driver, a web framework, an ORM,
# a migration tool, an HTTP client).
FORBIDDEN_THIRD_PARTY_PREFIXES = (
    "sqlalchemy",
    "psycopg",
    "alembic",
    "fastapi",
    "starlette",
    "httpx",
    "pydantic_settings",  # Settings/env parsing is a composition-root concern
)
# Deliberately not included: `logging`/`structlog`. Logging is an orthogonal,
# cross-cutting concern in most hexagonal architectures, not an adapter --
# unlike a database driver or web framework, using it does not make domain
# logic depend on infrastructure or untestable without it. The real target of
# this check is I/O boundaries: things that require a live database,
# network, or running server to exist.


def _iter_domain_modules() -> list[Path]:
    return sorted(DOMAIN_ROOT.rglob("*.py"))


def _imported_top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


class TestDomainNeverImportsAnAdapter:
    @pytest.mark.parametrize(
        "path", _iter_domain_modules(), ids=lambda p: str(p.relative_to(DOMAIN_ROOT))
    )
    def test_module_does_not_import_interlock_adapters(self, path: Path) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module_name = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module_name = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("interlock.adapters"):
                        module_name = alias.name

            if module_name and module_name.startswith("interlock.adapters"):
                pytest.fail(
                    f"{path.relative_to(DOMAIN_ROOT)} imports {module_name!r} -- "
                    "domain code must never import an adapter. If domain logic "
                    "needs this capability, express it as a port in "
                    "domain/ports/ and let the composition root inject the "
                    "concrete adapter instead."
                )

    @pytest.mark.parametrize(
        "path", _iter_domain_modules(), ids=lambda p: str(p.relative_to(DOMAIN_ROOT))
    )
    def test_module_does_not_import_forbidden_io_libraries(self, path: Path) -> None:
        names = _imported_top_level_names(path)
        violations = names & set(FORBIDDEN_THIRD_PARTY_PREFIXES)
        assert not violations, (
            f"{path.relative_to(DOMAIN_ROOT)} imports {sorted(violations)} -- "
            "these are adapter-layer/I-O libraries. Domain code must depend "
            "only on the standard library and other domain modules."
        )

    def test_this_check_actually_discovered_domain_modules(self) -> None:
        """A guard against the check silently passing because DOMAIN_ROOT
        resolved to an empty or wrong directory."""
        modules = _iter_domain_modules()
        assert len(modules) > 20, (
            f"Expected well over 20 domain modules, found {len(modules)} at "
            f"{DOMAIN_ROOT}. DOMAIN_ROOT is probably wrong."
        )
