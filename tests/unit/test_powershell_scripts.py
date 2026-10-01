"""The PowerShell scripts must parse, and must be plain ASCII.

Windows PowerShell 5.1 reads a script without a byte-order mark as the machine's
ANSI code page, so one stray dash or curly quote becomes garbage (or a syntax
error) on someone else's computer. This catches both mistakes where they are
made, instead of on a fresh install.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted([*ROOT.glob("*.ps1"), *(ROOT / "scripts").glob("*.ps1")])


def test_there_are_scripts_to_check() -> None:
    assert {p.name for p in SCRIPTS} >= {"install.ps1", "start-interlock.ps1", "setup-database.ps1"}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_is_plain_ascii(script: Path) -> None:
    offending = [i for i, byte in enumerate(script.read_bytes()) if byte > 127]

    assert not offending, f"{script.name} has non-ASCII bytes (first at offset {offending[0]})"


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="needs Windows PowerShell")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_parses(script: Path) -> None:
    parser = "[System.Management.Automation.Language.Parser]"
    command = (
        "$e = $null; $t = $null; "
        f"[void]{parser}::ParseFile('{script}', [ref]$t, [ref]$e); "
        "$e | ForEach-Object { $_.Message }; if ($e) { exit 1 }"
    )

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{script.name} does not parse: {result.stdout.strip()}"


def test_the_installer_wrapper_passes_options_through() -> None:
    wrapper = (ROOT / "install.cmd").read_text(encoding="ascii")

    assert "install.ps1" in wrapper
    assert "%*" in wrapper
    assert "-ExecutionPolicy Bypass" in wrapper
