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
# redshift_connector over the dialect, with verified TLS (oblako trust has added
# the cert), as the book connects.
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
    eng = sa.create_engine(URL, connect_args={"sslmode": "verify-ca"})
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


# ---------------------------------------------------------------------------
# System objects stay out of reflection, so Alembic leaves them alone
# ---------------------------------------------------------------------------
SYSTEM_PREFIXES = ("stl_", "stv_", "svv_", "pg_")


def _is_system(name: str) -> bool:
    return name.startswith(SYSTEM_PREFIXES)


def _diff_names(diffs) -> list[str]:
    """The table names autogenerate's comparison touches, schema-qualified."""
    names = []
    for d in diffs:
        for item in d if isinstance(d, list) else [d]:
            table = item[1] if len(item) > 1 else None
            if isinstance(table, sa.Table):
                names.append(table.fullname)
            elif len(item) > 3 and isinstance(item[3], str):
                names.append(item[3] if item[2] is None else f"{item[2]}.{item[3]}")
    return names


@pytest.mark.skipif(
    not _catalog_compat_present(), reason="image without catalog compat"
)
def test_reflection_lists_no_system_objects(engine):
    # Redshift keeps STL/STV tables, SVV views and its internals in pg_catalog and
    # pg_ schemas; in public they would read as the user's own tables
    insp = sa.inspect(engine)
    assert not [t for t in insp.get_table_names() if _is_system(t)]
    assert not [v for v in insp.get_view_names() if _is_system(v)]
    internal = {"pg_oblako", "oblako_ml", "redshift_compat"}
    assert not internal & set(insp.get_schema_names())


@pytest.mark.skipif(
    not _catalog_compat_present(), reason="image without catalog compat"
)
def test_orm_with_redshift_options_and_alembic_autogenerate(engine):
    """An ORM model with DISTKEY/SORTKEY creates, and autogenerate diffs it exactly.

    The comparison proposes nothing for the system objects (it used to propose
    dropping stl_scan, stv_blocklist and stv_tbl_perm), nothing for the table as
    created, and the one added column once the model gains it.
    """
    alembic_autogenerate = pytest.importorskip("alembic.autogenerate")
    from alembic.migration import MigrationContext
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    name = "sa_orm_probe"
    options = {
        "redshift_diststyle": "KEY",
        "redshift_distkey": "user_id",
        "redshift_sortkey": "id",
    }

    class V1(DeclarativeBase):
        pass

    class EventV1(V1):
        __tablename__ = name
        __table_args__ = options
        id: Mapped[int] = mapped_column(primary_key=True)
        user_id: Mapped[int]

    class V2(DeclarativeBase):
        pass

    class EventV2(V2):
        __tablename__ = name
        __table_args__ = options
        id: Mapped[int] = mapped_column(primary_key=True)
        user_id: Mapped[int]
        segment: Mapped[str | None] = mapped_column(sa.String(32))

    V1.metadata.drop_all(engine)
    V1.metadata.create_all(engine)
    try:
        with engine.connect() as conn:
            for opts in ({}, {"include_schemas": True}):
                ctx = MigrationContext.configure(conn, opts=opts)
                touched = _diff_names(
                    alembic_autogenerate.compare_metadata(ctx, V1.metadata)
                )
                assert not [n for n in touched if _is_system(n.split(".")[-1])]
                assert not [n for n in touched if n.split(".")[0] == "pg_oblako"]
                assert name not in touched
            ctx = MigrationContext.configure(conn)
            changes = [
                d
                for d in alembic_autogenerate.compare_metadata(ctx, V2.metadata)
                if not isinstance(d, list) and d[0] == "add_column" and d[2] == name
            ]
            assert [(d[0], d[3].name) for d in changes] == [("add_column", "segment")]
    finally:
        V1.metadata.drop_all(engine)
