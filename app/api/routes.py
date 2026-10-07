"""Routes API : auth, chaînes, puis modules / commandes / timers / logs PAR chaîne, SSE."""
import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..channels import ChannelContext
from ..db import Command, LogEntry, Session, Timer
from ..events import audit, bus, serialize_log
from ..security import (client_ip, clear_session, issue_session, login_limiter,
                        require_admin, verify_credentials)

public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])


def get_ctx(cid: int, request: Request) -> ChannelContext:
    ctx = request.app.state.registry.by_id.get(cid)
    if ctx is None:
        raise HTTPException(404, "Chaîne inconnue")
    return ctx


# ---------------- Auth ----------------
class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@public.get("/health")
async def health():
    return {"ok": True}


@public.post("/auth/login")
async def login(data: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    if not login_limiter.allow(ip):
        raise HTTPException(429, "Trop de tentatives, réessayez dans une minute.")
    if not verify_credentials(data.username, data.password):
        await audit("auth", f"Échec de connexion depuis {ip}")
        raise HTTPException(401, "Identifiants invalides")
    issue_session(response)
    await audit("auth", f"Connexion admin depuis {ip}")
    return {"ok": True}


@public.post("/auth/logout")
async def logout(response: Response):
    clear_session(response)
    return {"ok": True}


@router.get("/auth/me")
async def me(user: str = Depends(require_admin)):
    return {"user": user}


@router.get("/status")
async def status(request: Request):
    return request.app.state.twitch.status()


# ---------------- Chaînes ----------------
class ChannelIn(BaseModel):
    login: str = Field(pattern=r"^#?[A-Za-z0-9_]{3,25}$")


class EnabledIn(BaseModel):
    enabled: bool


@router.get("/channels")
async def list_channels(request: Request):
    return [c.describe() for c in request.app.state.registry.by_id.values()]


@router.post("/channels", status_code=201)
async def add_channel(data: ChannelIn, request: Request):
    try:
        ctx = await request.app.state.registry.add(data.login)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return ctx.describe()


@router.put("/channels/{cid}/enabled")
async def set_channel_enabled(cid: int, body: EnabledIn, request: Request):
    get_ctx(cid, request)
    return (await request.app.state.registry.set_enabled(cid, body.enabled)).describe()


@router.delete("/channels/{cid}", status_code=204)
async def delete_channel(cid: int, request: Request):
    get_ctx(cid, request)
    await request.app.state.registry.remove(cid)


# ---------------- Modules (par chaîne) ----------------
def _module(ctx: ChannelContext, name: str):
    if name not in ctx.manager.modules:
        raise HTTPException(404, "Module inconnu")
    return ctx.manager


@router.get("/channels/{cid}/modules")
async def list_modules(ctx: ChannelContext = Depends(get_ctx)):
    return [ctx.manager.describe(n) for n in ctx.manager.modules]


@router.put("/channels/{cid}/modules/{name}/enabled")
async def set_enabled(name: str, body: EnabledIn, ctx: ChannelContext = Depends(get_ctx)):
    mgr = _module(ctx, name)
    await mgr.set_enabled(name, body.enabled)
    return mgr.describe(name)


@router.put("/channels/{cid}/modules/{name}/config")
async def set_config(name: str, body: dict, ctx: ChannelContext = Depends(get_ctx)):
    mgr = _module(ctx, name)
    try:
        await mgr.set_config(name, body)
    except ValidationError as exc:
        msgs = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise HTTPException(422, "; ".join(msgs))
    return mgr.describe(name)


# ---------------- Commandes (par chaîne) ----------------
class CommandIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{1,25}$")
    response: str = Field(min_length=1, max_length=500)
    enabled: bool = True
    permission: Literal["everyone", "vip", "mod", "broadcaster"] = "everyone"
    cooldown_global: int = Field(5, ge=0, le=86400)
    cooldown_user: int = Field(15, ge=0, le=86400)


def _cmd_dict(c: Command) -> dict:
    return {k: getattr(c, k) for k in ("id", "name", "response", "enabled", "permission",
                                       "cooldown_global", "cooldown_user", "uses")}


async def _get_owned(model, item_id: int, ctx: ChannelContext, session):
    obj = await session.get(model, item_id)
    if not obj or obj.channel_id != ctx.id:  # jamais d'accès inter-chaînes
        raise HTTPException(404, "Introuvable")
    return obj


@router.get("/channels/{cid}/commands")
async def list_commands(ctx: ChannelContext = Depends(get_ctx)):
    async with Session() as s:
        q = select(Command).where(Command.channel_id == ctx.id).order_by(Command.name)
        return [_cmd_dict(c) for c in (await s.execute(q)).scalars()]


async def _save_command(ctx: ChannelContext, item_id: int | None, data: CommandIn) -> dict:
    async with Session() as s:
        if item_id is None:
            cmd = Command(channel_id=ctx.id)
            s.add(cmd)
        else:
            cmd = await _get_owned(Command, item_id, ctx, s)
        for k, v in data.model_dump().items():
            setattr(cmd, k, v.lower() if k == "name" else v)
        try:
            await s.commit()
        except IntegrityError:
            raise HTTPException(409, "Cette commande existe déjà")
    await ctx.manager.modules["commands"].reload()
    return _cmd_dict(cmd)


@router.post("/channels/{cid}/commands", status_code=201)
async def create_command(data: CommandIn, ctx: ChannelContext = Depends(get_ctx)):
    res = await _save_command(ctx, None, data)
    await audit("config", f"Commande !{res['name']} créée", channel=ctx.id)
    return res


@router.put("/channels/{cid}/commands/{item_id}")
async def update_command(item_id: int, data: CommandIn, ctx: ChannelContext = Depends(get_ctx)):
    res = await _save_command(ctx, item_id, data)
    await audit("config", f"Commande !{res['name']} modifiée", channel=ctx.id)
    return res


@router.delete("/channels/{cid}/commands/{item_id}", status_code=204)
async def delete_command(item_id: int, ctx: ChannelContext = Depends(get_ctx)):
    async with Session() as s:
        cmd = await _get_owned(Command, item_id, ctx, s)
        name = cmd.name
        await s.delete(cmd)
        await s.commit()
    await ctx.manager.modules["commands"].reload()
    await audit("config", f"Commande !{name} supprimée", channel=ctx.id)


# ---------------- Timers (par chaîne) ----------------
class TimerIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    message: str = Field(min_length=1, max_length=500)
    interval_minutes: int = Field(15, ge=1, le=1440)
    min_messages: int = Field(5, ge=0, le=10000)
    enabled: bool = True


def _timer_dict(t: Timer) -> dict:
    return {k: getattr(t, k) for k in ("id", "name", "message", "interval_minutes", "min_messages", "enabled")}


@router.get("/channels/{cid}/timers")
async def list_timers(ctx: ChannelContext = Depends(get_ctx)):
    async with Session() as s:
        q = select(Timer).where(Timer.channel_id == ctx.id).order_by(Timer.id)
        return [_timer_dict(t) for t in (await s.execute(q)).scalars()]


@router.post("/channels/{cid}/timers", status_code=201)
async def create_timer(data: TimerIn, ctx: ChannelContext = Depends(get_ctx)):
    async with Session() as s:
        t = Timer(channel_id=ctx.id, **data.model_dump())
        s.add(t)
        await s.commit()
    await ctx.manager.modules["timers"].reload()
    await audit("config", f"Timer « {t.name} » créé", channel=ctx.id)
    return _timer_dict(t)


@router.put("/channels/{cid}/timers/{item_id}")
async def update_timer(item_id: int, data: TimerIn, ctx: ChannelContext = Depends(get_ctx)):
    async with Session() as s:
        t = await _get_owned(Timer, item_id, ctx, s)
        for k, v in data.model_dump().items():
            setattr(t, k, v)
        await s.commit()
    await ctx.manager.modules["timers"].reload()
    await audit("config", f"Timer « {t.name} » modifié", channel=ctx.id)
    return _timer_dict(t)


@router.delete("/channels/{cid}/timers/{item_id}", status_code=204)
async def delete_timer(item_id: int, ctx: ChannelContext = Depends(get_ctx)):
    async with Session() as s:
        t = await _get_owned(Timer, item_id, ctx, s)
        name = t.name
        await s.delete(t)
        await s.commit()
    await ctx.manager.modules["timers"].reload()
    await audit("config", f"Timer « {name} » supprimé", channel=ctx.id)


# ---------------- Logs & temps réel ----------------
@router.get("/channels/{cid}/logs")
async def get_logs(limit: int = 100, category: str | None = None, ctx: ChannelContext = Depends(get_ctx)):
    """Logs de la chaîne + événements globaux (connexions admin, système)."""
    q = (select(LogEntry).where(or_(LogEntry.channel_id == ctx.id, LogEntry.channel_id.is_(None)))
         .order_by(LogEntry.id.desc()).limit(max(1, min(limit, 500))))
    if category:
        q = q.where(LogEntry.category == category)
    async with Session() as s:
        return [serialize_log(e) for e in (await s.execute(q)).scalars()]


@router.get("/events")
async def events(request: Request):
    q = bus.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {ev['type']}\ndata: {json.dumps(ev['data'])}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
