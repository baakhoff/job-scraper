"""Tests for the CSV / JSON export helpers (offline, pure functions)."""

from __future__ import annotations

import json

from src.export import rows_to_csv, rows_to_json

_ROWS: list[dict[str, object]] = [
    {"name": "Acme, Inc", "size": 200, "note": None},
    {"name": 'Globex "Labs"', "size": None, "note": "ok"},
]


def test_rows_to_csv_header_order_and_escaping() -> None:
    out = rows_to_csv(_ROWS, fields=["name", "size", "note"]).splitlines()
    assert out[0] == "name,size,note"
    # A value with a comma is quoted; None becomes an empty cell.
    assert out[1] == '"Acme, Inc",200,'
    # Embedded quotes are doubled per CSV rules.
    assert out[2] == '"Globex ""Labs""",,ok'


def test_rows_to_csv_infers_columns_when_unspecified() -> None:
    header = rows_to_csv(_ROWS).splitlines()[0]
    assert header == "name,size,note"


def test_rows_to_json_respects_field_projection() -> None:
    parsed = json.loads(rows_to_json(_ROWS, fields=["name"]))
    assert parsed == [{"name": "Acme, Inc"}, {"name": 'Globex "Labs"'}]
