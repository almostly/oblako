"""Integration tests: AVG of an integer column returns BIGINT, as on Redshift.

Requires the engine (docker compose up redshift) built from this repo's image: the
wire proxy rewrites avg( to pg_oblako.avg( (proxy/integer_avg.py), whose
overloads return BIGINT for integers and mirror PostgreSQL's avg otherwise
(initdb.d/11_integer_avg.sql).
"""

import psycopg2
import pytest

RS_CONFIG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)


@pytest.fixture
def cursor():
    c = psycopg2.connect(**RS_CONFIG)
    c.autocommit = True
    yield c.cursor()
    c.close()


def _row(cursor, sql):
    cursor.execute(sql)
    return cursor.fetchone()


@pytest.mark.parametrize("cast", ["smallint", "integer", "bigint"])
def test_integer_avg_is_bigint_and_truncates(cursor, cast):
    value, kind = _row(
        cursor,
        f"SELECT avg(x::{cast}), pg_typeof(avg(x::{cast}))::text"
        " FROM (VALUES (1), (2), (4)) t(x)",
    )
    assert (value, kind) == (2, "bigint")  # 7 / 3 = 2.33..., truncated


def test_negative_average_truncates_toward_zero(cursor):
    assert _row(cursor, "SELECT avg(x) FROM (VALUES (-1), (-2)) t(x)") == (-1,)


def test_empty_and_null_inputs(cursor):
    assert _row(cursor, "SELECT avg(x) FROM (SELECT NULL::int AS x) t") == (None,)
    assert _row(cursor, "SELECT avg(x) FROM (VALUES (3), (NULL)) t(x)") == (3,)


def test_bigint_sum_does_not_overflow(cursor):
    sql = (
        "SELECT avg(x) FROM (VALUES (9223372036854775807::bigint),"
        " (9223372036854775805::bigint)) t(x)"
    )
    assert _row(cursor, sql) == (9223372036854775806,)


def test_other_types_keep_their_avg(cursor):
    kinds = _row(
        cursor,
        "SELECT pg_typeof(avg(x::float))::text, pg_typeof(avg(x::numeric))::text"
        " FROM (VALUES (1), (2)) t(x)",
    )
    assert kinds == ("double precision", "numeric")


def test_other_types_match_postgres_values(cursor):
    assert _row(cursor, "SELECT avg(x::numeric) FROM (VALUES (1), (2)) t(x)")[0] == 1.5
    assert _row(cursor, "SELECT avg(x::float) FROM (VALUES (1), (2)) t(x)") == (1.5,)


def test_unqualified_create_still_lands_in_public(cursor):
    cursor.execute("DROP TABLE IF EXISTS avg_probe")
    cursor.execute("CREATE TABLE avg_probe (n int)")
    schema = _row(
        cursor,
        "SELECT schemaname FROM pg_tables WHERE tablename = 'avg_probe'",
    )
    cursor.execute("DROP TABLE avg_probe")
    assert schema == ("public",)
