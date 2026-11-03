"""The README and guide are the front door, so their links and pictures have to work."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PAGES = [
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "guide.md",
    ROOT / "docs" / "mac-testing.md",
]
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
_HTML_LINK = re.compile(r'(?:href|src)="([^"]+)"')
_CODE = re.compile(r"```.*?```", re.DOTALL)


def _anchors(path: Path) -> set[str]:
    text = _CODE.sub("", path.read_text(encoding="utf-8"))
    titles = re.findall(r"^#{1,6} (.+)$", text, flags=re.M)
    return {re.sub(r"[^\w\- ]", "", t.replace("`", "").lower()).replace(" ", "-") for t in titles}


def _links(path: Path) -> list[str]:
    text = _CODE.sub("", path.read_text(encoding="utf-8"))
    return _MARKDOWN_LINK.findall(text) + _HTML_LINK.findall(text)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_links_and_pictures_on_this_site_lead_somewhere(page):
    broken = []
    for link in _links(page):
        if link.startswith(("http://", "https://", "mailto:")):
            continue
        target, _, fragment = link.partition("#")
        path = (page.parent / target).resolve() if target else page
        if not path.exists() or (
            fragment and path.suffix == ".md" and fragment not in _anchors(path)
        ):
            broken.append(link)
    assert broken == []


@pytest.mark.parametrize("name", ["banner.svg", "board.svg", "alert.svg", "chat-demo.svg"])
def test_pictures_are_labeled_and_have_only_made_up_details(name):
    text = (ROOT / "docs" / name).read_text(encoding="utf-8")
    assert "<title" in text and "<desc" in text
    assert not re.findall(r"[\w.+-]+@(?!example\.com)[\w-]+\.\w+", text)


def test_the_front_page_is_honest_about_the_big_limits():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "likely goes against the airlines' terms" in text
    assert "hasn't been tried on a real Mac" in text
    assert "Don't rely on it as your only way" in text
