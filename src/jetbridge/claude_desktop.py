"""Connect the jetbridge tools to the Claude Desktop app by editing its config file.

The file also holds the person's other servers and settings, so only the jetbridge entry ever
changes, a file that can't be understood is left alone, and a copy is kept before each change.
"""

from __future__ import annotations

import json
import os
import shutil
from enum import Enum
from pathlib import Path
from typing import Any

from jetbridge import platforms

_MAC_APP = Path("/Applications/Claude.app")
SERVER_NAME = "jetbridge"
CONFIG_NAME = "claude_desktop_config.json"


class State(Enum):
    NOT_INSTALLED = "not installed"
    NOT_CONNECTED = "not connected"
    CONNECTED = "connected"
    OUT_OF_DATE = "connected, but pointing at an old copy of jetbridge"
    UNREADABLE = "its config file couldn't be read"


class ConfigProblem(Exception):
    """A config file can't be changed safely. Nothing was written."""


def config_files() -> list[Path]:
    """Where this computer's Claude Desktop keeps its config. Empty if the app isn't installed.

    The Microsoft Store build reads a copy inside its package folder, and its Edit Config button
    has been reported to open the other file, so both are kept the same when both exist.
    """
    if platforms.IS_MAC:
        folder = Path.home() / "Library" / "Application Support" / "Claude"
        return [folder / CONFIG_NAME] if folder.is_dir() or _MAC_APP.exists() else []
    files: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    roaming = os.environ.get("APPDATA")
    if local:
        packages = Path(local) / "Packages"
        if packages.is_dir():
            for package in sorted(packages.glob("Claude_*")):
                files.append(package / "LocalCache" / "Roaming" / "Claude" / CONFIG_NAME)
    if roaming:
        folder = Path(roaming) / "Claude"
        if folder.is_dir() or (local and (Path(local) / "AnthropicClaude").is_dir()):
            files.append(folder / CONFIG_NAME)
    return files


def server_program() -> Path:
    """The full path, because the app starts servers without this folder or uv to go on. It's
    python rather than jetbridge-mcp.exe because the app keeps the program open, and an update can't
    replace that .exe while it's in use."""
    return platforms.environment_python()


def server_entry() -> dict[str, Any]:
    entry: dict[str, Any] = {
        "command": str(server_program()),
        "args": ["-m", "jetbridge.mcp_server"],
    }
    state_folder = os.environ.get("JETBRIDGE_HOME")
    if state_folder:
        entry["env"] = {"JETBRIDGE_HOME": state_folder}
    return entry


def _read(path: Path) -> dict[str, Any]:
    """The config's contents. Raises ``ConfigProblem`` rather than guess at a damaged file."""
    try:
        # PowerShell and Notepad can both leave a byte order mark.
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise ConfigProblem(f"couldn't read {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigProblem(
            f"{path} isn't valid JSON (line {exc.lineno}), so I left it alone. Fix or delete it, "
            "then try again"
        ) from exc
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
        raise ConfigProblem(f"{path} isn't laid out as expected, so I left it alone")
    return config


def _write(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".jetbridge-backup"))
    temporary = path.with_name(path.name + ".jetbridge-tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def status() -> State:
    files = config_files()
    if not files:
        return State.NOT_INSTALLED
    try:
        entries = [_read(path).get("mcpServers", {}).get(SERVER_NAME) for path in files]
    except ConfigProblem:
        return State.UNREADABLE
    expected = server_entry()
    if all(entry == expected for entry in entries):
        return State.CONNECTED if server_program().exists() else State.OUT_OF_DATE
    return State.OUT_OF_DATE if any(entries) else State.NOT_CONNECTED


def connect() -> bool:
    """Add or update the jetbridge entry. True if a file changed. Requires the app installed."""
    files = config_files()
    if not files:
        raise ConfigProblem("Claude Desktop isn't installed on this computer")
    if not server_program().exists():
        raise ConfigProblem(f"{server_program()} is missing. Run 'uv sync' and try again")
    # Read them all first, so a damaged file means nothing changes anywhere.
    configs = [(path, _read(path)) for path in files]
    entry = server_entry()
    changed = False
    for path, config in configs:
        servers = config.setdefault("mcpServers", {})
        if servers.get(SERVER_NAME) != entry:
            servers[SERVER_NAME] = entry
            _write(path, config)
            changed = True
    return changed


def disconnect() -> bool:
    """Remove the jetbridge entry and nothing else. True if a file changed."""
    configs = [(path, _read(path)) for path in config_files()]
    changed = False
    for path, config in configs:
        servers = config.get("mcpServers", {})
        if SERVER_NAME in servers:
            del servers[SERVER_NAME]
            _write(path, config)
            changed = True
    return changed
