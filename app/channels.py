"""Contexte par chaîne : chaque chaîne a son propre gestionnaire de modules, sa config et ses logs."""
import logging

from sqlalchemy import delete, select

from .db import Channel, Command, LogEntry, ModuleState, Session, Timer
from .events import audit, bus
from .modules.manager import ModuleManager

log = logging.getLogger("cocobot.channels")


class ChannelContext:
    """Façade passée aux modules : toutes les actions Twitch sont limitées à cette chaîne."""

    def __init__(self, row: Channel, twitch) -> None:
        self.id = row.id
        self.channel = row.login           # login Twitch
        self.broadcaster_id = row.twitch_id
        self.enabled = row.enabled
        self.twitch = twitch
        self.manager = ModuleManager(self)

    async def say(self, text: str) -> None:
        await self.twitch.say(self.channel, text)

    async def timeout(self, user_id: str, seconds: int, reason: str) -> bool:
        return await self.twitch.ban(self.broadcaster_id, user_id, reason, seconds, self.id)

    async def ban(self, user_id: str, reason: str) -> bool:
        return await self.twitch.ban(self.broadcaster_id, user_id, reason, None, self.id)

    async def delete_message(self, msg_id: str) -> bool:
        return await self.twitch.delete_message(self.broadcaster_id, msg_id)

    async def stream_info(self) -> dict | None:
        return await self.twitch.stream_info(self.channel)

    def describe(self) -> dict:
        joined = bool(self.twitch.bot and self.twitch.bot.get_channel(self.channel)) if self.enabled else False
        return {"id": self.id, "login": self.channel, "enabled": self.enabled, "joined": joined}


class ChannelRegistry:
    def __init__(self, twitch) -> None:
        self.twitch = twitch
        self.by_id: dict[int, ChannelContext] = {}
        self.by_login: dict[str, ChannelContext] = {}   # uniquement les chaînes actives

    async def load(self, seed_login: str = "") -> None:
        async with Session() as s:
            rows = (await s.execute(select(Channel).order_by(Channel.id))).scalars().all()
        if not rows and seed_login:  # première installation : reprise de TWITCH_CHANNEL
            try:
                await self.add(seed_login, joined=False)
            except Exception as exc:  # noqa: BLE001 - l'utilisateur pourra l'ajouter depuis le dashboard
                log.warning("Chaîne initiale #%s non ajoutée : %s", seed_login, exc)
            return
        for row in rows:
            await self._activate(ChannelContext(row, self.twitch))

    async def _activate(self, ctx: ChannelContext) -> None:
        self.by_id[ctx.id] = ctx
        if ctx.enabled:
            self.by_login[ctx.channel] = ctx
            await ctx.manager.load()

    async def add(self, login: str, joined: bool = True) -> ChannelContext:
        login = login.lower().lstrip("#").strip()
        if login in {c.channel for c in self.by_id.values()}:
            raise ValueError("Cette chaîne est déjà ajoutée")
        user = await self.twitch.resolve_user(login)
        if user is None:
            raise LookupError("Chaîne Twitch introuvable (ou token Twitch invalide)")
        async with Session() as s:
            row = Channel(login=user[1], twitch_id=user[0], enabled=True)
            s.add(row)
            await s.commit()
        ctx = ChannelContext(row, self.twitch)
        await self._activate(ctx)
        if joined:
            await self.twitch.join(ctx.channel)
        await audit("config", f"Chaîne #{ctx.channel} ajoutée")
        await bus.publish("channels", {})
        return ctx

    async def set_enabled(self, cid: int, enabled: bool) -> ChannelContext:
        ctx = self.by_id[cid]
        if ctx.enabled == enabled:
            return ctx
        ctx.enabled = enabled
        async with Session() as s:
            row = await s.get(Channel, cid)
            row.enabled = enabled
            await s.commit()
        if enabled:
            self.by_login[ctx.channel] = ctx
            await ctx.manager.load()
            await self.twitch.join(ctx.channel)
        else:
            self.by_login.pop(ctx.channel, None)
            await ctx.manager.shutdown()
            await self.twitch.part(ctx.channel)
        await audit("config", f"Chaîne #{ctx.channel} {'activée' if enabled else 'désactivée'}")
        await bus.publish("channels", {})
        return ctx

    async def remove(self, cid: int) -> None:
        ctx = self.by_id.pop(cid)
        self.by_login.pop(ctx.channel, None)
        await ctx.manager.shutdown()
        await self.twitch.part(ctx.channel)
        async with Session() as s:
            for model in (Command, Timer, ModuleState, LogEntry):
                await s.execute(delete(model).where(model.channel_id == cid))
            await s.execute(delete(Channel).where(Channel.id == cid))
            await s.commit()
        await audit("config", f"Chaîne #{ctx.channel} supprimée (config et logs effacés)")
        await bus.publish("channels", {})

    async def shutdown(self) -> None:
        for ctx in self.by_id.values():
            if ctx.enabled:
                await ctx.manager.shutdown()
