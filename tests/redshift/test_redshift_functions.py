"""Integration tests for the Redshift date/time compatibility functions.

Requires the engine (docker compose up redshift). The test applies the shipped
`03_date_functions.sql` (idempotent CREATE OR REPLACE) so it is stable whether or
not the running image already bundles it, then asserts Redshift semantics.
"""

from pathlib import Path

import psycopg2
import pytest

RS_CONFIG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)
SQL = (
    Path(__file__).parents[2] / "oblako/images/redshift/initdb.d/03_date_functions.sql"
).read_text()


@pytest.fixture
def cursor():
    c = psycopg2.connect(**RS_CONFIG)
    c.autocommit = True
    cur = c.cursor()
    cur.execute(SQL)  # ensure the functions exist (CREATE OR REPLACE)
    yield cur
    c.close()


def _scalar(cursor, sql):
    cursor.execute(sql)
    return cursor.fetchone()[0]


def test_dateadd(cursor):
    assert (
        str(_scalar(cursor, "SELECT dateadd('day', 7, timestamp '2021-01-01')"))
        == "2021-01-08 00:00:00"
    )
    # month-end clamping, like Redshift
    assert (
        str(_scalar(cursor, "SELECT dateadd('month', 1, timestamp '2021-01-31')"))
        == "2021-02-28 00:00:00"
    )
    # quarter expands to 3 months
    assert (
        str(_scalar(cursor, "SELECT dateadd('quarter', 1, timestamp '2021-01-15')"))
        == "2021-04-15 00:00:00"
    )


def test_datediff_counts_boundaries(cursor):
    # boundary-crossing semantics, not elapsed time
    assert (
        _scalar(
            cursor,
            "SELECT datediff('year', timestamp '2020-12-31', timestamp '2021-01-01')",
        )
        == 1
    )
    assert (
        _scalar(
            cursor,
            "SELECT datediff('day', timestamp '2021-01-01', timestamp '2021-01-08')",
        )
        == 7
    )
    assert (
        _scalar(
            cursor,
            "SELECT datediff('month', timestamp '2021-01-31', timestamp '2021-03-01')",
        )
        == 2
    )
    assert (
        _scalar(
            cursor,
            "SELECT datediff('hour', timestamp '2021-01-01 22:59', timestamp '2021-01-01 23:01')",
        )
        == 1
    )


def test_last_day_and_add_months(cursor):
    assert (
        str(_scalar(cursor, "SELECT last_day(timestamp '2021-02-10')")) == "2021-02-28"
    )
    assert (
        str(_scalar(cursor, "SELECT add_months(timestamp '2021-01-31', 1)"))
        == "2021-02-28 00:00:00"
    )


def test_months_between(cursor):
    assert (
        _scalar(
            cursor,
            "SELECT months_between(timestamp '2021-03-15', timestamp '2021-01-15')",
        )
        == 2.0
    )


def test_trunc_and_convert_timezone(cursor):
    assert (
        str(_scalar(cursor, "SELECT trunc(timestamp '2021-06-19 14:30')"))
        == "2021-06-19"
    )
    # noon UTC -> 07:00 EST
    assert (
        str(
            _scalar(
                cursor,
                "SELECT convert_timezone('UTC','America/New_York', timestamp '2021-01-01 12:00')",
            )
        )
        == "2021-01-01 07:00:00"
    )


def test_getdate_and_abbreviations(cursor):
    assert _scalar(cursor, "SELECT getdate() IS NOT NULL") is True
    # abbreviations: 'mon' = month, 'm' = minute (Redshift)
    assert (
        _scalar(
            cursor,
            "SELECT datediff('mon', timestamp '2021-01-01', timestamp '2021-04-01')",
        )
        == 3
    )
    assert (
        _scalar(
            cursor,
            "SELECT datediff('m', timestamp '2021-01-01 10:00', timestamp '2021-01-01 10:05')",
        )
        == 5
    )


def _bare_datepart_supported() -> bool:
    """True if the proxy quotes bare dateparts (skips on a pre-rewrite image)."""
    try:
        c = psycopg2.connect(connect_timeout=3, **RS_CONFIG)
        try:
            c.cursor().execute("SELECT dateadd(day, 1, timestamp '2021-01-01')")
            return True
        finally:
            c.close()
    except Exception:
        return False


@pytest.mark.skipif(
    not _bare_datepart_supported(), reason="redshift image without datepart rewrite"
)
def test_bare_datepart_keywords(cursor):
    # Redshift accepts the datepart unquoted; the wire proxy quotes it
    assert (
        str(_scalar(cursor, "SELECT dateadd(month, 1, timestamp '2021-01-31')"))
        == "2021-02-28 00:00:00"
    )
    assert (
        _scalar(
            cursor,
            "SELECT datediff(day, timestamp '2021-01-01', timestamp '2021-03-01')",
        )
        == 59
    )
    assert _scalar(cursor, "SELECT date_part(dow, timestamp '2026-09-30')") == 3
