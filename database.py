import os
import re
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_raw_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost/glico")

# Railway injeta "postgresql://" — asyncpg precisa de "postgresql+asyncpg://"
# Também remove ?sslmode=require (asyncpg trata SSL via connect_args, não query string)
_url = re.sub(r"^postgresql://", "postgresql+asyncpg://", _raw_url)
_url = re.sub(r"\?.*$", "", _url)  # remove query string inteira

# SSL: Railway requer SSL em produção — detecta pelo hostname
_is_railway = "railway.app" in _raw_url or os.getenv("RAILWAY_ENVIRONMENT") is not None
_connect_args = {"ssl": "require"} if _is_railway else {}

engine = create_async_engine(_url, echo=False, connect_args=_connect_args)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
