"""CAS ingestion. MODULE_1.md §5.

    raw PDF -> pdf.decrypt_and_extract -> parse.parse_cas -> importer.import_cas

The split is deliberate: `pdf` is the only module that sees a password or a
file, `parse` is a pure function on lines, and `importer` turns staged rows into
ledger facts. A real CAS is Zone B personal data and cannot be committed, so
everything below `pdf` is tested against synthetic statements.
"""

from src.m1_ledger.cas.importer import (
    ImportReport,
    assign_sequences,
    import_cas,
    link_reversals,
    link_switch_groups,
)
from src.m1_ledger.cas.mapping import map_txn_type
from src.m1_ledger.cas.parse import (
    PARSER_VERSION,
    BalanceMarker,
    CasContext,
    CasParseError,
    CasState,
    StagedTxn,
    parse_cas,
    to_decimal,
)

__all__ = [
    "PARSER_VERSION",
    "BalanceMarker",
    "CasContext",
    "CasParseError",
    "CasState",
    "ImportReport",
    "StagedTxn",
    "assign_sequences",
    "import_cas",
    "link_reversals",
    "link_switch_groups",
    "map_txn_type",
    "parse_cas",
    "to_decimal",
]
