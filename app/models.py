"""
models.py — ORM-модели проекта.

Таблицы: clans, users, inventory_items, promocodes, clan_join_requests.
Порядок объявления важен: Clan до User (FK-зависимость).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ═══════════════════════════  CLAN  ═════════════════════════════════════════

class Clan(Base):
    __tablename__ = "clans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    leader_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="tg_id лидера клана",
    )
    total_elo: Mapped[int] = mapped_column(
        Integer, default=1000, server_default="1000",
    )

    members: Mapped[list["User"]] = relationship(
        back_populates="clan",
        foreign_keys="User.clan_id",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Clan '{self.name}' leader={self.leader_id} elo={self.total_elo}>"


# ═══════════════════════════  USER  ═════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False,
    )
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dark_stars: Mapped[int] = mapped_column(
        Integer, default=1000, server_default="1000",
    )
    elo_rating: Mapped[int] = mapped_column(
        Integer, default=1000, server_default="1000",
    )
    clan_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("clans.id", ondelete="SET NULL"),
        nullable=True,
    )
    clan_role: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment="leader или member",
    )
    last_daily: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    referred_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True,
        comment="tg_id пригласившего игрока",
    )

    inventory: Mapped[list["InventoryItem"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    clan: Mapped[Optional["Clan"]] = relationship(
        back_populates="members",
        foreign_keys=[clan_id],
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<User tg_id={self.tg_id} stars={self.dark_stars} elo={self.elo_rating}>"


# ═══════════════════════════  INVENTORY ITEM  ══════════════════════════════

class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.tg_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rarity: Mapped[str] = mapped_column(String(32), nullable=False)
    market_value: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0",
    )

    owner: Mapped["User"] = relationship(
        back_populates="inventory", lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Item '{self.item_name}' [{self.rarity}] val={self.market_value}>"


# ═══════════════════════════  PROMOCODE  ═══════════════════════════════════

class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    reward_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    max_activations: Mapped[int] = mapped_column(Integer, nullable=False)
    current_activations: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0",
    )
    activated_by: Mapped[str] = mapped_column(
        Text, default="", server_default="",
        comment="tg_id через запятую",
    )

    def __repr__(self) -> str:
        return f"<Promo '{self.code}' {self.current_activations}/{self.max_activations}>"


# ═══════════════════════════  CLAN JOIN REQUEST  ═══════════════════════════

class ClanJoinRequest(Base):
    __tablename__ = "clan_join_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.tg_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    clan_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("clans.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending",
        comment="pending / accepted / rejected",
    )

    def __repr__(self) -> str:
        return f"<ClanJoinRequest user={self.user_id} clan={self.clan_id} status={self.status}>"
