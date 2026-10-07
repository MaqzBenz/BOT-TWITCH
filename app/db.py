"""Moteur SQLite asynchrone + modèles ORM (SQLAlchemy 2)."""
import logging
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings

log = logging.getLogger("cocobot.db")
DB_FILE = settings.data_path / "cocobot.db"


def _migrate_legacy_db() -> None:
    """Base v1 (mono-chaîne) détectée : on la met de côté, une base multi-chaînes est recréée."""
    if not DB_FILE.exists():
        return
    con = sqlite3.connect(DB_FILE)
    try:
        has_cmds = con.execute("SELECT 1 FROM sqlite_master WHERE name='commands'").fetchone()
        cols = [r[1] for r in con.execute("PRAGMA table_info(commands)")] if has_cmds else ["channel_id"]
    finally:
        con.close()
    if "channel_id" not in cols:
        backup = DB_FILE.with_suffix(".v1.bak")
        DB_FILE.rename(backup)
        for ext in ("-wal", "-shm"):
            DB_FILE.with_name(DB_FILE.name + ext).unlink(missing_ok=True)
        log.warning("Ancienne base mono-chaîne sauvegardée dans %s", backup)


_migrate_legacy_db()
engine = create_async_engine(f"sqlite+aiosqlite:///{DB_FILE}")
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")   # moins d'écritures bloquantes sur carte SD
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(25), unique=True, index=True)
    twitch_id: Mapped[str] = mapped_column(String(20), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_CH = lambda: mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)  # noqa: E731


class ModuleState(Base):
    __tablename__ = "module_state"
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class Command(Base):
    __tablename__ = "commands"
    __table_args__ = (UniqueConstraint("channel_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = _CH()
    name: Mapped[str] = mapped_column(String(25), index=True)
    response: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(default=True)
    permission: Mapped[str] = mapped_column(String(12), default="everyone")
    cooldown_global: Mapped[int] = mapped_column(default=5)
    cooldown_user: Mapped[int] = mapped_column(default=15)
    uses: Mapped[int] = mapped_column(default=0)


class Timer(Base):
    __tablename__ = "timers"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = _CH()
    name: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(String(500))
    interval_minutes: Mapped[int] = mapped_column(default=15)
    min_messages: Mapped[int] = mapped_column(default=5)
    enabled: Mapped[bool] = mapped_column(default=True)


class LogEntry(Base):
    __tablename__ = "logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True, nullable=True)  # NULL = global (système, auth)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
