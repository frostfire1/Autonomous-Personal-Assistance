"""Hash chain for explainability_log rows (tamper evidence)."""

import hashlib
import json
from typing import Any

GENESIS = "0" * 64


def canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def compute_row_hash(prev_hash: str | None, row: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash or GENESIS}|{canonical(row)}".encode()).hexdigest()


def verify_chain(rows: list[tuple[str | None, dict[str, Any], str]]) -> int | None:
    """rows: ordered (prev_hash, content, row_hash). Returns index of first bad row, or None."""
    expected_prev: str | None = None
    for i, (prev_hash, content, row_hash) in enumerate(rows):
        if (prev_hash or None) != expected_prev:
            return i
        if compute_row_hash(prev_hash, content) != row_hash:
            return i
        expected_prev = row_hash
    return None
