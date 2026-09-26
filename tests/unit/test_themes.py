"""Three themes, one set of tokens. DECISIONS V1-81.

A token the light theme defines and the dark or Matrix theme forgets renders the
light colour on a dark ground: unreadable, and nothing else would notice.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "src" / "m6_views" / "static" / "app.css")
#: Shape and type, the same in every theme.
SHARED = {"--radius", "--radius-sm", "--font", "--mono"}


def _tokens(selector: str) -> set[str]:
    css = CSS.read_text(encoding="utf-8")
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert block, f"no {selector} block"
    return set(re.findall(r"^\s*(--[\w-]+):", block.group(1), re.M))


def test_every_theme_defines_every_colour() -> None:
    light = _tokens(":root") - SHARED
    for theme in ("dark", "matrix"):
        assert _tokens(f':root[data-theme="{theme}"]') >= light, theme
