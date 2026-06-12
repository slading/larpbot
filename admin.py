"""
admin.py — Расширенная админ-панель бота.

/admin (только ЛС, только ADMIN_IDS из .env).

Функционал:
  • 💰 Выдать баланс — начислить Dark Stars игроку по tg_id
  • 📉 Списать баланс — списать Dark Stars у игрока
  • ➕ Создать промокод — КОД СУММА ЛИМИТ
  • 📋 Список промокодов — все промо со статусом
  • 📊 Статистика бота — игроки, кланы, экономика
  • 📢 Создать рассылку — отправить текст всем юзерам
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import F, Router, Bot, types
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, select

from app.database import async_session
from app.models import Clan, Promocode, User

# ── Админ-ID из .env ────────────────────────────────────────────────────────
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)


# ═══════════════════════════  FSM  ══════════════════════════════════════════

class CreatePromo(StatesGroup):
    waiting_input = State()


class AdminState(StatesGroup):
    waiting_for_give = State()
    waiting_for_take = State()
    waiting_for_broadcast_msg = State()


# ═══════════════════════════  УТИЛИТЫ  ══════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="💰 Выдать баланс", callback_data="admin_give_balance"),
            types.InlineKeyboardButton(text="📉 Списать баланс", callback_data="admin_take_balance"),
        ],
        [
            types.InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:create_promo"),
            types.InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin:list_promos"),
        ],
        [
            types.InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats"),
            types.InlineKeyboardButton(text="📢 Создать рассылку", callback_data="admin_broadcast"),
        ],
        [
            types.InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close"),
        ],
    ])


def cancel_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 Отмена", callback_data="admin:cancel_fsm")],
    ])


# ═══════════════════════════  /admin  ═══════════════════════════════════════

@router.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа.")
        return

    await state.clear()
    await message.answer(
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери действие:",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


# ═══════════════════════════  ОБЩИЕ CALLBACKS  ═════════════════════════════

@router.callback_query(F.data == "admin:close")
async def cb_close(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "admin:cancel_fsm")
async def cb_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери действие:",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )
    await callback.answer("Отменено")


# ══════════════════════════════════════════════════════════════════════════════
#
#                    💰 ВЫДАТЬ / 📉 СПИСАТЬ БАЛАНС
#
# ══════════════════════════════════════════════════════════════════════════════


@router.callback_query(F.data == "admin_give_balance")
async def cb_give_balance_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AdminState.waiting_for_give)
    await callback.message.edit_text(
        "💰 <b>ВЫДАТЬ БАЛАНС</b>\n\n"
        "Введите <b>Telegram ID</b> игрока и <b>сумму</b> через пробел.\n\n"
        "Пример: <code>123456789 5000</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_give, F.text)
async def fsm_give_balance(message: types.Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer(
            "❌ Неверный формат! Нужно: <code>ID СУММА</code>\n"
            "Пример: <code>123456789 5000</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    if not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer(
            "❌ ID и сумма должны быть числами!\n"
            "Пример: <code>123456789 5000</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    target_id = int(parts[0])
    amount = int(parts[1])

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0!", reply_markup=cancel_keyboard())
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.tg_id == target_id))
        user = result.scalar_one_or_none()

        if user is None:
            await message.answer(
                f"❌ Игрок с ID <code>{target_id}</code> не найден в базе!",
                parse_mode="HTML",
                reply_markup=cancel_keyboard(),
            )
            return

        user.dark_stars += amount
        await session.commit()
        new_balance = user.dark_stars
        username = user.username

    display = f"@{username}" if username else f"ID:{target_id}"

    await state.clear()
    await message.answer(
        f"✅ <b>Успешно выдано {amount:,} ⭐ игроку {display}</b>\n"
        f"💼 Новый баланс: <b>{new_balance:,} ⭐</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )

    # Уведомляем игрока
    try:
        await bot.send_message(
            chat_id=target_id,
            text=(
                f"🎁 <b>Администрация начислила вам "
                f"{amount:,} ⭐ Dark Stars!</b>\n\n"
                f"💼 Баланс: <b>{new_balance:,} ⭐</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ── Списать баланс ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_take_balance")
async def cb_take_balance_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AdminState.waiting_for_take)
    await callback.message.edit_text(
        "📉 <b>СПИСАТЬ БАЛАНС</b>\n\n"
        "Введите <b>Telegram ID</b> игрока и <b>сумму</b> через пробел.\n\n"
        "Пример: <code>123456789 5000</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_take, F.text)
async def fsm_take_balance(message: types.Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer(
            "❌ Неверный формат! Нужно: <code>ID СУММА</code>\n"
            "Пример: <code>123456789 5000</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    if not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer(
            "❌ ID и сумма должны быть числами!",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    target_id = int(parts[0])
    amount = int(parts[1])

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0!", reply_markup=cancel_keyboard())
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.tg_id == target_id))
        user = result.scalar_one_or_none()

        if user is None:
            await message.answer(
                f"❌ Игрок с ID <code>{target_id}</code> не найден в базе!",
                parse_mode="HTML",
                reply_markup=cancel_keyboard(),
            )
            return

        user.dark_stars -= amount
        await session.commit()
        new_balance = user.dark_stars
        username = user.username

    display = f"@{username}" if username else f"ID:{target_id}"

    await state.clear()
    await message.answer(
        f"✅ <b>Успешно списано {amount:,} ⭐ у игрока {display}</b>\n"
        f"💼 Новый баланс: <b>{new_balance:,} ⭐</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )

    # Уведомляем игрока
    try:
        await bot.send_message(
            chat_id=target_id,
            text=(
                f"📉 <b>Администрация списала {amount:,} ⭐ Dark Stars "
                f"с вашего баланса.</b>\n\n"
                f"💼 Баланс: <b>{new_balance:,} ⭐</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#
#                    📊 СТАТИСТИКА БОТА
#
# ══════════════════════════════════════════════════════════════════════════════


@router.callback_query(F.data == "admin_stats")
async def cb_stats(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    async with async_session() as session:
        # Всего игроков
        total_users = (await session.execute(
            select(func.count(User.id))
        )).scalar()

        # Всего кланов
        total_clans = (await session.execute(
            select(func.count(Clan.id))
        )).scalar()

        # Общая масса монет
        total_stars = (await session.execute(
            select(func.coalesce(func.sum(User.dark_stars), 0))
        )).scalar()

        # Средний баланс
        avg_stars = (await session.execute(
            select(func.coalesce(func.avg(User.dark_stars), 0))
        )).scalar()

        # Всего промокодов
        total_promos = (await session.execute(
            select(func.count(Promocode.id))
        )).scalar()

        # Игроков с рефералами
        total_referrals = (await session.execute(
            select(func.count(User.id)).where(User.referred_by.isnot(None))
        )).scalar()

    await callback.message.edit_text(
        "📊 <b>СТАТИСТИКА БОТА</b>\n\n"
        f"👥 Всего игроков: <b>{total_users:,}</b>\n"
        f"🏰 Всего кланов: <b>{total_clans:,}</b>\n"
        f"🎟 Промокодов: <b>{total_promos:,}</b>\n"
        f"🔗 Пришло по рефералам: <b>{total_referrals:,}</b>\n\n"
        f"💰 Общая масса монет: <b>{total_stars:,} ⭐</b>\n"
        f"📈 Средний баланс: <b>{int(avg_stars):,} ⭐</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#
#                    📢 РАССЫЛКА
#
# ══════════════════════════════════════════════════════════════════════════════


@router.callback_query(F.data == "admin_broadcast")
async def cb_broadcast_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AdminState.waiting_for_broadcast_msg)
    await callback.message.edit_text(
        "📢 <b>СОЗДАТЬ РАССЫЛКУ</b>\n\n"
        "Отправь текст сообщения, который получат <b>все</b> пользователи бота.\n\n"
        "Поддерживается HTML-разметка (<b>, <i>, <code> и т.д.).",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminState.waiting_for_broadcast_msg, F.text)
async def fsm_broadcast(message: types.Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return

    broadcast_text = message.text.strip()

    if not broadcast_text:
        await message.answer("❌ Сообщение не может быть пустым!", reply_markup=cancel_keyboard())
        return

    await state.clear()

    # Получаем все tg_id
    async with async_session() as session:
        result = await session.execute(select(User.tg_id))
        all_ids = [row[0] for row in result.all()]

    total = len(all_ids)
    sent = 0
    failed = 0

    status_msg = await message.answer(
        f"📢 Рассылка запущена... (0/{total})",
        parse_mode="HTML",
    )

    for i, tg_id in enumerate(all_ids):
        try:
            await bot.send_message(chat_id=tg_id, text=broadcast_text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1

        # Обновляем статус каждые 25 сообщений
        if (i + 1) % 25 == 0:
            try:
                await status_msg.edit_text(
                    f"📢 Рассылка... ({i + 1}/{total})",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # Пауза для Telegram rate limit (30 msg/sec)
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>\n"
        f"👥 Всего: <b>{total}</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#
#                    ➕ СОЗДАНИЕ ПРОМОКОДОВ
#
# ══════════════════════════════════════════════════════════════════════════════


@router.callback_query(F.data == "admin:create_promo")
async def cb_create_promo(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(CreatePromo.waiting_input)
    await callback.message.edit_text(
        "🆕 <b>Создание промокода</b>\n\n"
        "Введи данные <b>одной строкой</b> в формате:\n"
        "<code>КОД СУММА ЛИМИТ</code>\n\n"
        "Пример: <code>CRYPTO2026 5000 50</code>\n\n"
        "• КОД — уникальный код промокода\n"
        "• СУММА — награда в Dark Stars\n"
        "• ЛИМИТ — макс. количество активаций",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(CreatePromo.waiting_input, F.text)
async def fsm_promo_input(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split()

    if len(parts) != 3:
        await message.answer(
            "❌ Неверный формат! Нужно: <code>КОД СУММА ЛИМИТ</code>\n"
            "Пример: <code>CRYPTO2026 5000 50</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    code_raw, amount_raw, limit_raw = parts

    if not amount_raw.isdigit() or not limit_raw.isdigit():
        await message.answer(
            "❌ СУММА и ЛИМИТ должны быть числами!\n"
            "Пример: <code>CRYPTO2026 5000 50</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    code = code_raw.strip()
    amount = int(amount_raw)
    limit = int(limit_raw)

    if amount <= 0 or limit <= 0:
        await message.answer(
            "❌ СУММА и ЛИМИТ должны быть больше 0!",
            reply_markup=cancel_keyboard(),
        )
        return

    if len(code) > 64:
        await message.answer(
            "❌ Код слишком длинный (макс. 64 символа)!",
            reply_markup=cancel_keyboard(),
        )
        return

    async with async_session() as session:
        exists = await session.execute(
            select(Promocode).where(Promocode.code == code)
        )
        if exists.scalar_one_or_none():
            await message.answer(
                f"⚠️ Промокод <code>{code}</code> уже существует! Введи другой:",
                parse_mode="HTML",
                reply_markup=cancel_keyboard(),
            )
            return

        promo = Promocode(code=code, reward_amount=amount, max_activations=limit)
        session.add(promo)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>ПРОМОКОД СОЗДАН!</b>\n\n"
        f"📝 Код: <code>{code}</code>\n"
        f"💰 Награда: <b>{amount:,} ⭐ Dark Stars</b>\n"
        f"🔢 Лимит: <b>{limit}</b> активаций\n\n"
        f"Игроки вводят: <code>промо {code}</code>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#
#                    📋 СПИСОК ПРОМОКОДОВ
#
# ══════════════════════════════════════════════════════════════════════════════


@router.callback_query(F.data == "admin:list_promos")
async def cb_list_promos(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    async with async_session() as session:
        result = await session.execute(
            select(Promocode).order_by(Promocode.id.desc())
        )
        promos = result.scalars().all()

    if not promos:
        await callback.message.edit_text(
            "📋 <b>Промокодов пока нет.</b>",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
        await callback.answer()
        return

    lines: list[str] = ["📋 <b>ПРОМОКОДЫ</b>\n"]
    for p in promos:
        status = "✅" if p.current_activations < p.max_activations else "🚫"
        lines.append(
            f"{status} <code>{p.code}</code> — "
            f"{p.reward_amount:,} ⭐  "
            f"({p.current_activations}/{p.max_activations})"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )
    await callback.answer()
