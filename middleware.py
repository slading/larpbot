"""
middleware.py — Глобальный анти-спам (Throttling) для сообщений и callback-ов.

Кулдаун: 2 секунды на любое действие.
  • Группы — молча игнорирует спам (не засоряет чат)
  • ЛС — один раз пишет предупреждение

Исключения: callback_data для мини-игр (мины, дуэли) пропускаются без задержки.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject


THROTTLE_SECONDS = 2.0

# Префиксы callback_data, которые НЕ тротлятся (игровые клики)
THROTTLE_EXEMPT_PREFIXES = (
    "mine_click_",
    "mine_cashout",
    "mine_noop_",
    "accept_duel_",
    "decline_duel_",
    "clan_req_accept_",
    "clan_req_reject_",
)


class ThrottlingMiddleware(BaseMiddleware):
    """
    Глобальный троттлинг по tg_id.

    Для каждого юзера хранит timestamp последнего пропущенного события.
    Если между событиями прошло меньше THROTTLE_SECONDS:
      • в группе — молчим (return без вызова handler)
      • в ЛС — отправляем одно предупреждение
    Callback-ы мини-игр (мины, дуэли) пропускаются без задержки.
    """

    def __init__(self) -> None:
        super().__init__()
        # tg_id → (last_allowed_ts, already_warned)
        self.cooldowns: Dict[int, tuple[float, bool]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # ── Callback мини-игр: пропускаем без троттлинга ──
        if isinstance(event, CallbackQuery) and event.data:
            if event.data.startswith(THROTTLE_EXEMPT_PREFIXES):
                return await handler(event, data)

        # Определяем user и chat_type
        user = None
        chat_type = None

        if isinstance(event, Message):
            user = event.from_user
            chat_type = event.chat.type if event.chat else None
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            chat_type = (
                event.message.chat.type
                if event.message and event.message.chat
                else ChatType.PRIVATE
            )

        # Если не удалось определить юзера — пропускаем без проверки
        if user is None:
            return await handler(event, data)

        tg_id = user.id
        now = time.monotonic()

        last_allowed, already_warned = self.cooldowns.get(tg_id, (0.0, False))
        elapsed = now - last_allowed

        if elapsed < THROTTLE_SECONDS:
            # ── Спам! Блокируем ──
            if chat_type == ChatType.PRIVATE and not already_warned:
                if isinstance(event, Message):
                    await event.answer("⚠️ Не спамь! Подожди немного.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Не спамь! Подожди немного.", show_alert=True)
                self.cooldowns[tg_id] = (last_allowed, True)

            return  # НЕ вызываем handler

        # ── Кулдаун прошёл — пропускаем ──
        self.cooldowns[tg_id] = (now, False)
        return await handler(event, data)
