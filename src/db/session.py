import logging
from collections.abc import AsyncGenerator

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.db.models import Base

logger = logging.getLogger(__name__)


def _get_database_url() -> str:
    """Normalize DATABASE_URL for async SQLAlchemy.

    Render provides postgres://... but SQLAlchemy async requires
    postgresql+asyncpg://... — handle that conversion here.
    """
    url = settings.DATABASE_URL

    # Render-style postgres:// → postgresql+asyncpg://
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    return url


_db_url = _get_database_url()
_is_sqlite = "sqlite" in _db_url

engine = create_async_engine(
    _db_url,
    echo=False,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _ensure_columns(conn) -> None:
    """Add missing columns to existing tables (create_all doesn't do this).

    Uses SQLAlchemy inspect to compare model schema vs actual DB schema.
    Only ADDs columns — never drops or alters existing ones. Safe to run
    on every startup (idempotent).
    """
    inspector = sa_inspect(conn)

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all already handled new tables

        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}

        for col in table.columns:
            if col.name not in existing_cols:
                # Build ALTER TABLE ADD COLUMN statement
                col_type = col.type.compile(dialect=conn.dialect)

                # Determine default value
                default = ""
                if col.default is not None:
                    default_val = col.default.arg
                    if isinstance(default_val, str):
                        default = f" DEFAULT '{default_val}'"
                    elif isinstance(default_val, bool):
                        default = f" DEFAULT {'true' if default_val else 'false'}"
                    elif isinstance(default_val, (int, float)):
                        default = f" DEFAULT {default_val}"

                # Handle nullability
                if col.nullable or col.nullable is None:
                    nullable = ""
                else:
                    nullable = " NOT NULL"

                # Build the statement
                if nullable and default:
                    stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}{default}{nullable}'
                elif nullable and not default:
                    # NOT NULL without default — use safe defaults
                    stmt = f"ALTER TABLE \"{table.name}\" ADD COLUMN \"{col.name}\" {col_type} DEFAULT ''{nullable}"
                else:
                    stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}{default}'

                conn.execute(text(stmt))
                logger.info(f"Auto-migration: added column {table.name}.{col.name} ({col_type})")


def _fix_product_urls(conn) -> None:
    """One-time fixups for product URLs with wrong path patterns."""
    # Altitude Sports: /products/ → /p/ (correct path prefix)
    result = conn.execute(text(
        "UPDATE products SET url = REPLACE(url, "
        "'altitude-sports.com/products/', 'altitude-sports.com/p/') "
        "WHERE url LIKE '%altitude-sports.com/products/%'"
    ))
    if result.rowcount:
        logger.info(
            f"URL fixup: corrected {result.rowcount} Altitude Sports product URLs "
            "(/products/ → /p/)"
        )


async def init_db() -> None:
    async with engine.begin() as conn:
        if _is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
        # Add any missing columns to existing tables
        await conn.run_sync(_ensure_columns)
        # Fix any product URLs with wrong path patterns
        await conn.run_sync(_fix_product_urls)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
