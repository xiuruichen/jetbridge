"""Remove what jetbridge put on this PC outside its own folder.

Each step reports what happened and a failed step doesn't stop the rest, so someone uninstalling
ends up with as little left behind as possible and knows about anything that is.
"""

from __future__ import annotations

import shutil
import winreg

import keyring

from jetbridge import claude_desktop, power, service, winsched
from jetbridge.notify import email as mail
from jetbridge.paths import home


def remove_from_user_path(folder: str, key_name: str = "Environment") -> bool:
    """Take one folder off the user PATH. Other entries keep their exact text."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_ALL_ACCESS) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return False
        wanted = folder.rstrip("\\").lower()
        entries = str(current).split(";")
        kept = [entry for entry in entries if entry.rstrip("\\").lower() != wanted]
        if len(kept) == len(entries):
            return False
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(kept))
        return True


def remove_everything() -> list[str]:
    report: list[str] = []
    state = home()
    launcher = state / "bin"

    if winsched.is_installed():
        try:
            winsched.remove()
            report.append("Stopped automatic checks.")
        except RuntimeError as exc:
            report.append(f"Couldn't remove the scheduled task, so remove 'jetbridge-tick': {exc}")

    try:
        store = service.open_store()
        try:
            if power.restore(store):
                report.append("Put your PC's sleep settings back as they were.")
        finally:
            store.close()
    except OSError as exc:
        report.append(f"Couldn't put your PC's sleep settings back: {exc}")

    try:
        if claude_desktop.disconnect():
            report.append("Disconnected Claude Desktop. Restart it to finish.")
    except claude_desktop.ConfigProblem as exc:
        report.append(f"Couldn't disconnect Claude Desktop: {exc}")

    try:
        config = mail.load_config()
    except ValueError:
        config = None
    if config:
        try:
            keyring.delete_password(mail.KEYRING_SERVICE, config.from_address)
            report.append("Deleted the saved Gmail password.")
        except keyring.errors.PasswordDeleteError:
            pass
        report.append(
            f"Also revoke the app password for {config.from_address} at "
            "https://myaccount.google.com/apppasswords."
        )

    try:
        if remove_from_user_path(str(launcher)):
            report.append("Removed the jetbridge command.")
    except OSError as exc:
        report.append(f"Couldn't update your PATH, so remove {launcher} from it: {exc}")

    shutil.rmtree(state, ignore_errors=True)
    if state.exists():
        report.append(f"Part of {state} was in use. Close jetbridge and Chrome, then delete it.")
    else:
        report.append(f"Deleted {state}.")
    report.append("Last step: delete the jetbridge project folder, usually in your home folder.")
    return report
