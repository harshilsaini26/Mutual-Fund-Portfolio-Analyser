"""The React islands and the page they enhance. DECISIONS V1-80.

The islands bundle is built once from `ui/src` and committed, so CI needs no
Node. What stops it going stale: its first line records the SHA-256 of the
sources it was built from, computed exactly as `ui/build.mjs` computes it, and
this test recomputes it. What keeps the pages honest without it: every island
sits on an element that already carries its text, and nothing styles a page
through markup the CSP would refuse.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "ui" / "src"
BUNDLE = ROOT / "src" / "m6_views" / "static" / "vendor" / "islands.v1.js"
TEMPLATES = ROOT / "src" / "m6_views" / "templates"


def _digest(folder: Path) -> str:
    """ui/build.mjs's `sourceDigest`: sorted relative paths, each followed by
    its text with line endings normalised."""
    digest = hashlib.sha256()
    files = sorted(
        p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()
    )
    for path in files:
        text = (folder / path).read_bytes().decode("utf-8").replace("\r\n", "\n")
        digest.update(f"{path}\n".encode())
        digest.update(text.encode())
    return digest.hexdigest()


def test_the_islands_bundle_was_built_from_the_sources_in_the_tree() -> None:
    first = BUNDLE.read_text(encoding="utf-8").split("\n", 1)[0]
    recorded = re.search(r"islands source sha256=([0-9a-f]{64})", first)
    assert recorded, "islands.v1.js has no source hash: `cd ui && npm run build`"
    assert recorded.group(1) == _digest(SOURCES), (
        "ui/src changed since islands.v1.js was built: `cd ui && npm ci && npm run build`"
    )


def test_every_island_sits_on_text_the_server_already_wrote() -> None:
    """With scripts off, or reduced motion asked for, an island does nothing:
    the element has to read correctly as it is."""
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        islands = r'<(\w+)[^>]*data-island="([\w-]+)"[^>]*>(.*?)</\1>'
        for match in re.finditer(islands, text, re.S):
            kind, inner = match.group(2), match.group(3).strip()
            if kind in ("blur-text", "count-up"):
                assert inner, f"{template.name}: an empty {kind} island"


def test_no_template_styles_through_markup() -> None:
    """The public copy's CSP is `style-src 'self'`: a `style=` attribute would be
    refused and the page would render wrong where it is published."""
    for template in TEMPLATES.rglob("*.html"):
        assert " style=" not in template.read_text(encoding="utf-8"), template.name
