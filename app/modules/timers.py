"""Module 3 : timers & annonces (intervalle de temps ET quota de messages)."""
import asyncio
import time

from pydantic import BaseModel
from sqlalchemy import select

from ..db import Session, Timer
from ..events import audit
from .base import BaseModule


class TimersConfig(BaseModel):
    only_when_live: bool = True   # n'annoncer que si le stream est en direct


class TimersModule(BaseModule):
    name = "timers"
    label = "Timers & Annonces"
    description = "Messages automatiques : toutes les X minutes ET après au moins N messages dans le chat."
    ConfigModel = TimersConfig

    def __init__(self, service) -> None:
        super().__init__(service)
        self._task: asyncio.Task | None = None
        self._timers: list[dict] = []
        self._state: dict[int, tuple[float, int]] = {}  # id -> (dernier envoi, compteur au dernier envoi)
        self._chat_count = 0

    async def on_start(self) -> None:
        await self.reload()
        self._task = asyncio.create_task(self._loop())

    async def on_stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def reload(self) -> None:
        async with Session() as s:
            rows = (await s.execute(select(Timer).where(Timer.enabled, Timer.channel_id == self.service.id))
                    ).scalars().all()
        self._timers = [{"id": r.id, "name": r.name, "message": r.message,
                         "interval": r.interval_minutes * 60, "min_messages": r.min_messages} for r in rows]
        now = time.monotonic()
        for t in self._timers:  # un nouveau timer démarre son décompte maintenant
            self._state.setdefault(t["id"], (now, self._chat_count))

    async def handle_message(self, msg) -> bool:
        self._chat_count += 1
        return False

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                now = time.monotonic()
                for t in self._timers:
                    last, count_then = self._state.get(t["id"], (now, self._chat_count))
                    if now - last < t["interval"] or self._chat_count - count_then < t["min_messages"]:
                        continue
                    if self.config.only_when_live and not await self.service.stream_info():
                        continue
                    await self.service.say(t["message"])
                    self._state[t["id"]] = (now, self._chat_count)
                    await audit("timer", f"Annonce « {t['name']} » envoyée", channel=self.service.id)
            except Exception:  # noqa: BLE001
                await audit("system", "Erreur dans la boucle des timers", channel=self.service.id)
