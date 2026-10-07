"""Application FastAPI : lifecycle, middlewares de sécurité, fichiers statiques."""
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import public, router
from .channels import ChannelRegistry
from .config import settings
from .db import init_db
from .events import audit, state
from .security import api_limiter, client_ip
from .twitch import TwitchService

STATIC = Path(__file__).parent / "static"

logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_fh = RotatingFileHandler(settings.data_path / "logs" / "cocobot.log", maxBytes=2_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_fh)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    twitch = TwitchService()
    registry = ChannelRegistry(twitch)
    twitch.registry = registry
    state.registry = registry
    app.state.twitch, app.state.registry = twitch, registry
    await twitch.init()
    await registry.load(seed_login=settings.twitch_channel.lower().lstrip("#"))
    await audit("system", f"CocoBot démarré ({len(registry.by_id)} chaîne(s))")
    await twitch.start()
    yield
    await registry.shutdown()
    await twitch.stop()


app = FastAPI(title="CocoBot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        if not api_limiter.allow(client_ip(request)):
            return JSONResponse({"detail": "Trop de requêtes"}, status_code=429)
    response = await call_next(request)
    response.headers.update({
        "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Cache-Control": response.headers.get("Cache-Control", "no-store")})
    return response


app.include_router(public)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(STATIC / "login.html")
