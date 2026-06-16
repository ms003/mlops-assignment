"""Schema-rendering helper (provided complete).

Loads the schema directly from sqlite and renders quoted CREATE TABLE
text suitable for prompt context. Identifiers are always double-quoted
so reserved-word table/column names (e.g. `order`) don't break either
the PRAGMA introspection here or the SQL the model emits later.
"""
from __future__ import annotations

import csv
import sqlite3
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "data" / "bird"


def db_path(db_id: str) -> Path:
    return DB_DIR / f"{db_id}.sqlite"


def _q(ident: str | None) -> str:
    """Double-quote a SQL identifier, escaping any embedded quotes."""
    if ident is None:
        ident = ""
    return '"' + ident.replace('"', '""') + '"'


def _description_dir(db_id: str) -> Path:
    """Return BIRD's optional column dictionary folder for a database."""
    return DB_DIR / "dev_20240627" / "dev_databases" / db_id / "database_description"


def _read_description_rows(csv_path: Path) -> list[dict[str, str]]:
    """Read BIRD dictionary CSV rows without failing on mixed encodings."""
    encodings = ("utf-8-sig", "cp1252", "latin-1")
    for encoding in encodings:
        try:
            with csv_path.open(newline="", encoding=encoding) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue

    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        return list(csv.DictReader(f))


def _render_table_dictionary(db_id: str, table: str) -> str:
    """Render optional BIRD column/value descriptions for one table."""
    csv_path = _description_dir(db_id) / f"{table}.csv"
    if not csv_path.exists():
        return ""

    lines: list[str] = [f"-- Column dictionary for {_q(table)}"]
    for row in _read_description_rows(csv_path):
        original = (row.get("original_column_name") or "").strip()
        if not original:
            continue
        friendly = (row.get("column_name") or "").strip()
        description = (row.get("column_description") or "").strip()
        value_description = (row.get("value_description") or "").strip()

        details: list[str] = []
        if friendly and friendly != original:
            details.append(f"name: {friendly}")
        if description and description != friendly:
            details.append(f"description: {description}")
        if value_description:
            details.append(f"values: {value_description}")

        if details:
            lines.append(f"--   {_q(original)} -> {'; '.join(details)}")

    return "\n".join(lines) if len(lines) > 1 else ""


@lru_cache(maxsize=32)
def render_schema(db_id: str) -> str:
    path = db_path(db_id)
    if not path.exists():
        raise FileNotFoundError(f"DB {db_id} not found at {path}. Did you run scripts/load_data.py?")

    parts: list[str] = [f"-- Database: {db_id}"]
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        for t in tables:
            parts.append(f"\nCREATE TABLE {_q(t)} (")
            col_lines: list[str] = []
            for _cid, name, ctype, notnull, _dflt, pk in conn.execute(f"PRAGMA table_info({_q(t)})"):
                line = f"  {_q(name)} {ctype}"
                if pk:
                    line += " PRIMARY KEY"
                if notnull and not pk:
                    line += " NOT NULL"
                col_lines.append(line)
            for fk in conn.execute(f"PRAGMA foreign_key_list({_q(t)})"):
                # (id, seq, ref_table, from, to, on_update, on_delete, match)
                col_lines.append(
                    f"  FOREIGN KEY ({_q(fk[3])}) REFERENCES {_q(fk[2])}({_q(fk[4])})"
                )
            parts.append(",\n".join(col_lines))
            parts.append(");")
            dictionary = _render_table_dictionary(db_id, t)
            if dictionary:
                parts.append(dictionary)
    return "\n".join(parts)


def available_dbs() -> list[str]:
    if not DB_DIR.exists():
        return []
    return sorted(p.stem for p in DB_DIR.glob("*.sqlite"))
