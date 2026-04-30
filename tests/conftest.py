"""Shared pytest fixtures for the SQL harness and Python tests.

The SQL harness loads the canonical BigQuery `.sql` files, transpiles them to
DuckDB via `sqlglot`, and runs them against in-memory DuckDB tables seeded
from the CSV fixtures in `tests/fixtures/`.

This lets reviewers run the full test suite offline, with no GCP credentials,
while keeping a single canonical SQL artifact per query.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import duckdb
import pytest
import sqlglot

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"

DEFAULT_RUN_DATE = date(2025, 4, 29)


# ---------------------------------------------------------------------------
# SQL rendering
# ---------------------------------------------------------------------------

_DECLARE_RE = re.compile(
    r"^\s*DECLARE\s+\w+\s+\w+\s+DEFAULT\s+[^;]+;\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# BigQuery -> DuckDB function rewrites that sqlglot does not handle cleanly.
_TIMESTAMP_SUB_RE = re.compile(
    r"TIMESTAMP_SUB\(\s*([^,]+?)\s*,\s*INTERVAL\s+(\d+)\s+(\w+)\s*\)",
    re.IGNORECASE,
)
_TIMESTAMP_DIFF_RE = re.compile(
    r"TIMESTAMP_DIFF\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*(\w+)\s*\)",
    re.IGNORECASE,
)
_TIMESTAMP_CAST_RE = re.compile(r"TIMESTAMP\(\s*([^)]+?)\s*\)")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def render_sql_for_duckdb(
    sql_path: Path,
    *,
    run_date: date,
    extra_params: dict[str, str] | None = None,
) -> str:
    """Render a BigQuery SQL file as an executable DuckDB statement.

    The transformation:
      1. Strips BigQuery scripting `DECLARE ... DEFAULT @x;` lines.
      2. Replaces the `run_date` and `run_ts` identifiers (declared in the
         stripped block) with literal DATE / TIMESTAMP values.
      3. Replaces any remaining `@param` placeholders with values supplied via
         ``extra_params``.
      4. Rewrites the BigQuery functions used by our SQL into their DuckDB
         equivalents (``TIMESTAMP_SUB`` → interval subtraction,
         ``TIMESTAMP_DIFF`` → ``date_diff``, ``TIMESTAMP(date)`` →
         ``CAST(date AS TIMESTAMP)``) and strips backtick identifiers.

    The result is a single statement DuckDB can execute directly.
    """
    raw = sql_path.read_text()
    iso = run_date.isoformat()

    rendered = _DECLARE_RE.sub("", raw)
    rendered = re.sub(r"\brun_ts\b", f"TIMESTAMP '{iso} 00:00:00'", rendered)
    rendered = re.sub(r"\brun_date\b", f"DATE '{iso}'", rendered)

    # Extra params are substituted both as ``@name`` placeholders (BigQuery
    # script parameter syntax) and as bare identifiers (the names declared in
    # the script's DECLARE block, e.g. ``threshold``).
    for name, literal in (extra_params or {}).items():
        rendered = rendered.replace(f"@{name}", literal)
        rendered = re.sub(rf"\b{re.escape(name)}\b", literal, rendered)

    rendered = _TIMESTAMP_SUB_RE.sub(r"(\1 - INTERVAL \2 \3)", rendered)
    rendered = _TIMESTAMP_DIFF_RE.sub(
        lambda m: f"date_diff('{m.group(3).lower()}', {m.group(2)}, {m.group(1)})",
        rendered,
    )
    rendered = _TIMESTAMP_CAST_RE.sub(r"CAST(\1 AS TIMESTAMP)", rendered)
    return _BACKTICK_RE.sub(r"\1", rendered)


# sqlglot is kept as a dev dependency for reviewers who want to experiment with
# alternative renderings. We import it lazily so that test collection does not
# pay the import cost when only the regex pipeline above is used.
def transpile_with_sqlglot(sql: str) -> str:  # pragma: no cover - convenience
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


# ---------------------------------------------------------------------------
# DuckDB session
# ---------------------------------------------------------------------------

_TABLE_COLUMNS = {
    "renter_activity": [
        "renter_id",
        "event_type",
        "event_timestamp",
        "property_id",
        "channel",
        "utm_source",
    ],
    "renter_profiles": [
        "renter_id",
        "email",
        "phone",
        "last_login",
        "subscription_status",
        "sms_consent",
        "email_consent",
        "dnd_until",
        "created_at",
    ],
    "suppression_list": ["renter_id", "suppression_reason", "suppressed_at"],
    "renter_send_scores": [
        "renter_id",
        "predicted_conversion_probability",
        "model_version",
        "scored_at",
    ],
}

# DuckDB CREATE TABLE statements that mirror the BigQuery schemas.
_DDL = {
    "renter_activity": """
        CREATE TABLE renter_activity (
            renter_id        VARCHAR,
            event_type       VARCHAR,
            event_timestamp  TIMESTAMP,
            property_id      VARCHAR,
            channel          VARCHAR,
            utm_source       VARCHAR
        )
    """,
    "renter_profiles": """
        CREATE TABLE renter_profiles (
            renter_id            VARCHAR,
            email                VARCHAR,
            phone                VARCHAR,
            last_login           TIMESTAMP,
            subscription_status  VARCHAR,
            sms_consent          BOOLEAN,
            email_consent        BOOLEAN,
            dnd_until            TIMESTAMP,
            created_at           TIMESTAMP
        )
    """,
    "suppression_list": """
        CREATE TABLE suppression_list (
            renter_id           VARCHAR,
            suppression_reason  VARCHAR,
            suppressed_at       TIMESTAMP
        )
    """,
    # The model score table lives in its own logical dataset in BigQuery
    # (`ml_predictions.renter_send_scores`). We mirror that with a DuckDB
    # schema so the SQL can reference it as written.
    "renter_send_scores": """
        CREATE SCHEMA IF NOT EXISTS ml_predictions;
        CREATE TABLE ml_predictions.renter_send_scores (
            renter_id                          VARCHAR,
            predicted_conversion_probability   DOUBLE,
            model_version                      VARCHAR,
            scored_at                          TIMESTAMP
        )
    """,
}

# Tables that live in a non-default schema; the helpers below transparently
# qualify the table name on insert.
_SCHEMA_FOR = {
    "renter_send_scores": "ml_predictions",
}


@pytest.fixture
def duck() -> Iterator[duckdb.DuckDBPyConnection]:
    """Fresh in-memory DuckDB connection seeded with empty fixture tables.

    Tests can call ``load_fixture(duck, name)`` or insert rows directly
    via ``duck.execute("INSERT INTO ...")``.
    """
    con = duckdb.connect(":memory:")
    for ddl in _DDL.values():
        # DDL blocks may contain a CREATE SCHEMA + CREATE TABLE pair separated
        # by ``;``. Split and execute statements one by one for portability.
        for stmt in filter(None, (s.strip() for s in ddl.split(";"))):
            con.execute(stmt)
    try:
        yield con
    finally:
        con.close()


def insert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: list[dict[str, object]],
) -> None:
    """Insert dict rows into a fixture table with explicit NULL handling.

    Missing keys default to NULL, so each test only writes the fields it cares
    about and the rest fall back to neutral defaults.
    """
    if table not in _TABLE_COLUMNS:
        raise KeyError(f"unknown fixture table: {table}")
    cols = _TABLE_COLUMNS[table]
    qualified = f"{_SCHEMA_FOR[table]}.{table}" if table in _SCHEMA_FOR else table
    placeholders = ", ".join(["?"] * len(cols))
    sql = f"INSERT INTO {qualified} ({', '.join(cols)}) VALUES ({placeholders})"
    for row in rows:
        con.execute(sql, [row.get(c) for c in cols])


@pytest.fixture
def insert(duck: duckdb.DuckDBPyConnection):  # type: ignore[no-untyped-def]
    """Convenience fixture returning a callable bound to the active DuckDB."""

    def _insert(table: str, rows: list[dict[str, object]]) -> None:
        insert_rows(duck, table, rows)

    return _insert
