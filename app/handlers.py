"""
handlers.py — Все игровые хэндлеры LARP-чат-бота.

private_router — ЛС: /start, Reply-клавиатура, профиль, магазин (Telegram Stars),
                 кланы, инвентарь, бонус, промо, оплата
group_router   — Группы: активация /start, кейсы, дуэли, переводы, кланы
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Set

from aiogram import F, Router, Bot, types
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import LabeledPrice, PreCheckoutQuery
from sqlalchemy import func, select, update

from app.database import async_session
from app.models import Clan, ClanJoinRequest, InventoryItem, Promocode, User
from app.nft_dataset import NFT_DATASET

# ═══════════════════════════  РОУТЕРЫ  ══════════════════════════════════════

private_router = Router()
private_router.message.filter(F.chat.type == ChatType.PRIVATE)
private_router.pre_checkout_query.filter()

group_router = Router()
group_router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

# ═══════════════════════════  ГЛОБАЛЬНОЕ СОСТОЯНИЕ  ═════════════════════════

activated_chats: Set[int] = set()

# ═══════════════════════════  КОНСТАНТЫ  ════════════════════════════════════

RARITY_WEIGHTS = {
    "Common": 0.65, "Rare": 0.23, "Epic": 0.09,
    "Legendary": 0.027, "Mythic": 0.003,
}
RARITIES = list(RARITY_WEIGHTS.keys())
WEIGHTS = list(RARITY_WEIGHTS.values())

CASE_PRICES = {"мемный": 500, "кит": 2500}

RARITY_EMOJI = {
    "Common": "⚪", "Rare": "🔵", "Epic": "🟣",
    "Legendary": "🟡", "Mythic": "🔴",
}

DAILY_BONUS = 2500
DAILY_COOLDOWN = timedelta(hours=24)
ELO_CHANGE = 50
CLAN_CREATION_COST = 5000
REFERRAL_BONUS = 2500

# Товары магазина: payload → (dark_stars, xtr_price, label)
SHOP_ITEMS = {
    "buy_stars_50":   (100_000,      50, "50 ⭐️ Stars ➡️ 100,000 Dark Stars"),
    "buy_stars_100":  (200_000,     100, "100 ⭐️ Stars ➡️ 200,000 Dark Stars"),
    "buy_stars_250":  (500_000,     250, "250 ⭐️ Stars ➡️ 500,000 Dark Stars"),
    "buy_stars_500":  (1_000_000,   500, "500 ⭐️ Stars ➡️ 1,000,000 Dark Stars"),
    "buy_stars_1000": (2_000_000,  1000, "1000 ⭐️ Stars ➡️ 2,000,000 Dark Stars"),
    "buy_stars_2500": (5_000_000,  2500, "2500 ⭐️ Stars ➡️ 5,000,000 Dark Stars"),
}

# Курс: 1 Telegram Star = 2000 Dark Stars
DARK_STARS_PER_XTR = 2000
MIN_CUSTOM_AMOUNT = 250

BATTLE_WINNER = [
    "нанёс удар смарт-контрактом 💥",
    "применил mass-adopt атаку 🚀",
    "запустил rug-pull-ловушку 🕳",
    "использовал flash-loan комбо ⚡",
    "активировал NFT-щит и контратаковал 🛡",
    "зарядил газ на максимум и отправил транзакцию первым 🏎️",
    "открыл секретный кейс и достал меч Виталика ⚔️",
    "заминтил NFT противника и продал на OpenSea 🖼️",
]

BATTLE_LOSER = [
    "поскользнулся на красной свече 📉",
    "забыл seed-фразу в бою 🤦",
    "попался на фишинг-ловушку 🎣",
    "его газ-лимит кончился ⛽",
    "потерял приватный ключ 🔑",
    "нажал 'Approve All' на скам-контракте 💀",
    "отправил токены на неправильную сеть 🌐",
    "купил на пике и продал на дне 📊",
]

# ═══════════════════════════  КЛАВИАТУРА ЛС  ══════════════════════════════

MAIN_KB = types.ReplyKeyboardMarkup(
    keyboard=[
        [
            types.KeyboardButton(text="👤 Мой Профиль"),
            types.KeyboardButton(text="📦 Кейсы"),
        ],
        [
            types.KeyboardButton(text="🛡️ Мой Клан"),
            types.KeyboardButton(text="💎 Магазин/Донат"),
        ],
        [
            types.KeyboardButton(text="🔗 Пригласить друга"),
        ],
    ],
    resize_keyboard=True,
)

# ═══════════════════════════  УТИЛИТЫ  ══════════════════════════════════════


async def get_or_create_user(session, tg_id: int, username: str | None = None) -> User:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(tg_id=tg_id, username=username, dark_stars=1000, elo_rating=1000)
        session.add(user)
        await session.flush()
    elif username and user.username != username:
        user.username = username
    return user


def roll_rarity() -> str:
    return random.choices(RARITIES, weights=WEIGHTS, k=1)[0]


def tag(u: types.User) -> str:
    return f"@{u.username}" if u.username else u.full_name


async def is_group_admin(message: types.Message) -> bool:
    member = await message.chat.get_member(message.from_user.id)
    return member.status in ("creator", "administrator")


# ═══════════════════════════  FSM: КАСТОМНЫЙ ДОНАТ  ═════════════════════════

class DonateState(StatesGroup):
    waiting_for_amount = State()


class MinesState(StatesGroup):
    playing = State()


class InventoryState(StatesGroup):
    waiting_for_sell_id = State()


# Слоты: value → множитель (None = проигрыш)
# value == 64: три семёрки; 1,22,43: три одинаковых
SLOT_JACKPOT = {64}
SLOT_TRIPLE = {1, 22, 43}
# Две одинаковых (частичные совпадения)
SLOT_DOUBLE = {2, 3, 4, 6, 11, 16, 17, 21, 32, 33, 38, 48, 49, 54, 59, 63}
MIN_SLOT_BET = 100
MIN_MINES_BET = 200
MINES_COUNT = 6
MINES_GRID_SIZE = 25   # 5×5


async def recalc_clan_elo(session, clan_id: int) -> int:
    """Пересчитать total_elo клана как сумму ELO всех участников."""
    q = await session.execute(
        select(func.coalesce(func.sum(User.elo_rating), 0))
        .where(User.clan_id == clan_id)
    )
    total = q.scalar()
    await session.execute(
        update(Clan).where(Clan.id == clan_id).values(total_elo=total)
    )
    return total


# ══════════════════════════════════════════════════════════════════════════════
#
#                            ЛС  (Private Chat)
#
# ══════════════════════════════════════════════════════════════════════════════


# ── /start ───────────────────────────────────────────────────────────────────

@private_router.message(Command("start"))
async def pm_start(message: types.Message, command: CommandObject, bot: Bot) -> None:
    tg_id = message.from_user.id
    username = message.from_user.username
    referrer_id: int | None = None

    # ── Парсим реферальную ссылку ──
    if command.args and command.args.startswith("ref_"):
        try:
            referrer_id = int(command.args.split("_", 1)[1])
        except (ValueError, IndexError):
            referrer_id = None
        # Нельзя пригласить самого себя
        if referrer_id == tg_id:
            referrer_id = None

    async with async_session() as session:
        # Проверяем, существует ли юзер уже
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        existing_user = result.scalar_one_or_none()

        if existing_user is None:
            # ── Новый игрок ──
            new_user = User(
                tg_id=tg_id,
                username=username,
                dark_stars=1000,
                elo_rating=1000,
            )

            if referrer_id is not None:
                # Проверяем, что пригласивший существует
                ref_result = await session.execute(
                    select(User).where(User.tg_id == referrer_id)
                )
                referrer = ref_result.scalar_one_or_none()

                if referrer is not None:
                    new_user.referred_by = referrer_id
                    new_user.dark_stars += REFERRAL_BONUS   # 1000 + 2500 = 3500
                    referrer.dark_stars += REFERRAL_BONUS

            session.add(new_user)
            await session.commit()

            # Уведомляем пригласившего
            if referrer_id is not None and referrer is not None:
                try:
                    await bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            f"🎉 <b>По твоей реферальной ссылке зарегистрировался "
                            f"новый игрок!</b>\n\n"
                            f"👤 Новичок: @{username or message.from_user.full_name}\n"
                            f"💰 Тебе начислено <b>+{REFERRAL_BONUS:,} ⭐ Dark Stars</b>!"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass  # Пригласивший мог заблокировать бота

            ref_text = ""
            if new_user.referred_by:
                ref_text = (
                    f"\n🎁 Бонус за приглашение: <b>+{REFERRAL_BONUS:,} ⭐</b>\n"
                    f"💰 Итого на старте: <b>{new_user.dark_stars:,} ⭐</b>\n"
                )

            await message.answer(
                f"👋 <b>Добро пожаловать, {message.from_user.full_name}!</b>\n\n"
                "Это <b>LARP Case Bot</b> — текстовый симулятор крипто-кейсов 🎰\n\n"
                "Тебе начислено <b>1 000 ⭐ Dark Stars</b> на старт!\n"
                f"{ref_text}\n"
                "Используй кнопки ниже для навигации.\n"
                "Кейсы открываются <b>только в групповых чатах</b> 💬",
                parse_mode="HTML",
                reply_markup=MAIN_KB,
            )
        else:
            # ── Юзер уже существует — просто приветствие ──
            if username and existing_user.username != username:
                existing_user.username = username
                await session.commit()

            await message.answer(
                f"👋 <b>С возвращением, {message.from_user.full_name}!</b>\n\n"
                f"Твой баланс: <b>{existing_user.dark_stars:,} ⭐ Dark Stars</b>\n\n"
                "Используй кнопки ниже для навигации 🎮",
                parse_mode="HTML",
                reply_markup=MAIN_KB,
            )


# ── 👤 Мой Профиль ──────────────────────────────────────────────────────────

@private_router.message(F.text == "👤 Мой Профиль")
async def pm_profile(message: types.Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        inv_q = await session.execute(
            select(
                func.count(InventoryItem.id),
                func.coalesce(func.sum(InventoryItem.market_value), 0),
            ).where(InventoryItem.user_id == user.tg_id)
        )
        item_count, total_value = inv_q.one()

        clan_name = user.clan.name if user.clan else "—"
        clan_role = (user.clan_role or "—").capitalize() if user.clan else "—"
        await session.commit()

    await message.answer(
        f"┌──── 📋 <b>ПРОФИЛЬ</b> ────┐\n"
        f"│\n"
        f"│  👤  <b>{message.from_user.full_name}</b>\n"
        f"│  🏷  @{user.username or '—'}\n"
        f"│\n"
        f"│  ⭐  Dark Stars: <b>{user.dark_stars:,}</b>\n"
        f"│  🏆  ELO: <b>{user.elo_rating}</b>\n"
        f"│  🏰  Клан: <b>{clan_name}</b>\n"
        f"│  👑  Роль: <b>{clan_role}</b>\n"
        f"│\n"
        f"│  🎒  Предметов: <b>{item_count}</b>\n"
        f"│  💰  Стоимость инвентаря: <b>{total_value:,} ⭐</b>\n"
        f"│\n"
        f"└─────────────────────┘",
        parse_mode="HTML",
    )


# ── 📦 Кейсы (инфо, открытие только в группе) ───────────────────────────────

@private_router.message(F.text == "📦 Кейсы")
async def pm_cases(message: types.Message) -> None:
    await message.answer(
        "📦 <b>КЕЙСЫ</b>\n\n"
        "Вы можете посмотреть список кейсов здесь, но открывать их "
        "можно <b>ТОЛЬКО в групповых чатах</b>, чтобы все видели ваш дроп!\n\n"
        "📦  <b>Мемный Кейс</b> — <b>500 ⭐</b>\n"
        "💎  <b>NFT Кит Кейс</b> — <b>2 500 ⭐</b>\n\n"
        "Используйте команды в чате:\n"
        "  <code>открыть мемный</code>\n"
        "  <code>открыть кит</code>",
        parse_mode="HTML",
    )


# ── 🛡️ Мой Клан ────────────────────────────────────────────────────────────

@private_router.message(F.text == "🛡️ Мой Клан")
async def pm_clan(message: types.Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        if not user.clan_id or not user.clan:
            await message.answer(
                "🛡️ <b>МОЙ КЛАН</b>\n\n"
                "❌ Вы не состоите в клане.\n\n"
                "Создайте его в чате командой:\n"
                "<code>создать клан Название</code>\n\n"
                "Или попросите лидера другого клана принять вас!",
                parse_mode="HTML",
            )
            return

        clan = user.clan
        total_elo = await recalc_clan_elo(session, clan.id)
        member_count_q = await session.execute(
            select(func.count(User.id)).where(User.clan_id == clan.id)
        )
        member_count = member_count_q.scalar()

        leader_q = await session.execute(
            select(User.username).where(User.tg_id == clan.leader_id)
        )
        leader_username = leader_q.scalar() or "—"

        await session.commit()

    role_display = "👑 Лидер" if user.clan_role == "leader" else "🙋 Участник"

    await message.answer(
        f"🛡️ <b>ВАШ КЛАН: {clan.name}</b>\n\n"
        f"👑 Лидер: @{leader_username}\n"
        f"🎖 Ваш статус: <b>{role_display}</b>\n"
        f"👥 Участников: <b>{member_count}</b>\n"
        f"🏆 Общий ELO-рейтинг клана: <b>{total_elo:,}</b>",
        parse_mode="HTML",
    )


# ── 🔗 Пригласить друга ──────────────────────────────────────────────────────

@private_router.message(F.text == "🔗 Пригласить друга")
async def pm_referral(message: types.Message, bot: Bot) -> None:
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"

    async with async_session() as session:
        count_q = await session.execute(
            select(func.count(User.id)).where(User.referred_by == message.from_user.id)
        )
        invited_count = count_q.scalar()

    await message.answer(
        f"🎁 <b>ПРИГЛАШАЙ ДРУЗЕЙ И ПОЛУЧАЙ STARS!</b>\n\n"
        f"Отправь эту ссылку другу. Когда он запустит бота, "
        f"вы <b>ОБА</b> получите по <b>{REFERRAL_BONUS:,} ⭐ Dark Stars</b>!\n\n"
        f"🔗 Ваша реферальная ссылка:\n"
        f"<code>{ref_link}</code>\n\n"
        f"👥 Всего приглашено друзей: <b>{invited_count}</b>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#
#                    💎 МАГАЗИН / ДОНАТ (Telegram Stars)
#
# ══════════════════════════════════════════════════════════════════════════════


@private_router.message(F.text == "💎 Магазин/Донат")
async def pm_shop(message: types.Message, state: FSMContext) -> None:
    await state.clear()

    buttons = []
    for payload, (ds, xtr, label) in SHOP_ITEMS.items():
        buttons.append([types.InlineKeyboardButton(
            text=label,
            callback_data=f"shop:{payload}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="🔮 Кастомная сумма ✏️",
        callback_data="shop:buy_custom",
    )])

    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        "💎 <b>МАГАЗИН / ДОНАТ</b>\n\n"
        f"💱 Курс: <b>1 Telegram Star = {DARK_STARS_PER_XTR:,} Dark Stars</b>\n\n"
        "Выбери пакет или введи свою сумму:",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── Покупка готового пакета ──────────────────────────────────────────────────

@private_router.callback_query(F.data.startswith("shop:buy_stars_"))
async def cb_shop_buy_pack(callback: types.CallbackQuery, bot: Bot) -> None:
    payload = callback.data.split(":", 1)[1]

    if payload not in SHOP_ITEMS:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return

    dark_stars, xtr_price, label = SHOP_ITEMS[payload]

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Покупка {dark_stars:,} Dark Stars",
        description=f"Вы получите {dark_stars:,} ⭐ Dark Stars на свой баланс в LARP Case Bot.",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=xtr_price)],
    )
    await callback.answer()


# ── Кастомная сумма: начало FSM ──────────────────────────────────────────────

@private_router.callback_query(F.data == "shop:buy_custom")
async def cb_shop_custom_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DonateState.waiting_for_amount)
    await callback.message.edit_text(
        "🔮 <b>КАСТОМНАЯ СУММА</b>\n\n"
        f"Курс: <b>1 Telegram Star = {DARK_STARS_PER_XTR} Dark Stars</b>\n\n"
        f"Введите количество Dark Stars (⭐), которое хотите приобрести\n"
        f"(минимум <b>{MIN_CUSTOM_AMOUNT:,} ⭐</b>):",
        parse_mode="HTML",
    )
    await callback.answer()


# ── Кастомная сумма: получение числа ─────────────────────────────────────────

@private_router.message(DonateState.waiting_for_amount, F.text)
async def fsm_donate_amount(message: types.Message, state: FSMContext, bot: Bot) -> None:
    raw = message.text.strip().replace(" ", "").replace(",", "")

    if not raw.isdigit():
        await message.answer(
            f"❌ Введи целое число (минимум {MIN_CUSTOM_AMOUNT:,}).\n"
            "Попробуй ещё раз или нажми /start для отмены.",
            parse_mode="HTML",
        )
        return

    amount = int(raw)

    if amount < MIN_CUSTOM_AMOUNT:
        await message.answer(
            f"❌ Минимальная сумма — <b>{MIN_CUSTOM_AMOUNT:,} ⭐ Dark Stars</b>.\n"
            "Введи число побольше:",
            parse_mode="HTML",
        )
        return

    stars_cost = max(1, math.ceil(amount / DARK_STARS_PER_XTR))

    # Telegram Stars: минимум 1, максимум 10000
    if stars_cost > 10000:
        await message.answer(
            "❌ Слишком большая сумма! Максимум <b>250 000 ⭐ Dark Stars</b> за раз.",
            parse_mode="HTML",
        )
        return

    await state.clear()

    payload = f"custom_{amount}"

    await bot.send_invoice(
        chat_id=message.from_user.id,
        title=f"Покупка {amount:,} Dark Stars",
        description=(
            f"Вы получите {amount:,} ⭐ Dark Stars на свой баланс.\n"
            f"Стоимость: {stars_cost} Telegram Stars."
        ),
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(
            label=f"{amount:,} Dark Stars",
            amount=stars_cost,
        )],
    )


# ── Pre-checkout (всегда одобряем) ───────────────────────────────────────────

@private_router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


# ── Успешная оплата ──────────────────────────────────────────────────────────

@private_router.message(F.successful_payment)
async def on_successful_payment(message: types.Message) -> None:
    payload = message.successful_payment.invoice_payload
    xtr_paid = message.successful_payment.total_amount

    # Определяем сколько Dark Stars начислить
    if payload in SHOP_ITEMS:
        # Готовый пакет
        dark_stars = SHOP_ITEMS[payload][0]
    elif payload.startswith("custom_"):
        # Кастомная сумма
        try:
            dark_stars = int(payload.split("_", 1)[1])
        except (ValueError, IndexError):
            await message.answer("⚠️ Ошибка обработки платежа. Обратитесь к администратору.")
            return
    else:
        await message.answer("⚠️ Неизвестный платёж. Обратитесь к администратору.")
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        user.dark_stars += dark_stars
        await session.commit()
        new_balance = user.dark_stars

    await message.answer(
        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
        f"Вам начислено <b>+{dark_stars:,} ⭐ Dark Stars</b>\n"
        f"Оплачено: <b>{xtr_paid} Telegram Stars</b>\n\n"
        f"💰 Текущий баланс: <b>{new_balance:,} ⭐</b>\n\n"
        f"Время крутить кейсы на полную мощность! 🚀\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#
#                        ЛС: бонус, инвентарь, промо
#
# ══════════════════════════════════════════════════════════════════════════════


@private_router.message(F.text.casefold().in_({"бонус", "ежедневный"}))
async def pm_daily(message: types.Message) -> None:
    await _handle_daily(message)


@private_router.message(F.text.casefold() == "инвентарь")
async def pm_inventory(message: types.Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        result = await session.execute(
            select(InventoryItem)
            .where(InventoryItem.user_id == user.tg_id)
            .order_by(InventoryItem.market_value.desc())
        )
        items = result.scalars().all()

    if not items:
        await message.answer(
            "🎒 Твой инвентарь пуст!\n\n"
            "Открой кейс в групповом чате: <code>открыть мемный</code>",
            parse_mode="HTML",
        )
        return

    lines: list[str] = ["🎒 <b>ТВОЙ ИНВЕНТАРЬ</b>\n"]
    total = 0
    for idx, item in enumerate(items, 1):
        emoji = RARITY_EMOJI.get(item.rarity, "⚪")
        lines.append(
            f"  {idx}. {emoji} <b>{item.item_name}</b> "
            f"[{item.rarity}] — {item.market_value:,} ⭐"
        )
        total += item.market_value

    lines.append(f"\n💰 <b>Итого: {total:,} ⭐</b>  ({len(items)} шт.)")

    inv_kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="💰 Продать предмет", callback_data="inv_sell_item_menu"),
            types.InlineKeyboardButton(text="💥 Продать ВСЁ", callback_data="inv_sell_all_confirm"),
        ],
        [
            types.InlineKeyboardButton(text="🔄 Трейды (Coming Soon)", callback_data="inv_trades_soon"),
        ],
    ])

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=inv_kb)


@private_router.message(F.text.casefold().startswith("промо "))
async def pm_promo(message: types.Message) -> None:
    await _handle_promo(message)


# ══════════════════════════════════════════════════════════════════════════════
#
#                 💰 ПРОДАЖА ПРЕДМЕТОВ (ЛС — Inline + FSM)
#
# ══════════════════════════════════════════════════════════════════════════════


# ── Трейды (заглушка) ────────────────────────────────────────────────────────

@private_router.callback_query(F.data == "inv_trades_soon")
async def cb_trades_soon(callback: types.CallbackQuery) -> None:
    await callback.answer(
        "🔄 Система обмена (Трейды) находится в разработке и появится "
        "в следующем крупном обновлении! Следите за новостями в @larpcase 🔥",
        show_alert=True,
    )


# ── Продать предмет: начало FSM ─────────────────────────────────────────────

@private_router.callback_query(F.data == "inv_sell_item_menu")
async def cb_sell_item_menu(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InventoryState.waiting_for_sell_id)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "💰 <b>ПРОДАЖА ПРЕДМЕТА</b>\n\n"
        "Введите <b>порядковый номер</b> предмета из вашего инвентаря, "
        "который вы хотите продать:\n\n"
        "<i>(Номера видны в списке инвентаря выше)</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@private_router.message(InventoryState.waiting_for_sell_id, F.text)
async def fsm_sell_item(message: types.Message, state: FSMContext) -> None:
    raw = message.text.strip()

    if not raw.isdigit():
        await message.answer(
            "❌ Введи номер предмета (число).\n"
            "Или напиши <code>инвентарь</code> чтобы посмотреть список.",
            parse_mode="HTML",
        )
        return

    item_index = int(raw)

    if item_index < 1:
        await message.answer("❌ Номер должен быть больше 0!")
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        result = await session.execute(
            select(InventoryItem)
            .where(InventoryItem.user_id == user.tg_id)
            .order_by(InventoryItem.market_value.desc())
        )
        items = result.scalars().all()

        if not items:
            await state.clear()
            await message.answer("🎒 Инвентарь пуст! Нечего продавать.")
            return

        if item_index > len(items):
            await message.answer(
                f"❌ У тебя всего <b>{len(items)}</b> предметов. "
                f"Введи число от 1 до {len(items)}.",
                parse_mode="HTML",
            )
            return

        item = items[item_index - 1]
        item_name = item.item_name
        item_value = item.market_value
        item_rarity = item.rarity

        # Удаляем предмет
        await session.delete(item)
        # Начисляем стоимость
        user.dark_stars += item_value
        await session.commit()
        new_balance = user.dark_stars

    await state.clear()

    emoji = RARITY_EMOJI.get(item_rarity, "⚪")
    await message.answer(
        f"✅ <b>ПРЕДМЕТ ПРОДАН!</b>\n\n"
        f"{emoji} <b>{item_name}</b> [{item_rarity}]\n"
        f"💰 Получено: <b>+{item_value:,} ⭐</b>\n"
        f"💼 Баланс: <b>{new_balance:,} ⭐</b>\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥",
        parse_mode="HTML",
    )


# ── Продать ВСЁ: подтверждение ──────────────────────────────────────────────

@private_router.callback_query(F.data == "inv_sell_all_confirm")
async def cb_sell_all_confirm(callback: types.CallbackQuery) -> None:
    confirm_kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Да, продать всё", callback_data="inv_sell_all_yes"),
            types.InlineKeyboardButton(text="❌ Отмена", callback_data="inv_sell_all_no"),
        ],
    ])

    await callback.message.edit_text(
        "⚠️ <b>Вы уверены, что хотите продать ВСЕ предметы "
        "из вашего инвентаря?</b>\n\n"
        "Это действие <b>необратимо</b>!",
        parse_mode="HTML",
        reply_markup=confirm_kb,
    )
    await callback.answer()


@private_router.callback_query(F.data == "inv_sell_all_no")
async def cb_sell_all_no(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "❌ <b>Продажа отменена.</b>\n\n"
        "Напиши <code>инвентарь</code> чтобы открыть список предметов.",
        parse_mode="HTML",
    )
    await callback.answer("Отменено")


@private_router.callback_query(F.data == "inv_sell_all_yes")
async def cb_sell_all_yes(callback: types.CallbackQuery) -> None:
    async with async_session() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )

        # Считаем сумму и количество
        stats_q = await session.execute(
            select(
                func.count(InventoryItem.id),
                func.coalesce(func.sum(InventoryItem.market_value), 0),
            ).where(InventoryItem.user_id == user.tg_id)
        )
        item_count, total_value = stats_q.one()

        if item_count == 0:
            await callback.message.edit_text(
                "🎒 У вас нет предметов для продажи!",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        # Удаляем ВСЕ предметы
        all_items_q = await session.execute(
            select(InventoryItem).where(InventoryItem.user_id == user.tg_id)
        )
        all_items = all_items_q.scalars().all()
        for item in all_items:
            await session.delete(item)

        # Начисляем
        user.dark_stars += total_value
        await session.commit()
        new_balance = user.dark_stars

    await callback.message.edit_text(
        f"💥 <b>БАБАХ! ВСЁ ПРОДАНО!</b>\n\n"
        f"🎒 Продано предметов: <b>{item_count}</b>\n"
        f"💰 Получено: <b>+{total_value:,} ⭐</b>\n"
        f"💼 Баланс: <b>{new_balance:,} ⭐</b>\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥",
        parse_mode="HTML",
    )
    await callback.answer(f"💥 Продано {item_count} предметов!")


# ── ЛС: лидерборд ───────────────────────────────────────────────────────────

@private_router.message(F.text.casefold().in_({"топ", "лидерборд"}))
async def pm_leaderboard(message: types.Message) -> None:
    await _handle_leaderboard(message)


# ══════════════════════════════════════════════════════════════════════════════
#
#                       ГРУППЫ  (Group / Supergroup)
#
# ══════════════════════════════════════════════════════════════════════════════


# ── /start — активация ───────────────────────────────────────────────────────

@group_router.message(Command("start"))
async def group_activate(message: types.Message) -> None:
    chat_id = message.chat.id

    if chat_id in activated_chats:
        await message.answer("✅ Бот уже активирован в этом чате! Пишите команды 🎮", parse_mode="HTML")
        return

    if not await is_group_admin(message):
        await message.answer(
            "⛔ Только <b>админ группы</b> может активировать бота.\n"
            "Попроси админа написать /start",
            parse_mode="HTML",
        )
        return

    activated_chats.add(chat_id)
    await message.answer(
        "🚀 <b>Бот успешно активирован в этом чате!</b>\n"
        "Начинаем лудоманское безумие!\n\n"
        "Доступные команды:\n"
        "  📋  <code>профиль</code> / <code>б</code>\n"
        "  🎁  <code>бонус</code>\n"
        "  📦  <code>кейсы</code>\n"
        "  🎰  <code>открыть мемный</code> / <code>открыть кит</code>\n"
        "  🎒  <code>инвентарь</code>\n"
        "  🏆  <code>топ</code> / <code>лидерборд</code>\n"
        "  💸  <code>п 100</code> (ответом)\n"
        "  ⚔️  <code>дуэль</code> (ответом)\n"
        "  🎰  <code>слот 500</code> / <code>казино 1000</code>\n"
        "  💣  <code>мины 500</code>\n"
        "  🎟  <code>промо КОД</code>\n"
        "  🛡  <code>создать клан Название</code>\n"
        "  🛡  <code>вступить в клан Название</code>\n"
        "  🛡  <code>принять в клан</code> (ответом)\n"
        "  🛡  <code>выйти из клана</code>",
        parse_mode="HTML",
    )


# ── Фильтр активации ────────────────────────────────────────────────────────

def _activated(handler):
    """Декоратор: молча игнорирует, если чат не активирован."""
    from functools import wraps

    @wraps(handler)
    async def wrapper(message: types.Message, **kwargs):
        if message.chat.id not in activated_chats:
            return
        return await handler(message, **kwargs)
    return wrapper


# ── Группа: профиль (краткий) ────────────────────────────────────────────────

@group_router.message(F.text.casefold().in_({"профиль", "мой профиль", "б"}))
@_activated
async def group_profile(message: types.Message, **kwargs) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        inv_q = await session.execute(
            select(
                func.count(InventoryItem.id),
                func.coalesce(func.sum(InventoryItem.market_value), 0),
            ).where(InventoryItem.user_id == user.tg_id)
        )
        item_count, total_value = inv_q.one()
        clan_name = user.clan.name if user.clan else "—"
        await session.commit()

    await message.answer(
        f"📋 <b>{message.from_user.full_name}</b>\n\n"
        f"⭐ Dark Stars: <b>{user.dark_stars:,}</b>\n"
        f"🏆 ELO: <b>{user.elo_rating}</b>\n"
        f"🏰 Клан: <b>{clan_name}</b>\n"
        f"🎒 Предметов: <b>{item_count}</b> (на <b>{total_value:,} ⭐</b>)\n\n"
        f"<i>Полный инвентарь — в ЛС бота</i>\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥",
        parse_mode="HTML",
    )


# ── Группа: бонус ────────────────────────────────────────────────────────────

@group_router.message(F.text.casefold().in_({"ежедневный", "бонус"}))
@_activated
async def group_daily(message: types.Message, **kwargs) -> None:
    await _handle_daily(message)


# ── Группа: кейсы (список) ──────────────────────────────────────────────────

@group_router.message(F.text.casefold().in_({"кейсы", "кейс"}))
@_activated
async def group_cases(message: types.Message, **kwargs) -> None:
    await message.answer(
        "🗂 <b>ДОСТУПНЫЕ КЕЙСЫ</b>\n\n"
        "📦  <b>Мемный Кейс</b> — <b>500 ⭐</b>\n"
        "      Команда: <code>открыть мемный</code>\n\n"
        "💎  <b>NFT Кит Кейс</b> — <b>2 500 ⭐</b>\n"
        "      Команда: <code>открыть кит</code>\n\n"
        "Удачи, дегенерат! 🎰",
        parse_mode="HTML",
    )


# ── Группа: открытие кейсов ─────────────────────────────────────────────────

@group_router.message(F.text.casefold().in_({"открыть мемный", "открыть кит"}))
@_activated
async def group_open_case(message: types.Message, **kwargs) -> None:
    case_key = message.text.strip().lower().split(maxsplit=1)[1]
    price = CASE_PRICES[case_key]

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        if user.dark_stars < price:
            deficit = price - user.dark_stars
            await message.answer(
                f"❌ <b>Недостаточно Dark Stars!</b>\n\n"
                f"{tag(message.from_user)}, не хватает <b>{deficit:,} ⭐</b>\n"
                f"Баланс: <b>{user.dark_stars:,} ⭐</b>\n\n"
                f"Напиши <code>бонус</code> или купи в ЛС бота 💎",
                parse_mode="HTML",
            )
            return

        user.dark_stars -= price

        rarity = roll_rarity()
        loot = random.choice(NFT_DATASET[rarity])
        item_name = loot["name"]
        item_value = loot["value"]

        session.add(InventoryItem(
            user_id=user.tg_id,
            item_name=item_name,
            rarity=rarity,
            market_value=item_value,
        ))
        await session.commit()

    spinning = await message.answer(
        f"🎰 <b>{tag(message.from_user)} КРУТИТ КЕЙС...</b>",
        parse_mode="HTML",
    )
    await asyncio.sleep(0.6)

    emoji = RARITY_EMOJI.get(rarity, "⚪")
    result_text = (
        f"🎰 <b>КЕЙС ОТКРЫТ!</b>\n\n"
        f"🎮 Игрок: {tag(message.from_user)}\n"
        f"Редкость: {emoji} <b>{rarity}</b>\n"
        f"Предмет: <b>{item_name}</b>\n"
        f"Стоимость: <b>{item_value:,} ⭐</b>\n\n"
        f"💼 Предмет добавлен в инвентарь!\n"
        f"Остаток: <b>{user.dark_stars:,} ⭐</b>\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥"
    )

    if rarity in ("Legendary", "Mythic"):
        result_text = f"🔥🔥🔥 <b>ДЖЕКПОТ!</b> 🔥🔥🔥\n\n" + result_text

    await spinning.edit_text(result_text, parse_mode="HTML")


# ── Группа: инвентарь (краткий, последние 10) ────────────────────────────────

@group_router.message(F.text.casefold() == "инвентарь")
@_activated
async def group_inventory(message: types.Message, **kwargs) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        result = await session.execute(
            select(InventoryItem)
            .where(InventoryItem.user_id == user.tg_id)
            .order_by(InventoryItem.id.desc())
            .limit(10)
        )
        items = result.scalars().all()

        total_q = await session.execute(
            select(
                func.count(InventoryItem.id),
                func.coalesce(func.sum(InventoryItem.market_value), 0),
            ).where(InventoryItem.user_id == user.tg_id)
        )
        total_count, total_value = total_q.one()

    if not items:
        await message.answer(
            f"🎒 {tag(message.from_user)}, инвентарь пуст!\n"
            "Открой кейс: <code>открыть мемный</code>",
            parse_mode="HTML",
        )
        return

    lines: list[str] = [f"🎒 <b>ИНВЕНТАРЬ {tag(message.from_user)}</b>\n"]
    for idx, item in enumerate(items, 1):
        emoji = RARITY_EMOJI.get(item.rarity, "⚪")
        lines.append(f"  {idx}. {emoji} <b>{item.item_name}</b> [{item.rarity}] — {item.market_value:,} ⭐")

    lines.append(f"\n💰 <b>Итого: {total_value:,} ⭐</b>  ({total_count} шт.)")
    if total_count > 10:
        lines.append(f"\n<i>Показаны последние 10. Полный список — в ЛС бота.</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ── Группа: промокод ────────────────────────────────────────────────────────

@group_router.message(F.text.casefold().startswith("промо "))
@_activated
async def group_promo(message: types.Message, **kwargs) -> None:
    await _handle_promo(message)


# ── Группа: лидерборд ───────────────────────────────────────────────────────

@group_router.message(F.text.casefold().in_({"топ", "лидерборд"}))
@_activated
async def group_leaderboard(message: types.Message, **kwargs) -> None:
    await _handle_leaderboard(message)


# ── Группа: перевод ─────────────────────────────────────────────────────────

@group_router.message(F.text.casefold().regexp(r"^(п|передать)\s+\d+$"))
@_activated
async def group_transfer(message: types.Message, **kwargs) -> None:
    if not message.reply_to_message:
        await message.answer("↩️ Ответь на сообщение человека, которому хочешь передать ⭐!", parse_mode="HTML")
        return

    target = message.reply_to_message
    if target.from_user.is_bot:
        await message.answer("🤖 Нельзя переводить Dark Stars боту!")
        return
    if target.from_user.id == message.from_user.id:
        await message.answer("🤦 Нельзя переводить самому себе!")
        return

    amount = int(message.text.strip().split()[1])
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0!")
        return

    async with async_session() as session:
        sender = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        receiver = await get_or_create_user(session, target.from_user.id, target.from_user.username)

        if sender.dark_stars < amount:
            await message.answer(
                f"❌ Недостаточно средств!\n"
                f"Баланс: <b>{sender.dark_stars:,} ⭐</b>, нужно: <b>{amount:,} ⭐</b>",
                parse_mode="HTML",
            )
            return

        sender.dark_stars -= amount
        receiver.dark_stars += amount
        await session.commit()

    await message.answer(
        f"🤝 <b>УСПЕШНЫЙ ПЕРЕВОД!</b>\n\n"
        f"{tag(message.from_user)} перевёл <b>{amount:,} ⭐</b> для {tag(target.from_user)}!\n\n"
        f"💸 {tag(message.from_user)}: <b>{sender.dark_stars:,} ⭐</b>\n"
        f"💰 {tag(target.from_user)}: <b>{receiver.dark_stars:,} ⭐</b>",
        parse_mode="HTML",
    )


# ── Группа: дуэль ───────────────────────────────────────────────────────────

@group_router.message(F.text.casefold() == "дуэль")
@_activated
async def group_duel(message: types.Message, **kwargs) -> None:
    if not message.reply_to_message:
        await message.answer("⚔️ Ответь на сообщение противника, чтобы вызвать его на дуэль!", parse_mode="HTML")
        return

    target = message.reply_to_message
    if target.from_user.is_bot:
        await message.answer("🤖 Нельзя драться с ботом!")
        return
    if target.from_user.id == message.from_user.id:
        await message.answer("🤦 Нельзя вызвать себя на дуэль!")
        return

    attacker_id = message.from_user.id
    defender_id = target.from_user.id

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="⚔️ Принять",
                callback_data=f"accept_duel_{attacker_id}_{defender_id}",
            ),
            types.InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"decline_duel_{attacker_id}_{defender_id}",
            ),
        ],
    ])

    await message.answer(
        f"⚔️ <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"🔹 {tag(message.from_user)} вызывает на дуэль {tag(target.from_user)}!\n\n"
        f"У вас есть <b>60 секунд</b>, чтобы принять вызов.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── Callback: принять / отклонить дуэль ─────────────────────────────────────

@group_router.callback_query(F.data.startswith("accept_duel_"))
async def cb_accept_duel(callback: types.CallbackQuery) -> None:
    parts = callback.data.split("_")
    # accept_duel_{attacker_id}_{defender_id}
    attacker_id = int(parts[2])
    defender_id = int(parts[3])

    # Только защитник может принять
    if callback.from_user.id != defender_id:
        await callback.answer("❌ Это не ваш вызов!", show_alert=True)
        return

    # ── Расчёт дуэли ──
    async with async_session() as session:
        p1 = await get_or_create_user(session, attacker_id)
        p2 = await get_or_create_user(session, defender_id, callback.from_user.username)

        inv1 = (await session.execute(
            select(func.coalesce(func.sum(InventoryItem.market_value), 0))
            .where(InventoryItem.user_id == p1.tg_id)
        )).scalar()

        inv2 = (await session.execute(
            select(func.coalesce(func.sum(InventoryItem.market_value), 0))
            .where(InventoryItem.user_id == p2.tg_id)
        )).scalar()

        p1_chance = 0.65 if inv1 >= inv2 else 0.35
        p1_wins = random.random() < p1_chance

        if p1_wins:
            winner, loser = p1, p2
            w_inv, l_inv = inv1, inv2
        else:
            winner, loser = p2, p1
            w_inv, l_inv = inv2, inv1

        winner.elo_rating += ELO_CHANGE
        loser.elo_rating -= ELO_CHANGE

        if winner.clan_id:
            await recalc_clan_elo(session, winner.clan_id)
        if loser.clan_id and loser.clan_id != winner.clan_id:
            await recalc_clan_elo(session, loser.clan_id)

        await session.commit()

    w_name = f"@{winner.username}" if winner.username else f"ID:{winner.tg_id}"
    l_name = f"@{loser.username}" if loser.username else f"ID:{loser.tg_id}"

    battle_log = (
        f"⚔️ <b>ДУЭЛЬ ПРИНЯТА!</b>\n\n"
        f"🔹 {w_name} (инв. {w_inv:,} ⭐)\n"
        f"    <i>vs</i>\n"
        f"🔸 {l_name} (инв. {l_inv:,} ⭐)\n\n"
        f"━━━━━━━ ⚔️ БОЙ ━━━━━━━\n\n"
        f"🟢 {w_name} {random.choice(BATTLE_WINNER)}\n"
        f"🔴 {l_name} {random.choice(BATTLE_LOSER)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏆 <b>Победитель: {w_name}!</b>\n"
        f"   📈 ELO: <b>+{ELO_CHANGE}</b> → <b>{winner.elo_rating}</b>\n\n"
        f"💀 <b>Проигравший: {l_name}</b>\n"
        f"   📉 ELO: <b>-{ELO_CHANGE}</b> → <b>{loser.elo_rating}</b>\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥"
    )

    await callback.message.edit_text(battle_log, parse_mode="HTML")
    await callback.answer("⚔️ Дуэль началась!")


@group_router.callback_query(F.data.startswith("decline_duel_"))
async def cb_decline_duel(callback: types.CallbackQuery) -> None:
    parts = callback.data.split("_")
    attacker_id = int(parts[2])
    defender_id = int(parts[3])

    # Отклонить может защитник ИЛИ атакующий (отмена своего вызова)
    if callback.from_user.id not in (attacker_id, defender_id):
        await callback.answer("❌ Это не ваш вызов!", show_alert=True)
        return

    who = "Вызов отменён" if callback.from_user.id == attacker_id else "Дуэль отклонена"
    await callback.message.edit_text(
        f"❌ <b>{who}!</b>",
        parse_mode="HTML",
    )
    await callback.answer(who)


# ══════════════════════════════════════════════════════════════════════════════
#
#                      🛡️ КЛАНЫ (Групповые команды)
#
# ══════════════════════════════════════════════════════════════════════════════


# ── Создать клан ─────────────────────────────────────────────────────────────

@group_router.message(F.text.casefold().startswith("создать клан "))
@_activated
async def group_create_clan(message: types.Message, **kwargs) -> None:
    clan_name = message.text.strip()[len("создать клан "):].strip()

    if not clan_name:
        await message.answer("❌ Укажи название клана: <code>создать клан Название</code>", parse_mode="HTML")
        return

    if len(clan_name) > 64:
        await message.answer("❌ Название клана слишком длинное (макс. 64 символа)!")
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        if user.clan_id:
            await message.answer("⚠️ Ты уже состоишь в клане! Сначала выйди: <code>выйти из клана</code>", parse_mode="HTML")
            return

        if user.dark_stars < CLAN_CREATION_COST:
            deficit = CLAN_CREATION_COST - user.dark_stars
            await message.answer(
                f"❌ Создание клана стоит <b>{CLAN_CREATION_COST:,} ⭐</b>\n"
                f"Не хватает: <b>{deficit:,} ⭐</b>\n"
                f"Твой баланс: <b>{user.dark_stars:,} ⭐</b>",
                parse_mode="HTML",
            )
            return

        exists = await session.execute(
            select(Clan).where(Clan.name == clan_name)
        )
        if exists.scalar_one_or_none():
            await message.answer(f"⚠️ Клан с названием «{clan_name}» уже существует!", parse_mode="HTML")
            return

        user.dark_stars -= CLAN_CREATION_COST

        clan = Clan(name=clan_name, leader_id=user.tg_id, total_elo=user.elo_rating)
        session.add(clan)
        await session.flush()

        user.clan_id = clan.id
        user.clan_role = "leader"
        await session.commit()

    await message.answer(
        f"🛡️ <b>Клан «{clan_name}» успешно создан!</b>\n\n"
        f"👑 Лидер: {tag(message.from_user)}\n"
        f"💰 Списано: <b>{CLAN_CREATION_COST:,} ⭐</b>\n\n"
        f"Принимай участников: ответь на сообщение юзера командой\n"
        f"<code>принять в клан</code>",
        parse_mode="HTML",
    )


# ── Принять в клан ───────────────────────────────────────────────────────────

@group_router.message(F.text.casefold() == "принять в клан")
@_activated
async def group_clan_invite(message: types.Message, **kwargs) -> None:
    if not message.reply_to_message:
        await message.answer("↩️ Ответь на сообщение человека, которого хочешь принять в клан!", parse_mode="HTML")
        return

    target = message.reply_to_message
    if target.from_user.is_bot:
        await message.answer("🤖 Нельзя принять бота в клан!")
        return
    if target.from_user.id == message.from_user.id:
        await message.answer("🤦 Нельзя принять самого себя!")
        return

    async with async_session() as session:
        leader = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        if not leader.clan_id or leader.clan_role != "leader":
            await message.answer("⛔ Только <b>лидер клана</b> может принимать участников!", parse_mode="HTML")
            return

        new_member = await get_or_create_user(session, target.from_user.id, target.from_user.username)

        if new_member.clan_id:
            await message.answer(
                f"⚠️ {tag(target.from_user)} уже состоит в клане!",
                parse_mode="HTML",
            )
            return

        new_member.clan_id = leader.clan_id
        new_member.clan_role = "member"

        await recalc_clan_elo(session, leader.clan_id)
        await session.commit()

        clan = leader.clan

    await message.answer(
        f"🛡️ <b>{tag(target.from_user)} принят в клан «{clan.name}»!</b>\n\n"
        f"👑 Лидер: {tag(message.from_user)}\n"
        f"👥 Добро пожаловать!",
        parse_mode="HTML",
    )


# ── Выйти из клана ──────────────────────────────────────────────────────────

@group_router.message(F.text.casefold() == "выйти из клана")
@_activated
async def group_clan_leave(message: types.Message, **kwargs) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        if not user.clan_id:
            await message.answer("❌ Ты не состоишь в клане!", parse_mode="HTML")
            return

        clan_id = user.clan_id
        clan_name = user.clan.name if user.clan else "?"
        is_leader = user.clan_role == "leader"

        if is_leader:
            members_q = await session.execute(
                select(User).where(User.clan_id == clan_id, User.tg_id != user.tg_id)
            )
            other_members = members_q.scalars().all()

            for m in other_members:
                m.clan_id = None
                m.clan_role = None

            user.clan_id = None
            user.clan_role = None

            await session.execute(
                select(Clan).where(Clan.id == clan_id)
            )
            clan_obj = (await session.execute(
                select(Clan).where(Clan.id == clan_id)
            )).scalar_one_or_none()
            if clan_obj:
                await session.delete(clan_obj)

            await session.commit()

            kicked_count = len(other_members)
            await message.answer(
                f"🛡️ <b>Клан «{clan_name}» расформирован!</b>\n\n"
                f"👑 Лидер {tag(message.from_user)} покинул клан.\n"
                f"👥 Исключено участников: <b>{kicked_count}</b>",
                parse_mode="HTML",
            )
        else:
            user.clan_id = None
            user.clan_role = None
            await recalc_clan_elo(session, clan_id)
            await session.commit()

            await message.answer(
                f"🛡️ {tag(message.from_user)} покинул клан «{clan_name}».",
                parse_mode="HTML",
            )


# ── Вступить в клан (заявка) ──────────────────────────────────────────────────

@group_router.message(F.text.casefold().startswith("вступить в клан "))
@_activated
async def group_clan_join(message: types.Message, bot: Bot, **kwargs) -> None:
    clan_name = message.text.strip()[len("вступить в клан "):].strip()

    if not clan_name:
        await message.answer(
            "❌ Укажи название клана: <code>вступить в клан Название</code>",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        if user.clan_id:
            await message.answer(
                "⚠️ Ты уже состоишь в клане! Сначала выйди: <code>выйти из клана</code>",
                parse_mode="HTML",
            )
            return

        # Ищем клан
        clan_q = await session.execute(
            select(Clan).where(Clan.name == clan_name)
        )
        clan = clan_q.scalar_one_or_none()

        if clan is None:
            await message.answer(
                f"❌ Клан «{clan_name}» не найден!",
                parse_mode="HTML",
            )
            return

        # Проверяем, нет ли уже pending-заявки
        existing_q = await session.execute(
            select(ClanJoinRequest).where(
                ClanJoinRequest.user_id == user.tg_id,
                ClanJoinRequest.clan_id == clan.id,
                ClanJoinRequest.status == "pending",
            )
        )
        if existing_q.scalar_one_or_none():
            await message.answer(
                "⏳ У тебя уже есть активная заявка в этот клан! Ожидай решения лидера.",
                parse_mode="HTML",
            )
            return

        # Создаём заявку
        request = ClanJoinRequest(
            user_id=user.tg_id,
            clan_id=clan.id,
            status="pending",
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        leader_id = clan.leader_id
        clan_name_db = clan.name

        await session.commit()

    await message.answer(
        f"⏳ Ваша заявка на вступление в клан «<b>{clan_name_db}</b>» "
        f"отправлена лидеру! Ожидайте решения.\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥",
        parse_mode="HTML",
    )

    # Уведомляем лидера
    display = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="✅ Принять",
                callback_data=f"clan_req_accept_{request_id}",
            ),
            types.InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"clan_req_reject_{request_id}",
            ),
        ],
    ])

    try:
        await bot.send_message(
            chat_id=leader_id,
            text=(
                f"🛡️ <b>НОВАЯ ЗАЯВКА В КЛАН!</b>\n\n"
                f"Игрок: {display}\n"
                f"ID: <code>{message.from_user.id}</code>\n"
                f"⭐ Баланс: <b>{user.dark_stars:,}</b>\n"
                f"🏆 ELO: <b>{user.elo_rating}</b>\n\n"
                f"Хотите принять его в клан «<b>{clan_name_db}</b>»?\n\n"
                f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
                f"Подписывайся! 🔥"
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        pass  # Лидер мог заблокировать бота


# ── Callback: лидер принимает/отклоняет заявку ───────────────────────────────

@private_router.callback_query(F.data.startswith("clan_req_accept_"))
async def cb_clan_req_accept(callback: types.CallbackQuery, bot: Bot) -> None:
    request_id = int(callback.data.split("_")[-1])

    async with async_session() as session:
        req_q = await session.execute(
            select(ClanJoinRequest).where(ClanJoinRequest.id == request_id)
        )
        request = req_q.scalar_one_or_none()

        if request is None or request.status != "pending":
            await callback.answer("⚠️ Эта заявка уже обработана!", show_alert=True)
            return

        # Проверяем, что кнопку жмёт именно лидер
        clan_q = await session.execute(
            select(Clan).where(Clan.id == request.clan_id)
        )
        clan = clan_q.scalar_one_or_none()

        if clan is None:
            await callback.answer("❌ Клан больше не существует!", show_alert=True)
            request.status = "rejected"
            await session.commit()
            return

        if callback.from_user.id != clan.leader_id:
            await callback.answer("⛔ Только лидер клана может обрабатывать заявки!", show_alert=True)
            return

        # Находим заявителя
        user_q = await session.execute(
            select(User).where(User.tg_id == request.user_id)
        )
        applicant = user_q.scalar_one_or_none()

        if applicant is None:
            request.status = "rejected"
            await session.commit()
            await callback.answer("❌ Игрок не найден!", show_alert=True)
            return

        if applicant.clan_id:
            request.status = "rejected"
            await session.commit()
            await callback.message.edit_text(
                "⚠️ Игрок уже вступил в другой клан. Заявка отклонена автоматически.",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        # Принимаем
        request.status = "accepted"
        applicant.clan_id = clan.id
        applicant.clan_role = "member"
        await recalc_clan_elo(session, clan.id)
        await session.commit()

        applicant_display = f"@{applicant.username}" if applicant.username else f"ID:{applicant.tg_id}"
        clan_name = clan.name

    await callback.message.edit_text(
        f"✅ <b>Игрок {applicant_display} успешно принят в клан «{clan_name}»!</b>",
        parse_mode="HTML",
    )
    await callback.answer("✅ Принят!")

    # Уведомляем игрока
    try:
        await bot.send_message(
            chat_id=request.user_id,
            text=(
                f"🎉 <b>Поздравляем!</b>\n\n"
                f"Лидер клана «<b>{clan_name}</b>» одобрил вашу заявку.\n"
                f"Теперь вы часть банды! 🛡️\n\n"
                f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
                f"Подписывайся! 🔥"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


@private_router.callback_query(F.data.startswith("clan_req_reject_"))
async def cb_clan_req_reject(callback: types.CallbackQuery, bot: Bot) -> None:
    request_id = int(callback.data.split("_")[-1])

    async with async_session() as session:
        req_q = await session.execute(
            select(ClanJoinRequest).where(ClanJoinRequest.id == request_id)
        )
        request = req_q.scalar_one_or_none()

        if request is None or request.status != "pending":
            await callback.answer("⚠️ Эта заявка уже обработана!", show_alert=True)
            return

        clan_q = await session.execute(
            select(Clan).where(Clan.id == request.clan_id)
        )
        clan = clan_q.scalar_one_or_none()

        if clan and callback.from_user.id != clan.leader_id:
            await callback.answer("⛔ Только лидер клана может обрабатывать заявки!", show_alert=True)
            return

        # Находим заявителя для отображения имени
        user_q = await session.execute(
            select(User).where(User.tg_id == request.user_id)
        )
        applicant = user_q.scalar_one_or_none()

        request.status = "rejected"
        await session.commit()

        applicant_display = f"@{applicant.username}" if applicant and applicant.username else f"ID:{request.user_id}"
        clan_name = clan.name if clan else "?"

    await callback.message.edit_text(
        f"❌ <b>Заявка игрока {applicant_display} отклонена.</b>",
        parse_mode="HTML",
    )
    await callback.answer("❌ Отклонено")

    # Уведомляем игрока
    try:
        await bot.send_message(
            chat_id=request.user_id,
            text=(
                f"😔 <b>Ваша заявка на вступление в клан «{clan_name}» "
                f"была отклонена лидером.</b>\n\n"
                f"Не расстраивайся — создай свой клан или попробуй другой!\n\n"
                f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
                f"Подписывайся! 🔥"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#
#                    🎰 СЛОТЫ / АВТОМАТ (ЛС + Группы)
#
# ══════════════════════════════════════════════════════════════════════════════


async def _handle_slots(message: types.Message) -> None:
    """Общая логика слот-машины (работает и в ЛС, и в группе)."""
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "❌ Использование: <code>слот 500</code> или <code>казино 1000</code>\n"
            f"Минимальная ставка: <b>{MIN_SLOT_BET:,} ⭐</b>",
            parse_mode="HTML",
        )
        return

    bet = int(parts[1])
    if bet < MIN_SLOT_BET:
        await message.answer(
            f"❌ Минимальная ставка — <b>{MIN_SLOT_BET:,} ⭐</b>!",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        if user.dark_stars < bet:
            await message.answer(
                f"❌ Недостаточно средств!\n"
                f"Баланс: <b>{user.dark_stars:,} ⭐</b>, ставка: <b>{bet:,} ⭐</b>",
                parse_mode="HTML",
            )
            return

        # Списываем ставку авансом
        user.dark_stars -= bet
        await session.commit()

    # Крутим барабаны
    dice_msg = await message.answer_dice(emoji="🎰")
    await asyncio.sleep(2.0)

    value = dice_msg.dice.value

    if value in SLOT_JACKPOT:
        multiplier = 25.0
        result_emoji = "🔥🔥🔥"
        result_text = "ТРИ СЕМЁРКИ! ДЖЕКПОТ!"
    elif value in SLOT_TRIPLE:
        multiplier = 5.0
        result_emoji = "🎉🎉🎉"
        result_text = "ТРИ ОДИНАКОВЫХ!"
    elif value in SLOT_DOUBLE:
        multiplier = 1.5
        result_emoji = "✨"
        result_text = "Две одинаковых!"
    else:
        multiplier = 0.0
        result_emoji = "💀"
        result_text = "Проигрыш..."

    winnings = int(bet * multiplier)

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        if winnings > 0:
            user.dark_stars += winnings
        await session.commit()
        balance = user.dark_stars

    if multiplier > 0:
        profit = winnings - bet
        msg = (
            f"{result_emoji} <b>{result_text}</b>\n\n"
            f"🎰 Ставка: <b>{bet:,} ⭐</b>\n"
            f"💰 Выигрыш: <b>{winnings:,} ⭐</b> (×{multiplier})\n"
            f"📈 Чистая прибыль: <b>+{profit:,} ⭐</b>\n"
            f"💼 Баланс: <b>{balance:,} ⭐</b>"
        )
    else:
        msg = (
            f"{result_emoji} <b>{result_text}</b>\n\n"
            f"🎰 Ставка: <b>{bet:,} ⭐</b>\n"
            f"💸 Списано: <b>-{bet:,} ⭐</b>\n"
            f"💼 Баланс: <b>{balance:,} ⭐</b>"
        )

    msg += (
        "\n\n📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        "Подписывайся! 🔥"
    )

    await dice_msg.reply(msg, parse_mode="HTML")


# ── ЛС: слоты ────────────────────────────────────────────────────────────────

@private_router.message(F.text.casefold().regexp(r"^(слот|казино)\s+\d+$"))
async def pm_slots(message: types.Message) -> None:
    await _handle_slots(message)


# ── Группа: слоты ────────────────────────────────────────────────────────────

@group_router.message(F.text.casefold().regexp(r"^(слот|казино)\s+\d+$"))
@_activated
async def group_slots(message: types.Message, **kwargs) -> None:
    await _handle_slots(message)


# ── Группа: мины ─────────────────────────────────────────────────────────────

@group_router.message(F.text.casefold().regexp(r"^мины\s+\d+$"))
@_activated
async def group_mines_start(message: types.Message, state: FSMContext, **kwargs) -> None:
    # Проверяем, нет ли уже активной игры
    current_state = await state.get_state()
    if current_state == MinesState.playing:
        await message.answer("⚠️ У тебя уже идёт игра в мины! Доиграй или забери куш.")
        return

    parts = message.text.strip().split()
    bet = int(parts[1])

    if bet < MIN_MINES_BET:
        await message.answer(
            f"❌ Минимальная ставка в мины — <b>{MIN_MINES_BET:,} ⭐</b>!",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        if user.dark_stars < bet:
            await message.answer(
                f"❌ Недостаточно средств!\n"
                f"Баланс: <b>{user.dark_stars:,} ⭐</b>, ставка: <b>{bet:,} ⭐</b>",
                parse_mode="HTML",
            )
            return

        user.dark_stars -= bet
        await session.commit()

    field = _mines_generate_field()

    await state.set_state(MinesState.playing)
    await state.update_data(
        bet=bet,
        multiplier=1.0,
        diamonds_opened=0,
        field=field,
        opened=[],
    )

    kb = _mines_build_keyboard(field, opened=[], multiplier=1.0)

    await message.answer(
        f"💣 <b>ИГРА МИНЫ!</b>\n\n"
        f"Ставка: <b>{bet:,} ⭐</b>\n"
        f"На поле <b>{MINES_COUNT}</b> мин. Открывайте ячейки!\n"
        f"Каждый 💎 увеличивает множитель.\n"
        f"Попадёте на 💥 — потеряете ставку!",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ══════════════════════════════════════════════════════════════════════════════
#
#                    💣 МИНЫ (Mines) — только ЛС
#
# ══════════════════════════════════════════════════════════════════════════════


def _mines_generate_field() -> list[bool]:
    """Генерирует поле 4×4: True = мина, False = алмаз."""
    field = [False] * MINES_GRID_SIZE
    mine_positions = random.sample(range(MINES_GRID_SIZE), MINES_COUNT)
    for pos in mine_positions:
        field[pos] = True
    return field


def _mines_build_keyboard(
    field: list[bool],
    opened: list[int],
    reveal_all: bool = False,
    multiplier: float = 1.0,
    game_over: bool = False,
) -> types.InlineKeyboardMarkup:
    """Строит Inline-клавиатуру 4×4 + кнопка «Забрать куш»."""
    rows: list[list[types.InlineKeyboardButton]] = []
    for row_idx in range(5):
        row_buttons: list[types.InlineKeyboardButton] = []
        for col_idx in range(5):
            idx = row_idx * 5 + col_idx

            if reveal_all:
                # Раскрываем всё поле
                if field[idx]:
                    text = "💥"
                else:
                    text = "💎" if idx in opened else "◻️"
                cb = f"mine_noop_{idx}"
            elif idx in opened:
                text = "💎"
                cb = f"mine_noop_{idx}"
            else:
                text = "⬜"
                cb = f"mine_click_{idx}"

            row_buttons.append(types.InlineKeyboardButton(text=text, callback_data=cb))
        rows.append(row_buttons)

    # Кнопка «Забрать куш»
    if not game_over and not reveal_all:
        cashout_text = f"💰 Забрать куш ({multiplier}x)"
        rows.append([types.InlineKeyboardButton(text=cashout_text, callback_data="mine_cashout")])

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@private_router.message(F.text.casefold().regexp(r"^мины\s+\d+$"))
async def pm_mines_start(message: types.Message, state: FSMContext) -> None:
    # Проверяем, нет ли уже активной игры
    current_state = await state.get_state()
    if current_state == MinesState.playing:
        await message.answer("⚠️ У тебя уже идёт игра в мины! Доиграй или забери куш.")
        return

    parts = message.text.strip().split()
    bet = int(parts[1])

    if bet < MIN_MINES_BET:
        await message.answer(
            f"❌ Минимальная ставка в мины — <b>{MIN_MINES_BET:,} ⭐</b>!",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        if user.dark_stars < bet:
            await message.answer(
                f"❌ Недостаточно средств!\n"
                f"Баланс: <b>{user.dark_stars:,} ⭐</b>, ставка: <b>{bet:,} ⭐</b>",
                parse_mode="HTML",
            )
            return

        # Списываем ставку
        user.dark_stars -= bet
        await session.commit()

    # Генерируем поле
    field = _mines_generate_field()

    await state.set_state(MinesState.playing)
    await state.update_data(
        bet=bet,
        multiplier=1.0,
        diamonds_opened=0,
        field=field,
        opened=[],
    )

    kb = _mines_build_keyboard(field, opened=[], multiplier=1.0)

    await message.answer(
        f"💣 <b>ИГРА МИНЫ!</b>\n\n"
        f"Ставка: <b>{bet:,} ⭐</b>\n"
        f"На поле <b>{MINES_COUNT}</b> мин. Открывайте ячейки!\n"
        f"Каждый 💎 увеличивает множитель.\n"
        f"Попадёте на 💥 — потеряете ставку!",
        parse_mode="HTML",
        reply_markup=kb,
    )


@private_router.callback_query(F.data.startswith("mine_click_"), MinesState.playing)
async def cb_mine_click(callback: types.CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split("_")[2])
    data = await state.get_data()

    field: list[bool] = data["field"]
    opened: list[int] = data["opened"]
    bet: int = data["bet"]
    diamonds_opened: int = data["diamonds_opened"]

    # Уже открыта — игнорируем
    if idx in opened:
        await callback.answer("Уже открыто!")
        return

    if field[idx]:
        # ── МИНА! ──
        opened.append(idx)
        kb = _mines_build_keyboard(field, opened, reveal_all=True, game_over=True)

        await callback.message.edit_text(
            f"💥 <b>БУМ! Вы подорвались на мине!</b>\n\n"
            f"Ставка <b>{bet:,} ⭐</b> потеряна.\n\n"
            f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
            f"Подписывайся! 🔥",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await state.clear()
        await callback.answer("💥 Мина!")
    else:
        # ── АЛМАЗ! ──
        opened.append(idx)
        diamonds_opened += 1
        multiplier = round(1.0 + diamonds_opened * 0.35, 2)

        await state.update_data(
            opened=opened,
            diamonds_opened=diamonds_opened,
            multiplier=multiplier,
        )

        kb = _mines_build_keyboard(field, opened, multiplier=multiplier)
        winnings = int(bet * multiplier)

        # Все алмазы открыты — автоматический кэшаут
        total_diamonds = MINES_GRID_SIZE - MINES_COUNT
        if diamonds_opened >= total_diamonds:
            async with async_session() as session:
                user = await get_or_create_user(
                    session, callback.from_user.id, callback.from_user.username
                )
                user.dark_stars += winnings
                await session.commit()
                balance = user.dark_stars

            kb = _mines_build_keyboard(field, opened, reveal_all=True, game_over=True)
            await callback.message.edit_text(
                f"🏆 <b>ВСЕ АЛМАЗЫ НАЙДЕНЫ!</b>\n\n"
                f"Ставка: <b>{bet:,} ⭐</b>\n"
                f"Множитель: <b>×{multiplier}</b>\n"
                f"💰 Выигрыш: <b>{winnings:,} ⭐</b>\n"
                f"💼 Баланс: <b>{balance:,} ⭐</b>\n\n"
                f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
                f"Подписывайся! 🔥",
                parse_mode="HTML",
                reply_markup=kb,
            )
            await state.clear()
            await callback.answer("🏆 Все алмазы найдены!")
            return

        await callback.message.edit_text(
            f"💣 <b>ИГРА МИНЫ!</b>\n\n"
            f"Ставка: <b>{bet:,} ⭐</b>\n"
            f"💎 Открыто алмазов: <b>{diamonds_opened}</b>\n"
            f"📈 Текущий множитель: <b>×{multiplier}</b>\n"
            f"💰 Текущий куш: <b>{winnings:,} ⭐</b>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await callback.answer(f"💎 Алмаз! Множитель: ×{multiplier}")


@private_router.callback_query(F.data == "mine_cashout", MinesState.playing)
async def cb_mine_cashout(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    bet: int = data["bet"]
    multiplier: float = data["multiplier"]
    field: list[bool] = data["field"]
    opened: list[int] = data["opened"]
    diamonds_opened: int = data["diamonds_opened"]

    if diamonds_opened == 0:
        await callback.answer("⚠️ Откройте хотя бы одну ячейку!", show_alert=True)
        return

    winnings = int(bet * multiplier)

    async with async_session() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )
        user.dark_stars += winnings
        await session.commit()
        balance = user.dark_stars

    kb = _mines_build_keyboard(field, opened, reveal_all=True, game_over=True)
    profit = winnings - bet

    await callback.message.edit_text(
        f"💰 <b>ВЫ УСПЕШНО ЗАБРАЛИ КУШ!</b>\n\n"
        f"Ставка: <b>{bet:,} ⭐</b>\n"
        f"Множитель: <b>×{multiplier}</b>\n"
        f"💰 Выигрыш: <b>{winnings:,} ⭐</b>\n"
        f"📈 Чистая прибыль: <b>+{profit:,} ⭐</b>\n"
        f"💼 Баланс: <b>{balance:,} ⭐</b>\n\n"
        f"📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        f"Подписывайся! 🔥",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await state.clear()
    await callback.answer(f"💰 Забрано {winnings:,} ⭐!")


@private_router.callback_query(F.data.startswith("mine_noop_"))
async def cb_mine_noop(callback: types.CallbackQuery) -> None:
    await callback.answer()


# ── Группа: callback-хэндлеры мин ────────────────────────────────────────────

@group_router.callback_query(F.data.startswith("mine_click_"), MinesState.playing)
async def group_cb_mine_click(callback: types.CallbackQuery, state: FSMContext) -> None:
    await cb_mine_click(callback, state)


@group_router.callback_query(F.data == "mine_cashout", MinesState.playing)
async def group_cb_mine_cashout(callback: types.CallbackQuery, state: FSMContext) -> None:
    await cb_mine_cashout(callback, state)


@group_router.callback_query(F.data.startswith("mine_noop_"))
async def group_cb_mine_noop(callback: types.CallbackQuery) -> None:
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#
#                       ОБЩИЕ ФУНКЦИИ (ЛС + Группы)
#
# ══════════════════════════════════════════════════════════════════════════════


async def _handle_leaderboard(message: types.Message) -> None:
    """Глобальный ТОП-10 игроков по суммарной стоимости инвентаря."""
    async with async_session() as session:
        # JOIN InventoryItem → User, группируем по user_id,
        # суммируем market_value, сортируем по убыванию, берём ТОП-10
        stmt = (
            select(
                InventoryItem.user_id,
                func.sum(InventoryItem.market_value).label("total_value"),
                func.count(InventoryItem.id).label("item_count"),
                User.username,
            )
            .join(User, User.tg_id == InventoryItem.user_id)
            .group_by(InventoryItem.user_id, User.username)
            .order_by(func.sum(InventoryItem.market_value).desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        rows = result.all()

    if not rows:
        await message.answer(
            "🏆 Топ пока пуст!\n"
            "Будь первым, кто откроет кейс и возглавит лидерборд!",
            parse_mode="HTML",
        )
        return

    MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

    lines: list[str] = ["🏆 <b>ГЛОБАЛЬНЫЙ ТОП ИГРОКОВ</b>\n<i>(по ценности инвентаря)</i>\n"]

    for idx, row in enumerate(rows, 1):
        user_id, total_value, item_count, username = row
        display_name = f"@{username}" if username else f"Игрок #{user_id}"
        medal = MEDALS.get(idx, f"{idx}.")
        if idx <= 3:
            lines.append(
                f"{medal} <b>{display_name}</b> — "
                f"<b>{total_value:,} ⭐</b> ({item_count} предметов)"
            )
        else:
            lines.append(
                f"  {idx}. {display_name} — "
                f"<b>{total_value:,} ⭐</b> ({item_count} предметов)"
            )

    lines.append(
        "\n——————————————————\n"
        "📢 Новости проекта, эксклюзивные промокоды и обновления тут: @larpcase\n"
        "Подписывайся! 🔥"
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


async def _handle_daily(message: types.Message) -> None:
    now = datetime.now(timezone.utc)

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        if user.last_daily is not None:
            next_at = user.last_daily + DAILY_COOLDOWN
            if now < next_at:
                rem = next_at - now
                h, r = divmod(int(rem.total_seconds()), 3600)
                m = r // 60
                await message.answer(
                    f"⏳ <b>Бонус ещё не готов!</b>\n\n"
                    f"Возвращайся через <b>{h}ч {m}мин</b> ⏰",
                    parse_mode="HTML",
                )
                return

        user.dark_stars += DAILY_BONUS
        user.last_daily = now
        await session.commit()

    await message.answer(
        f"🎁 <b>ЕЖЕДНЕВНЫЙ БОНУС!</b>\n\n"
        f"{tag(message.from_user)} получил <b>+{DAILY_BONUS:,} ⭐ Dark Stars</b>!\n"
        f"Баланс: <b>{user.dark_stars:,} ⭐</b>\n\n"
        f"Приходи завтра за новой порцией 🌟",
        parse_mode="HTML",
    )


async def _handle_promo(message: types.Message) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("❌ Использование: <code>промо КОД</code>", parse_mode="HTML")
        return

    code_input = parts[1].strip()

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

        result = await session.execute(
            select(Promocode).where(Promocode.code == code_input)
        )
        promo = result.scalar_one_or_none()

        if promo is None:
            await message.answer("❌ Промокод не найден.", parse_mode="HTML")
            return

        if promo.current_activations >= promo.max_activations:
            await message.answer("🚫 Этот промокод уже исчерпал все активации!", parse_mode="HTML")
            return

        tg_str = str(user.tg_id)
        used_list = [x.strip() for x in promo.activated_by.split(",") if x.strip()]

        if tg_str in used_list:
            await message.answer("⚠️ Ты уже активировал этот промокод!", parse_mode="HTML")
            return

        user.dark_stars += promo.reward_amount
        promo.current_activations += 1
        used_list.append(tg_str)
        promo.activated_by = ",".join(used_list)
        await session.commit()

    await message.answer(
        f"✅ <b>ПРОМОКОД АКТИВИРОВАН!</b>\n\n"
        f"Код: <code>{code_input}</code>\n"
        f"Получено: <b>+{promo.reward_amount:,} ⭐ Dark Stars</b>\n"
        f"Баланс: <b>{user.dark_stars:,} ⭐</b>",
        parse_mode="HTML",
    )
