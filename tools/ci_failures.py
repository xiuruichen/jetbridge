"""Turn failed tests into GitHub annotations, which can be read without signing in.

uv run python tools/ci_failures.py results.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

MAX_SHOWN = 40
MAX_MESSAGE = 500


def annotations(path: str) -> list[str]:
    lines = []
    for case in ET.parse(path).getroot().iter("testcase"):
        for problem in (*case.findall("failure"), *case.findall("error")):
            message = " ".join((problem.get("message") or "").split())[:MAX_MESSAGE]
            lines.append(f"::error title={case.get('name')}::{case.get('classname')}: {message}")
    return lines[:MAX_SHOWN]


if __name__ == "__main__":
    print("\n".join(annotations(sys.argv[1])))
