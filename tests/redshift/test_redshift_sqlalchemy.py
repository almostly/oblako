"""Integration test: the sqlalchemy-redshift dialect reflects against the engine.

Requires the engine (docker compose up redshift). The dialect's reflection reads
Redshift-only pg_catalog columns (reldiststyle, attencodingtype, attisdistkey,
attsortkeyord) and Spectrum svv_* views that stock PostgreSQL lacks; the proxy
answers the columns with neutral literals and the image ships the svv_* views, so
get_table_names / get_columns / autoload (and thus Alembic) work. Without the fix
these raise "column c.reldiststyle does not exist".
"""

import psycopg2
import pytest

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlalchemy_redshift")

RS_CONFIG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)
# redshift_connector over the dialect; ssl=False isolates this from the TLS path
# (covered by test_redshift_proxy.py), so it exercises only reflection.
URL = "redshift+redshift_connector://oblako:oblako@localhost:5439/oblako"
TABLE = "sa_reflect_probe"


def _catalog_compat_present() -> bool:
    """True if the engine answers the Redshift-only catalog columns.

    Skips cleanly on a pre-fix published image; runs for real once it republishes.
    """
    try:
        c = psycopg2.connect(connect_timeout=3, **RS_CONFIG)
        c.autocommit = True
        try:
            cur = c.cursor()
            cur.execute("SELECT c.reldiststyle FROM pg_catalog.pg_class c LIMIT 1")
            cur.execute("SELECT format_encoding(0)")
            return True
        except Exception:
            return False
        finally:
            c.close()
    except Exception:
        return False


@pytest.fixture
def engine():
    eng = sa.create_engine(URL, connect_args={"ssl": False})
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP TABLE IF EXISTS {TABLE}")
        # Redshift-flavored DDL: the dialect emits DISTSTYLE/DISTKEY/SORTKEY, the
        # proxy strips them; checkfirst runs the reldiststyle catalog probe.
        c.exec_driver_sql(
            f"CREATE TABLE {TABLE} "
            f"(id int NOT NULL, user_id int, payload varchar(64)) "
            f"DISTSTYLE KEY DISTKEY (user_id) SORTKEY (id)"
        )
    yield eng
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP TABLE IF EXISTS {TABLE}")
    eng.dispose()


@pytest.mark.skipif(
    not _catalog_compat_present(), reason="image without catalog compat"
)
def test_get_table_names(engine):
    # relations query: reads c.reldiststyle + UNIONs svv_external_tables
    assert TABLE in sa.inspect(engine).get_table_names()


@pytest.mark.skipif(
    not _catalog_compat_present(), reason="image without catalog compat"
)
def test_get_columns(engine):
    # column query: reads attencodingtype/format_encoding, attisdistkey,
    # attsortkeyord, adsrc, and UNIONs pg_get_late_binding_view_cols + svv_*
    cols = {c["name"]: c for c in sa.inspect(engine).get_columns(TABLE)}
    assert set(cols) == {"id", "user_id", "payload"}
    assert not cols["id"]["nullable"]  # NOT NULL round-trips through reflection


@pytest.mark.skipif(
    not _catalog_compat_present(), reason="image without catalog compat"
)
def test_autoload_and_default_checkfirst(engine):
    # create_all with the default checkfirst=True runs has_table (the catalog
    # probe) then a full autoload -> the Alembic-style reflection path
    md = sa.MetaData()
    reflected = sa.Table(TABLE, md, autoload_with=engine)
    assert [c.name for c in reflected.columns] == ["id", "user_id", "payload"]
    md.create_all(engine)  # checkfirst=True: no-op, but must not raise
