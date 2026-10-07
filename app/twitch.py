"""Service Twitch partagé : une connexion chat (TwitchIO/IRC) pour N chaînes + appels Helix."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx
from twitchio.ext import commands

from .config import settings
from .events import audit, bus

log = logging.getLogger("cocobot.twitch")
HELIX = "https://api.twitch.tv/helix"


@dataclass
class ChatMessage:
    user: str
    user_id: str
    content: str
    msg_id: str
    is_mod: bool
    is_broadcaster: bool
    is_vip: bool
    is_sub: bool
    emote_count: int


def count_emotes(tag: str | None) -> int:
    """Tag IRC 'emotes' : '25:0-4,6-10/1902:12-16' -> 3."""
    if not tag:
        return 0
    return sum(len(g.split(":", 1)[1].split(",")) for g in tag.split("/") if ":" in g)


class ChatBot(commands.Bot):
    def __init__(self, svc: "TwitchService", channels: list[str]) -> None:
        super().__init__(token="oauth:" + svc.token, prefix="!", initial_channels=channels)
        self.svc = svc

    async def event_ready(self) -> None:
        self.svc.set_state("connected")
        await audit("system", f"Bot connecté en tant que {self.nick}")
        # Sécurité : forcer le join sur toutes les chaînes actives enregistrées
        chans = list(self.svc.registry.by_login)
        if chans:
            log.info("Connexion aux canaux Twitch : %s", chans)
            await self.join_channels(chans)

    async def event_channel_joined(self, channel) -> None:
        log.info("Canal Twitch rejoint : #%s", channel.name)
        await audit("system", f"Canal #{channel.name} rejoint")

    async def event_message(self, message) -> None:
        if message.echo or message.author is None or message.channel is None:
            return
        chan_name = message.channel.name.lower()
        ctx = self.svc.registry.by_login.get(chan_name)
        if ctx is None:  # chaîne désactivée ou retirée
            return
        a, tags = message.author, message.tags or {}
        msg = ChatMessage(
            user=a.name, user_id=str(a.id), content=message.content or "", msg_id=tags.get("id", ""),
            is_mod=bool(a.is_mod), is_broadcaster=bool(a.is_broadcaster), is_vip=bool(a.is_vip),
            is_sub=bool(a.is_subscriber), emote_count=count_emotes(tags.get("emotes")),
        )
        log.info("[#%s] %s: %s", chan_name, msg.user, msg.content)
        await audit("chat", f"{msg.user}: {msg.content}", channel=ctx.id)
        await ctx.manager.dispatch(msg)


class TwitchService:
    def __init__(self) -> None:
        self.token = settings.twitch_bot_token.removeprefix("oauth:")
        self.refresh_token = settings.twitch_refresh_token
        self.client_id = settings.twitch_client_id
        self.http = httpx.AsyncClient(timeout=10)
        self.bot: ChatBot | None = None
        self.registry = None  # ChannelRegistry
        self.bot_user_id = ""
        self.bot_login = ""
        self.state = "not_configured"
        self._task: asyncio.Task | None = None
        self._stream_cache: dict[str, tuple[float, dict | None]] = {}
        self._token_file = settings.data_path / "tokens.json"
        self._load_saved_tokens()

    # ---------- état ----------
    def set_state(self, state: str) -> None:
        self.state = state
        asyncio.create_task(bus.publish("status", self.status()))

    def status(self) -> dict:
        return {"twitch": self.state, "bot": self.bot_login}

    # ---------- cycle de vie ----------
    async def init(self) -> None:
        """Valide le token avant le chargement des chaînes (résolution des IDs)."""
        if not self.token:
            return
        try:
            await self._validate()
        except Exception as exc:  # noqa: BLE001
            log.warning("Validation du token impossible au démarrage : %s", exc)

    async def start(self) -> None:
        if not self.token:
            log.warning("Twitch non configuré (TWITCH_BOT_TOKEN).")
            return
        self.state = "connecting"
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.bot:
            try:
                await self.bot.close()
            except Exception:  # noqa: BLE001
                pass
        await self.http.aclose()

    async def _run(self) -> None:
        try:
            await self._validate()
            self.bot = ChatBot(self, list(self.registry.by_login))
            await self.bot.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Erreur Twitch")
            self.set_state("error")
            await audit("system", f"Erreur Twitch : {exc}")

    # ---------- gestion des chaînes à chaud ----------
    async def join(self, login: str) -> None:
        if self.bot and self.state == "connected":
            await self.bot.join_channels([login])

    async def part(self, login: str) -> None:
        if self.bot and self.state == "connected":
            await self.bot.part_channels([login])

    # ---------- tokens ----------
    def _load_saved_tokens(self) -> None:
        if self._token_file.exists():
            d = json.loads(self._token_file.read_text())
            if d.get("source") == settings.twitch_bot_token:  # ignorer si .env a changé
                self.token, self.refresh_token = d["access"], d["refresh"]

    async def _validate(self) -> None:
        r = await self.http.get("https://id.twitch.tv/oauth2/validate", headers={"Authorization": f"OAuth {self.token}"})
        if r.status_code == 401 and await self._refresh():
            return await self._validate()
        r.raise_for_status()
        d = r.json()
        self.bot_user_id, self.bot_login = d["user_id"], d["login"]
        self.client_id = self.client_id or d["client_id"]
        missing = {"moderator:manage:banned_users", "moderator:manage:chat_messages"} - set(d.get("scopes", []))
        if missing:
            await audit("system", f"Scopes manquants (modération impossible) : {', '.join(sorted(missing))}")

    async def _refresh(self) -> bool:
        if not (self.refresh_token and settings.twitch_client_id and settings.twitch_client_secret):
            return False
        r = await self.http.post("https://id.twitch.tv/oauth2/token", data={
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
            "client_id": settings.twitch_client_id, "client_secret": settings.twitch_client_secret})
        if r.status_code != 200:
            log.error("Refresh token refusé : %s", r.text)
            return False
        d = r.json()
        self.token, self.refresh_token = d["access_token"], d.get("refresh_token", self.refresh_token)
        self._token_file.write_text(json.dumps(
            {"source": settings.twitch_bot_token, "access": self.token, "refresh": self.refresh_token}))
        self._token_file.chmod(0o600)
        await audit("system", "Token Twitch renouvelé")
        return True

    # ---------- Helix ----------
    async def helix(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self.token}", "Client-Id": self.client_id}
            r = await self.http.request(method, HELIX + path, headers=headers, **kw)
            if r.status_code == 401 and attempt == 0 and await self._refresh():
                continue
            return r
        return r  # pragma: no cover

    async def resolve_user(self, login: str) -> tuple[str, str] | None:
        """(id, login) d'un compte Twitch, ou None s'il n'existe pas."""
        r = await self.helix("GET", "/users", params={"login": login})
        if r.status_code == 200 and r.json()["data"]:
            d = r.json()["data"][0]
            return d["id"], d["login"]
        return None

    async def stream_info(self, login: str) -> dict | None:
        """Infos du stream en direct (cache 30 s) ; None si hors ligne."""
        ts, cached = self._stream_cache.get(login, (0.0, None))
        if time.monotonic() - ts < 30:
            return cached
        info = None
        try:
            r = await self.helix("GET", "/streams", params={"user_login": login})
            if r.status_code == 200 and r.json()["data"]:
                info = r.json()["data"][0]
        except httpx.HTTPError:
            pass
        self._stream_cache[login] = (time.monotonic(), info)
        return info

    # ---------- actions ----------
    async def say(self, login: str, text: str) -> None:
        if not (self.bot and self.state == "connected"):
            log.warning("Impossible d'envoyer le message sur #%s : bot non connecté", login)
            return
        chan = self.bot.get_channel(login)
        if chan:
            await chan.send(text[:450])
            log.info("Message envoyé sur #%s : %s", login, text[:60])
        else:
            log.warning("Canal #%s non trouvé dans le cache local TwitchIO, tentative via _ws direct", login)
            try:
                await self.bot._ws.send_privmsg(login, text[:450])
                log.info("Message envoyé via _ws direct sur #%s : %s", login, text[:60])
            except Exception as exc:
                log.error("Échec de l'envoi sur #%s : %s", login, exc)

    async def ban(self, broadcaster_id: str, user_id: str, reason: str, duration: int | None, audit_channel: int) -> bool:
        body = {"user_id": user_id, "reason": reason[:500]}
        if duration:
            body["duration"] = duration
        r = await self.helix("POST", "/moderation/bans", json={"data": body},
                             params={"broadcaster_id": broadcaster_id, "moderator_id": self.bot_user_id})
        if r.status_code != 200:
            await audit("system", f"Échec sanction Helix ({r.status_code}) : {r.text[:150]}", channel=audit_channel)
        return r.status_code == 200

    async def delete_message(self, broadcaster_id: str, msg_id: str) -> bool:
        if not msg_id:
            return False
        r = await self.helix("DELETE", "/moderation/chat", params={
            "broadcaster_id": broadcaster_id, "moderator_id": self.bot_user_id, "message_id": msg_id})
        return r.status_code == 204
