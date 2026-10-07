"""Module 4 : Logs & audit (rétention automatique)."""
import asyncio
from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy import delete, or_

from ..db import LogEntry, Session, utcnow
from .base import BaseModule


class LogsConfig(BaseModel):
    log_chat: bool = False                       # journaliser tous les messages (plus d'écritures disque)
    retention_days: int = Field(7, ge=1, le=365)


class LogsModule(BaseModule):
    name = "logs"
    label = "Logs & Audit"
    description = "Flux en direct des actions du bot, sanctions, commandes et changements de configuration."
    ConfigModel = LogsConfig

    _task: asyncio.Task | None = None

    async def on_start(self) -> None:
        self._task = asyncio.create_task(self._prune_loop())

    async def on_stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _prune_loop(self) -> None:
        while True:
            cutoff = utcnow() - timedelta(days=self.config.retention_days)
            async with Session() as s:
                await s.execute(delete(LogEntry).where(
                    LogEntry.ts < cutoff, or_(LogEntry.channel_id == self.service.id, LogEntry.channel_id.is_(None))))
                await s.commit()
            await asyncio.sleep(3600)
