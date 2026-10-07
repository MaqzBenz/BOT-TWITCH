"""Bus d'événements temps réel (SSE) + journal d'audit persistant (par chaîne)."""
import asyncio
import logging

from .db import LogEntry, Session

log = logging.getLogger("cocobot.events")


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, type_: str, data: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait({"type": type_, "data": data})
            except asyncio.QueueFull:  # client trop lent : on le déconnecte
                self._subs.discard(q)


bus = EventBus()

# Catégories toujours journalisées, même si le module Logs de la chaîne est désactivé
ALWAYS = {"auth", "config", "system"}


class _State:
    registry = None  # ChannelRegistry, renseigné par main.py


state = _State()


async def audit(category: str, message: str, data: dict | None = None, channel: int | None = None) -> None:
    """Enregistre une action (channel = id en base de la chaîne, None = global) et la diffuse au dashboard."""
    reg = state.registry
    if channel is not None and reg is not None and category not in ALWAYS:
        ctx = reg.by_id.get(channel)
        if ctx is None:
            return
        logs = ctx.manager.modules["logs"]
        if not logs.enabled or (category == "chat" and not logs.config.log_chat):
            return
    log.info("[%s|%s] %s", category, channel, message)
    async with Session() as s:
        entry = LogEntry(channel_id=channel, category=category, message=message[:500], data=data)
        s.add(entry)
        await s.commit()
    await bus.publish("log", serialize_log(entry))


def serialize_log(e: LogEntry) -> dict:
    return {"id": e.id, "channel_id": e.channel_id, "ts": e.ts.isoformat(),
            "category": e.category, "message": e.message, "data": e.data}
