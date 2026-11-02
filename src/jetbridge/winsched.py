"""Windows Task Scheduler integration.

The task runs only while the user is logged on, because Chrome needs a desktop session.
It catches up after sleep, runs on battery, and uses pythonw so no console window appears.
"""

from __future__ import annotations

import getpass
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "jetbridge-tick"
INTERVAL = "PT15M"


def _python() -> Path:
    scripts = Path(sys.prefix) / "Scripts"
    windowless = scripts / "pythonw.exe"
    return windowless if windowless.exists() else scripts / "python.exe"


def task_xml(python: Path, user: str, start: datetime) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>jetbridge award seat checks</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>{INTERVAL}</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{start:%Y-%m-%dT%H:%M:%S}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>-m jetbridge tick</Arguments>
      <WorkingDirectory>{escape(str(Path.home()))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, errors="replace", check=False
    )


def install() -> None:
    xml = task_xml(_python(), getpass.getuser(), datetime.now())
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "task.xml"
        path.write_text(xml, encoding="utf-16")
        result = _run("/Create", "/TN", TASK_NAME, "/XML", str(path), "/F")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def remove() -> None:
    result = _run("/Delete", "/TN", TASK_NAME, "/F")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def is_installed() -> bool:
    return _run("/Query", "/TN", TASK_NAME).returncode == 0
