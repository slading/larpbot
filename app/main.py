"""
main.py — Точка входа LARP-чат-бота.

  1. Загружает .env
  2. При старте проверяет схему БД — если таблицы устарели, пересоздаёт их
  3. Подключает роутеры: admin → private → group
  4. Запускает polling
"""

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

from app.database import engine, Base                        # noqa: E402
from app.handlers import private_router, group_router        # noqa: E402
from app.admin import router as admin_router                 # noqa: E402
from app.middleware import ThrottlingMiddleware               # noqa: E402


async def on_startup(bot: Bot) -> None:
    """
    Создаёт / пересоздаёт таблицы в Postgres.

    create_all НЕ делает ALTER TABLE (не добавляет новые колонки).
    Поэтому если схема изменилась — дропаем всё и создаём заново.
    В продакшене с реальными данными замени это на Alembic-миграции.
    """
    async with engine.begin() as conn:
        # DROP ALL — удаляет все таблицы, чтобы пересоздать с новой схемой
        await conn.run_sync(Base.metadata.drop_all)
        # CREATE ALL — создаёт все таблицы с актуальной схемой
        await conn.run_sync(Base.metadata.create_all)

    logging.info("✅ Таблицы БД пересозданы с актуальной схемой.")

    me = await bot.get_me()
    logging.info("🤖 Бот запущен: @%s (id=%s)", me.username, me.id)


async def on_shutdown(bot: Bot) -> None:
    await engine.dispose()
    logging.info("🛑 Пул соединений закрыт.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
        stream=sys.stdout,
    )

    token = os.getenv("BOT_TOKEN")
    if not token or token == "your_telegram_bot_token_here":
        logging.error("❌ BOT_TOKEN не задан! Укажи его в .env")
        sys.exit(1)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # ── Глобальный анти-спам (2 сек на любое действие) ──────────────────
    throttle = ThrottlingMiddleware()
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)

    # Порядок: admin (FSM в ЛС) → private (кнопки + оплата) → group (чаты)
    dp.include_router(admin_router)
    dp.include_router(private_router)
    dp.include_router(group_router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logging.info("🚀 Запускаем polling…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
