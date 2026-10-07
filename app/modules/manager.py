"""Gestionnaire de modules d'UNE chaîne : chargement, activation/désactivation à chaud, persistance."""
import logging

from sqlalchemy import select

from ..db import ModuleState, Session
from ..events import audit, bus
from .commands import CommandsModule
from .logs import LogsModule
from .protect import ProtectModule
from .timers import TimersModule

log = logging.getLogger("cocobot.modules")

# L'ordre = ordre d'exécution de la chaîne de traitement des messages
MODULE_CLASSES = [LogsModule, TimersModule, ProtectModule, CommandsModule]


class ModuleManager:
    def __init__(self, ctx) -> None:
        self.ctx = ctx  # ChannelContext
        self.modules = {cls.name: cls(ctx) for cls in MODULE_CLASSES}

    async def load(self) -> None:
        async with Session() as s:
            rows = {r.name: r for r in (await s.execute(
                select(ModuleState).where(ModuleState.channel_id == self.ctx.id))).scalars()}
            for name, mod in self.modules.items():
                row = rows.get(name)
                if row is None:
                    row = ModuleState(channel_id=self.ctx.id, name=name, enabled=name in ("logs", "commands"),
                                      config=mod.config.model_dump())
                    s.add(row)
                mod.config = mod.ConfigModel.model_validate(row.config or {})
                mod.enabled = row.enabled
            await s.commit()
        for mod in self.modules.values():
            if mod.enabled:
                await mod.on_start()

    async def shutdown(self) -> None:
        for mod in self.modules.values():
            if mod.enabled:
                await mod.on_stop()

    def describe(self, name: str) -> dict:
        m = self.modules[name]
        return {"channel_id": self.ctx.id, "name": m.name, "label": m.label, "description": m.description,
                "enabled": m.enabled, "config": m.config.model_dump()}

    async def _persist(self, name: str) -> None:
        m = self.modules[name]
        async with Session() as s:
            row = await s.get(ModuleState, (self.ctx.id, name))
            row.enabled, row.config = m.enabled, m.config.model_dump()
            await s.commit()
        await bus.publish("module", self.describe(name))

    async def set_enabled(self, name: str, enabled: bool) -> None:
        m = self.modules[name]
        if m.enabled == enabled:
            return
        m.enabled = enabled
        await (m.on_start() if enabled else m.on_stop())
        await self._persist(name)
        await audit("config", f"Module « {m.label} » {'activé' if enabled else 'désactivé'}", channel=self.ctx.id)

    async def set_config(self, name: str, raw: dict) -> None:
        m = self.modules[name]
        m.config = m.ConfigModel.model_validate(raw)  # lève ValidationError si invalide
        await self._persist(name)
        if m.enabled:
            await m.on_config_changed()
        await audit("config", f"Configuration du module « {m.label} » modifiée", channel=self.ctx.id)

    async def dispatch(self, msg) -> None:
        for mod in self.modules.values():
            if not mod.enabled:
                continue
            try:
                if await mod.handle_message(msg):
                    break
            except Exception:  # noqa: BLE001 - un module défaillant ne doit pas tuer le bot
                log.exception("Erreur dans le module %s (#%s)", mod.name, self.ctx.channel)
