"""Name normalisation. MODULE_0.md §7.4.

Used for alias matching and manager dedup. Deterministic and **versioned**: if
this function changes, every `name_alias.alias_norm` in the warehouse is stale
and must be rebuilt. §7.4 says to treat that as a migration, and `NORM_VERSION`
below is what a migration would check.

The suffixes stripped are the ones that carry no information about which
company a name refers to. `Reliance Industries Ltd`, `Reliance Industries
Limited` and `Reliance Industries` are one issuer, and a disclosure will use
all three across a year.
"""

from __future__ import annotations

import re
import unicodedata

#: Bump when the algorithm changes. `name_alias` must then be rebuilt.
NORM_VERSION = "1"

#: §7.4 verbatim. `co` and `the` are deliberately included: "The Ramco Cements"
#: and "Ramco Cements" are the same issuer, and so are "Tata Steel Co" and
#: "Tata Steel".
SUFFIXES = re.compile(
    r"\b(ltd|limited|pvt|private|plc|inc|corp|corporation|co|company|the|and|&)\b"
)


def normalise_name(s: str) -> str:
    """Fold a raw instrument or issuer name to its comparable form.

    NFKD then ASCII-drop first: Indian disclosures carry curly quotes, en
    dashes and the occasional non-breaking space, and two names differing only
    by punctuation must not become two issuers.
    """
    text = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = SUFFIXES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()
