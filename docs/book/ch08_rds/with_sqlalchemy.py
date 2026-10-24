from decimal import Decimal

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
)

# no host, port or credentials: psycopg reads the PG* variables
engine = sa.create_engine("postgresql+psycopg://")


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    sku: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))


class DocPage(Base):
    __tablename__ = "doc_pages"
    slug: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    embedding = mapped_column(Vector(3))


with Session(engine) as session:
    upsert = insert(Product).values(
        sku="CUP-01", name="Cup", price=Decimal("6.50")
    )
    session.execute(
        upsert.on_conflict_do_update(
            index_elements=["sku"],
            set_={"price": upsert.excluded.price},
        )
    )
    session.commit()

    cheap = session.scalars(
        sa.select(Product)
        .where(Product.price < 10)
        .order_by(Product.sku)
    )
    print([(p.sku, p.price) for p in cheap])

    query = [0.1, 1.0, 0.3]
    nearest = session.scalars(
        sa.select(DocPage)
        .order_by(DocPage.embedding.cosine_distance(query))
        .limit(2)
    )
    print([page.slug for page in nearest])
