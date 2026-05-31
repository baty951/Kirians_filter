from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Chat(Base):
    """Per-chat configuration. One row per group/supergroup the bot moderates."""

    __tablename__ = "chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Feature toggles
    filter_badwords: Mapped[bool] = mapped_column(Boolean, default=True)
    filter_links: Mapped[bool] = mapped_column(Boolean, default=False)
    filter_media: Mapped[bool] = mapped_column(Boolean, default=False)
    filter_language: Mapped[bool] = mapped_column(Boolean, default=False)
    antiflood_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    captcha_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Tunables
    warn_limit: Mapped[int] = mapped_column(Integer, default=3)
    mute_minutes: Mapped[int] = mapped_column(Integer, default=60)
    welcome_text: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    log_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Comma-separated script names to block when filter_language is on.
    blocked_scripts: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    warnings: Mapped[list["Warning"]] = relationship(back_populates="chat", cascade="all, delete-orphan")
    banned_words: Mapped[list["BannedWord"]] = relationship(back_populates="chat", cascade="all, delete-orphan")


class Warning(Base):
    """A single warning issued to a user in a chat."""

    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.chat_id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chat: Mapped["Chat"] = relationship(back_populates="warnings")


class ActionLog(Base):
    """Audit log of moderation events: message deletions (with the deleted
    text), mutes/unmutes, bans/unbans, kicks, warns — by admins and the bot."""

    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.chat_id", ondelete="CASCADE"), index=True
    )
    # Event type, e.g. DELETE / MUTE / UNMUTE / BAN / UNBAN / KICK / WARN / PURGE.
    action: Mapped[str] = mapped_column(String(32))
    # Who performed it: an admin's user id, or the bot's id for automatic actions.
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The affected user, when applicable.
    target_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Free-form extra payload — the deleted message text for DELETE events.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BannedWord(Base):
    """A forbidden word/pattern scoped to a chat (chat_id NULL = global)."""

    __tablename__ = "banned_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=True, index=True
    )
    pattern: Mapped[str] = mapped_column(String(256))
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False)

    chat: Mapped["Chat | None"] = relationship(back_populates="banned_words")
