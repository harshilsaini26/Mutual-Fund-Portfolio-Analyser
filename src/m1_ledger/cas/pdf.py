"""The only place a password or a PDF is touched. MODULE_1.md §5.2.

Everything else in this package works on `list[str]`, which is what makes the
parser testable: a real CAS carries a PAN, folio numbers and a postal address,
so it is Zone B and can never be committed to this repository. The tests run
against synthetic statements in the same layout.

`pypdf` and `pdfplumber` are imported inside the functions, not at module
scope. Neither is needed to parse text, and importing them eagerly would make
the whole ledger unimportable on a machine that has not installed them — for a
capability most test runs never use.
"""

from __future__ import annotations

import hashlib
from typing import Any


class CasDecryptError(ValueError):
    """Wrong password, or not an encrypted CAS."""


def file_id(pdf_bytes: bytes) -> str:
    """Content hash of the source file. MODULE_1.md §5.7.

    Every transaction points back at the file it came from, so `PLAN.md` §4.2
    ("every number traces to a row in an archived source file") holds for the
    ledger. Hashing the bytes rather than naming the file means a re-download
    of the same statement is recognised as the same statement.
    """
    return hashlib.sha256(pdf_bytes).hexdigest()


def decrypt_and_extract(pdf_bytes: bytes, password: str) -> list[str]:
    """Decrypt, then extract text with layout preserved.

    `layout=True` is not optional. CAS columns are positional — amount, units,
    NAV and unit balance are distinguished by where they sit on the line, and
    without layout preservation `pdfplumber` returns them in reading order with
    the whitespace collapsed, which destroys the only thing that separates a
    NAV from a unit count.

    The password is a parameter and is never written anywhere. §5.4: "From user
    input at import time. Never stored."
    """
    import io

    import pdfplumber
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted and reader.decrypt(password) == 0:
        raise CasDecryptError("password rejected by the PDF")

    buffer = io.BytesIO()
    writer = pypdf.PdfWriter()
    # Named apart from the `page` below on purpose: `pypdf.PageObject` and
    # `pdfplumber.page.Page` are different types from different libraries, and
    # reusing one name for both is a type error the moment the `cas` extra is
    # actually installed — which CI never does.
    for source_page in reader.pages:
        writer.add_page(source_page)
    writer.write(buffer)
    buffer.seek(0)

    lines: list[str] = []
    with pdfplumber.open(buffer) as doc:
        for page in doc.pages:
            text: Any = page.extract_text(layout=True) or ""
            lines.extend(str(text).splitlines())
    return lines
