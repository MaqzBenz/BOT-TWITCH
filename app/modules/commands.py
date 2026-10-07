"""Module 2 : commandes personnalisées avec variables dynamiques et cooldowns."""
import time
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select, update

from ..db import Command, Session
from ..events import audit
from .base import BaseModule

import logging

log = logging.getLogger("cocobot.commands")
PERM_LEVEL = {"everyone": 0, "vip": 1, "mod": 2, "broadcaster": 3}


class CommandsConfig(BaseModel):
    prefix: str = Field("!", min_length=1, max_length=3)


def format_uptime(started_at: str) -> str:
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    secs = int((datetime.now(timezone.utc) - start).total_seconds())
    h, m = divmod(secs // 60, 60)
    return f"{h}h {m}min" if h else f"{m}min"


class CommandsModule(BaseModule):
    name = "commands"
    label = "Commandes & Utilitaires"
    description = "Commandes personnalisées (!commande → réponse), variables dynamiques et cooldowns."
    ConfigModel = CommandsConfig

    def __init__(self, service) -> None:
        super().__init__(service)
        self.cache: dict[str, dict] = {}
        self._global_cd: dict[str, float] = {}
        self._user_cd: dict[tuple[str, str], float] = {}

    async def on_start(self) -> None:
        await self.reload()

    async def reload(self) -> None:
        async with Session() as s:
            rows = (await s.execute(select(Command).where(Command.channel_id == self.service.id))).scalars().all()
        self.cache = {r.name.lower(): {"id": r.id, "response": r.response, "enabled": r.enabled, "permission": r.permission,
                                       "cd_global": r.cooldown_global, "cd_user": r.cooldown_user} for r in rows}
        log.info("[#%s] %d commande(s) chargées : %s", self.service.channel, len(self.cache), list(self.cache.keys()))

    async def handle_message(self, msg) -> bool:
        prefix = self.config.prefix
        if not msg.content.startswith(prefix):
            return False
        parts = msg.content[len(prefix):].split(None, 1)
        if not parts:
            return False
        name, args = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        cmd = self.cache.get(name)

        # Fallback dynamique si pas encore en cache
        if not cmd:
            async with Session() as s:
                row = (await s.execute(select(Command).where(Command.channel_id == self.service.id, Command.name == name))).scalar_one_or_none()
                if row:
                    cmd = {"id": row.id, "response": row.response, "enabled": row.enabled, "permission": row.permission,
                           "cd_global": row.cooldown_global, "cd_user": row.cooldown_user}
                    self.cache[name] = cmd

        if not cmd:
            log.debug("[#%s] Commande inconnue : '%s%s'", self.service.channel, prefix, name)
            return False
        if not cmd["enabled"]:
            log.info("[#%s] Commande '%s%s' ignorée car désactivée", self.service.channel, prefix, name)
            return False

        level = 3 if msg.is_broadcaster else 2 if msg.is_mod else 1 if msg.is_vip else 0
        required_level = PERM_LEVEL.get(cmd.get("permission", "everyone"), 0)
        if level < required_level:
            log.info("[#%s] Commande '%s%s' refusée à %s (niveau requis: %d, niveau: %d)",
                     self.service.channel, prefix, name, msg.user, required_level, level)
            return True

        now = time.monotonic()
        if level < 2:  # les modérateurs et diffuseurs ignorent les cooldowns
            if now < self._global_cd.get(name, 0) or now < self._user_cd.get((name, msg.user_id), 0):
                log.info("[#%s] Commande '%s%s' en cooldown pour %s", self.service.channel, prefix, name, msg.user)
                return True
            self._global_cd[name] = now + cmd["cd_global"]
            self._user_cd[(name, msg.user_id)] = now + cmd["cd_user"]
            if len(self._user_cd) > 5000:
                self._user_cd = {k: v for k, v in self._user_cd.items() if v > now}

        async with Session() as s:
            await s.execute(update(Command).where(Command.id == cmd["id"]).values(uses=Command.uses + 1))
            uses_row = await s.get(Command, cmd["id"])
            uses = uses_row.uses if uses_row else 1
            await s.commit()

        text = await self._render(cmd["response"], msg, args, uses)
        await self.service.say(text)
        log.info("[#%s] Commande '%s%s' exécutée par %s -> %s", self.service.channel, prefix, name, msg.user, text[:50])
        await audit("command", f"{msg.user} a utilisé {prefix}{name}", {"user": msg.user, "command": name},
                    channel=self.service.id)
        return True

    async def _render(self, template: str, msg, args: str, uses: int) -> str:
        values = {"{user}": msg.user, "{channel}": self.service.channel, "{args}": args,
                  "{touser}": (args.split()[0].lstrip("@") if args else msg.user), "{count}": str(uses)}
        if any(v in template for v in ("{uptime}", "{game}", "{title}", "{viewers}")):
            info = await self.service.stream_info()
            values.update({
                "{uptime}": format_uptime(info["started_at"]) if info else "hors ligne",
                "{game}": info["game_name"] if info else "inconnu",
                "{title}": info["title"] if info else "hors ligne",
                "{viewers}": str(info["viewer_count"]) if info else "0"})
        for k, v in values.items():
            template = template.replace(k, v)
        return template
