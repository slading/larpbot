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

# Некоторые хостинги (Railway, Heroku) могут назвать переменную иначе
if not _raw:
    _raw = os.getenv("DATABASE_PRIVATE_URL")
if not _raw:
    _raw = os.getenv("DATABASE_PUBLIC_URL")
if not _raw:
    _raw = os.getenv("PGDATABASE")  # Railway иногда задаёт отдельные PG-переменные

# Если Railway задал отдельные PG_* переменные — собираем URL вручную
if not _raw:
    pg_host = os.getenv("PGHOST")
    pg_user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    pg_pass = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")
    pg_db = os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB")
    pg_port = os.getenv("PGPORT", "5432")
    if pg_host and pg_user and pg_pass and pg_db:
        _raw = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}?sslmode=require"

if not _raw:
    import sys
    print("❌ DATABASE_URL не задан!", file=sys.stderr)
    print("   Доступные переменные окружения:", file=sys.stderr)
    for k, v in sorted(os.environ.items()):
        if any(word in k.upper() for word in ("PG", "DB", "DATA", "SQL", "POSTGRES", "NEON")):
            print(f"     {k}={v[:50]}...", file=sys.stderr)
    raise RuntimeError(
        "DATABASE_URL не задан! На Railway добавь переменную DATABASE_URL "
        "во вкладке Variables. Значение — строка подключения из Neon."
    )

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
