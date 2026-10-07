"""Module 1 : CocoProtect - automodération et sanctions progressives."""
import re
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..events import audit
from .base import BaseModule


class BlacklistEntry(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    regex: bool = False


class Caps(BaseModel):
    enabled: bool = False
    min_length: int = Field(12, ge=1, le=500)
    max_percent: int = Field(70, ge=10, le=100)


class Repeat(BaseModel):
    enabled: bool = False
    max_chars: int = Field(8, ge=2, le=100)      # même caractère répété plus de N fois


class Emotes(BaseModel):
    enabled: bool = False
    max_emotes: int = Field(10, ge=1, le=100)


class Links(BaseModel):
    enabled: bool = False
    whitelist: list[str] = Field(default_factory=list, max_length=200)


class Step(BaseModel):
    action: Literal["warn", "delete", "timeout", "ban"]
    duration: int = Field(60, ge=1, le=1_209_600)


class ProtectConfig(BaseModel):
    blacklist: list[BlacklistEntry] = Field(default_factory=list, max_length=500)
    caps: Caps = Caps()
    repeat: Repeat = Repeat()
    emotes: Emotes = Emotes()
    links: Links = Links()
    exempt_mods: bool = True
    exempt_vips: bool = True
    exempt_subs: bool = False
    steps: list[Step] = Field(
        default_factory=lambda: [Step(action="warn"), Step(action="delete"),
                                 Step(action="timeout", duration=60), Step(action="timeout", duration=600)],
        min_length=1, max_length=10)
    reset_minutes: int = Field(30, ge=1, le=1440)
    warn_message: str = Field("@{user} merci de respecter les règles du chat ({reason}).", max_length=300)

    @field_validator("blacklist")
    @classmethod
    def _validate_regex(cls, entries):
        for e in entries:
            if e.regex:
                try:
                    re.compile(e.pattern)
                except re.error as exc:
                    raise ValueError(f"Regex invalide « {e.pattern} » : {exc}") from exc
        return entries


LINK_RE = re.compile(r"(?i)\b(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,24})(?:/\S*)?")


class ProtectModule(BaseModule):
    name = "protect"
    label = "CocoProtect"
    description = "Automodération : liste noire, anti-spam (majuscules, répétitions, émotes, liens) et sanctions progressives."
    ConfigModel = ProtectConfig

    def __init__(self, service) -> None:
        super().__init__(service)
        self._offenses: dict[str, tuple[int, float]] = {}   # user_id -> (nb, dernier timestamp)
        self._permits: dict[str, float] = {}                # login -> expiration
        self._compiled: list[tuple[re.Pattern | None, str]] = []

    async def on_start(self) -> None:
        await self.on_config_changed()

    async def on_config_changed(self) -> None:
        self._compiled = [
            (re.compile(e.pattern, re.IGNORECASE) if e.regex else None, e.pattern.lower())
            for e in self.config.blacklist]

    async def handle_message(self, msg) -> bool:
        c = self.config
        text = msg.content
        # !permit <user> : autorise un lien pendant 60 s (mods uniquement)
        if (msg.is_mod or msg.is_broadcaster) and text.lower().startswith("!permit "):
            target = text.split()[1].lstrip("@").lower()
            self._permits[target] = time.monotonic() + 60
            await self.service.say(f"@{target} peut poster un lien pendant 60 secondes.")
            return True
        if (msg.is_broadcaster or (c.exempt_mods and msg.is_mod)
                or (c.exempt_vips and msg.is_vip) or (c.exempt_subs and msg.is_sub)):
            return False
        reason = self._detect(msg)
        if not reason:
            return False
        await self._sanction(msg, reason)
        return True

    def _detect(self, msg) -> str | None:
        c, text = self.config, msg.content
        low = text.lower()
        for rx, plain in self._compiled:
            if (rx.search(text) if rx else plain in low):
                return "mot interdit"
        if c.caps.enabled:
            letters = [ch for ch in text if ch.isalpha()]
            if len(letters) >= c.caps.min_length and \
                    100 * sum(ch.isupper() for ch in letters) / len(letters) >= c.caps.max_percent:
                return "trop de majuscules"
        if c.repeat.enabled and re.search(r"(.)\1{%d,}" % c.repeat.max_chars, text):
            return "caractères répétés"
        if c.emotes.enabled and msg.emote_count > c.emotes.max_emotes:
            return "abus d'émotes"
        if c.links.enabled and self._has_forbidden_link(msg):
            return "lien non autorisé"
        return None

    def _has_forbidden_link(self, msg) -> bool:
        if self._permits.get(msg.user.lower(), 0) > time.monotonic():
            return False
        allowed = [d.lower().lstrip(".") for d in self.config.links.whitelist]
        for domain in LINK_RE.findall(msg.content):
            domain = domain.lower()
            if not any(domain == a or domain.endswith("." + a) for a in allowed):
                return True
        return False

    async def _sanction(self, msg, reason: str) -> None:
        c, now = self.config, time.monotonic()
        count, last = self._offenses.get(msg.user_id, (0, 0.0))
        if now - last > c.reset_minutes * 60:
            count = 0
        count += 1
        self._offenses[msg.user_id] = (count, now)
        if len(self._offenses) > 2000:  # purge mémoire
            self._offenses = {k: v for k, v in self._offenses.items() if now - v[1] <= c.reset_minutes * 60}

        step = c.steps[min(count - 1, len(c.steps) - 1)]
        svc, label = self.service, f"CocoProtect : {reason}"
        action_performed = step.action

        if msg.is_mod:
            # Sur Twitch, l'API ne permet pas de timeout/ban un modo sans lui retirer son rôle
            # Donc on supprime son message et on l'avertit
            await svc.delete_message(msg.msg_id)
            if step.action in ("timeout", "ban"):
                action_performed = "delete+warn (modo)"
                await svc.say(f"@{msg.user} (modérateur) : votre message a été supprimé ({reason}).")
            elif step.action == "warn":
                await svc.say(c.warn_message.replace("{user}", msg.user).replace("{reason}", reason))
        else:
            if step.action == "warn":
                await svc.say(c.warn_message.replace("{user}", msg.user).replace("{reason}", reason))
            elif step.action == "delete":
                await svc.delete_message(msg.msg_id)
            elif step.action == "timeout":
                await svc.delete_message(msg.msg_id)
                await svc.timeout(msg.user_id, step.duration, label)
            elif step.action == "ban":
                await svc.ban(msg.user_id, label)

        detail = f" ({step.duration}s)" if step.action == "timeout" and not msg.is_mod else ""
        await audit("mod", f"{msg.user} — {reason} → {action_performed}{detail} (infraction n°{count})",
                    {"user": msg.user, "reason": reason, "action": action_performed, "message": msg.content[:200]},
                    channel=svc.id)
