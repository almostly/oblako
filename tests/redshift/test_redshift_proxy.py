"""Integration test: Redshift physical DDL is tolerated end to end.

Requires the engine (docker compose up redshift). The bundled wire proxy strips
DISTSTYLE/DISTKEY/SORTKEY/ENCODE so a Redshift CREATE TABLE (as awswrangler and
dbt emit) succeeds against the PostgreSQL engine behind it.
"""

import psycopg2
import pytest

RS_CONFIG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)


def _proxy_present() -> bool:
    """True if the running engine has the DDL proxy (a DISTSTYLE create works).

    Lets the test skip cleanly when CI is still on a pre-proxy published image;
    it runs for real once the image republishes.
    """
    try:
        c = psycopg2.connect(**RS_CONFIG)
        c.autocommit = True
        try:
            cur = c.cursor()
            cur.execute("DROP TABLE IF EXISTS _oblako_proxy_probe")
            cur.execute("CREATE TABLE _oblako_proxy_probe (id int) DISTSTYLE AUTO")
            cur.execute("DROP TABLE _oblako_proxy_probe")
            return True
        except Exception:
            return False
        finally:
            c.close()
    except Exception:
        return False


@pytest.mark.skipif(not _proxy_present(), reason="redshift image without the DDL proxy")
def test_physical_ddl_is_accepted():
    c = psycopg2.connect(**RS_CONFIG)
    c.autocommit = True
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS proxy_phys")
    cur.execute(
        "CREATE TABLE proxy_phys (id int ENCODE az64, s varchar(10) ENCODE lzo) "
        "DISTSTYLE KEY DISTKEY (id) COMPOUND SORTKEY (id)"
    )
    cur.execute("INSERT INTO proxy_phys VALUES (1, 'x')")
    cur.execute("SELECT count(*) FROM proxy_phys")
    assert cur.fetchone()[0] == 1
    cur.execute("DROP TABLE proxy_phys")
    c.close()


@pytest.mark.skipif(not _proxy_present(), reason="redshift image without the DDL proxy")
def test_varchar_max_ddl_is_accepted():
    # dlt's redshift destination emits varchar(max); the proxy maps it to text
    c = psycopg2.connect(**RS_CONFIG)
    c.autocommit = True
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS proxy_vmax")
    cur.execute("CREATE TABLE proxy_vmax (id int, payload varchar(max))")
    cur.execute("INSERT INTO proxy_vmax VALUES (1, %s)", ("x" * 70000,))
    cur.execute("SELECT length(payload) FROM proxy_vmax")
    assert cur.fetchone()[0] == 70000
    cur.execute("DROP TABLE proxy_vmax")
    c.close()
