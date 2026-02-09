from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.db.models import Base


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


async def init_db() -> None:
    async with engine.begin() as conn:
        if _is_sqlite:
            await conn.execute(
                __import__("sqlalchemy").text("PRAGMA journal_mode=WAL")
            )
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
