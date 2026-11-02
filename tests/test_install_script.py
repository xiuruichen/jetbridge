"""install.ps1 has to run on Windows PowerShell 5.1, including when piped from the web."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "install.ps1"

# Every tool looks missing, and winget is a stand-in, so nothing is really installed.
FAKES = """
$global:wingetCalls = @()
function Get-Command { param($Name, $ErrorAction) if ($Name -eq 'winget') { 'fake' } }
function Test-Path { param($Path) $false }
function Read-Host { param($Prompt) Write-Host "ASKED: $Prompt"; '%s' }
function winget { $global:wingetCalls += ($args -join ' '); $global:LASTEXITCODE = 0 }
"""


def run_powershell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )


def run_piped_with_missing_tools(answer: str) -> str:
    """Run the script the way the README says to, piped into iex."""
    result = run_powershell(
        FAKES % answer
        + f"Get-Content -Raw '{SCRIPT}' | Invoke-Expression; "
        + "Write-Host 'STILL OPEN'; "
        + '$global:wingetCalls | ForEach-Object { Write-Host "WINGET: $_" }'
    )
    return result.stdout + result.stderr


def test_script_is_ascii():
    # Windows PowerShell 5.1 misreads non-ASCII characters in a file with no byte order mark.
    SCRIPT.read_bytes().decode("ascii")


def test_script_parses_in_windows_powershell():
    result = run_powershell(
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPT}', [ref]$null, [ref]$errors); "
        "$errors | ForEach-Object { $_.Message }; exit $errors.Count"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_failing_never_closes_a_window_when_piped():
    text = SCRIPT.read_text(encoding="ascii")
    exits = [line.strip() for line in text.splitlines() if re.search(r"\bexit \d", line)]
    assert exits == ["if ($jetbridgeInstallFailed -and $PSCommandPath) { exit 1 }"]


@pytest.mark.parametrize("answer", ["n", ""])
def test_nothing_is_installed_without_a_yes(answer):
    output = run_piped_with_missing_tools(answer)
    assert "ASKED: Install with winget" in output
    assert "WINGET:" not in output
    assert "Nothing was installed" in output
    assert "STILL OPEN" in output


def test_a_yes_installs_only_the_three_tools():
    output = run_piped_with_missing_tools("y")
    assert re.findall(r"WINGET: install --id (\S+) -e", output) == [
        "Git.Git",
        "astral-sh.uv",
        "Google.Chrome",
    ]
    assert "STILL OPEN" in output
