"""Serialize row dicts to CSV or JSON for download.

Small, dependency-free helpers (stdlib ``csv`` / ``json``) used by the web
export endpoints and the CLI. Callers pass a list of flat dicts; column order
is taken from an explicit ``fields`` list when given, else from the union of
keys across rows (stable, first-seen order).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Sequence


def _columns(rows: Sequence[dict[str, object]], fields: Sequence[str] | None) -> list[str]:
    """Resolve the column order: explicit ``fields`` or first-seen union of keys."""
    if fields is not None:
        return list(fields)
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def rows_to_csv(
    rows: Iterable[dict[str, object]], *, fields: Sequence[str] | None = None
) -> str:
    """Render rows as a CSV string (with header). ``None`` becomes an empty cell."""
    rows = list(rows)
    columns = _columns(rows, fields)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({col: _cell(row.get(col)) for col in columns})
    return buffer.getvalue()


def _cell(value: object) -> object:
    """Flatten a value for a CSV cell (None -> '', lists -> '; '-joined)."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return value


def rows_to_json(
    rows: Iterable[dict[str, object]], *, fields: Sequence[str] | None = None
) -> str:
    """Render rows as a pretty JSON array string."""
    rows = list(rows)
    if fields is not None:
        rows = [{col: row.get(col) for col in fields} for row in rows]
    return json.dumps(rows, indent=2, ensure_ascii=False, default=str)
