"""install.ps1 has to run on Windows PowerShell 5.1, including when piped from the web."""

from __future__ import annotations

import re
import subprocess
import uuid
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


# Loads the script's functions without running the install.
LOAD_FUNCTIONS = (
    "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
    f"'{SCRIPT}', [ref]$null, [ref]$null); "
    "$ast.FindAll({ $args[0] -is "
    "[System.Management.Automation.Language.FunctionDefinitionAst] }, $false) | "
    "ForEach-Object { Invoke-Expression $_.Extent.Text }; "
)


def run_functions(command: str) -> str:
    result = run_powershell(LOAD_FUNCTIONS + command)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("", "C:\\bin"),
        ("C:\\a", "C:\\a;C:\\bin"),
        ("C:\\a;", "C:\\a;C:\\bin"),
        ("C:\\a;c:\\BIN\\", "C:\\a;c:\\BIN\\"),
        ("%USERPROFILE%\\x", "%USERPROFILE%\\x;C:\\bin"),
    ],
)
def test_path_gets_the_folder_once(current, expected):
    output = run_functions(f"Add-FolderToPathText '{current}' 'C:\\bin'")
    assert output == expected


def test_user_path_keeps_variables_unexpanded_and_adds_once():
    # A scratch registry key stands in for the real PATH.
    name = f"Software\\jetbridge-test-{uuid.uuid4().hex}"
    output = run_functions(
        f"$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('{name}'); "
        "$key.SetValue('Path', '%USERPROFILE%\\x', "
        "[Microsoft.Win32.RegistryValueKind]::ExpandString); $key.Close(); "
        f"Add-FolderToUserPath 'C:\\jb' '{name}'; Add-FolderToUserPath 'C:\\jb' '{name}'; "
        f"$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('{name}'); "
        "$key.GetValue('Path', '', "
        "[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames); "
        "$key.GetValueKind('Path'); $key.Close(); "
        f"[Microsoft.Win32.Registry]::CurrentUser.DeleteSubKey('{name}')"
    )
    assert output.splitlines() == ["%USERPROFILE%\\x;C:\\jb", "ExpandString"]


def test_launcher_passes_arguments_to_the_program(tmp_path):
    program = tmp_path / "with space" / "show.cmd"
    program.parent.mkdir()
    program.write_text("@echo ARGS %*\r\n", encoding="ascii")
    folder = tmp_path / "bin"
    run_functions(f"Write-Launcher '{program}' '{folder}'")
    result = subprocess.run(
        ["cmd", "/c", str(folder / "jetbridge.cmd"), "watch", "list"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "ARGS -m jetbridge watch list"


def test_environment_folder_follows_the_synced_folder_setting():
    assert (
        run_functions("$env:UV_PROJECT_ENVIRONMENT = $null; Get-EnvironmentFolder 'C:\\jb'")
        == "C:\\jb\\.venv"
    )
    assert (
        run_functions("$env:UV_PROJECT_ENVIRONMENT = 'D:\\venv'; Get-EnvironmentFolder 'C:\\jb'")
        == "D:\\venv"
    )
