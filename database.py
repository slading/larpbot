"""
database.py — Асинхронное подключение к PostgreSQL (SQLAlchemy 2.0 + asyncpg).

Автоматически нормализует DATABASE_URL из .env:
  • postgresql:// → postgresql+asyncpg://
  • sslmode=require → ssl=require
  • убирает channel_binding, options и пр.
"""

import os
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


# ═══════════════════════════  URL-ФИКС  ════════════════════════════════════

def _fix_asyncpg_url(raw_url: str) -> str:
    """Приводит DATABASE_URL к формату, совместимому с asyncpg."""

    if raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlparse(raw_url)
    params = parse_qs(parsed.query)

    INCOMPATIBLE = {"sslmode", "channel_binding", "options"}

    needs_ssl = False
    if "sslmode" in params:
        val = params["sslmode"][0].lower()
        if val in ("require", "verify-ca", "verify-full"):
            needs_ssl = True

    clean = {k: v for k, v in params.items() if k not in INCOMPATIBLE}

    if needs_ssl and "ssl" not in clean:
        clean["ssl"] = ["require"]

    new_query = urlencode(clean, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


# ═══════════════════════════  ENGINE  ══════════════════════════════════════

_raw = os.getenv("DATABASE_URL")
if not _raw:
    raise RuntimeError("DATABASE_URL не задан! Проверь .env")

DATABASE_URL = _fix_asyncpg_url(_raw)

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    echo=False,
)

# ═══════════════════════════  SESSION  ═════════════════════════════════════

async_session = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


# ═══════════════════════════  BASE  ════════════════════════════════════════

class Base(AsyncAttrs, DeclarativeBase):
    pass
