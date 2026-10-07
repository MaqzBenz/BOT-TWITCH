"""Authentification admin : Argon2, JWT en cookie HttpOnly, CSRF, rate limiting."""
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Header, HTTPException, Request, Response

from .config import settings

ph = PasswordHasher()
_DUMMY_HASH = ph.hash(secrets.token_hex(16))  # égalise le temps de réponse si l'utilisateur est inconnu

if settings.admin_password_hash:
    ADMIN_HASH = settings.admin_password_hash
elif settings.admin_password:
    if len(settings.admin_password) < 12:
        raise RuntimeError("ADMIN_PASSWORD doit contenir au moins 12 caractères.")
    ADMIN_HASH = ph.hash(settings.admin_password)
else:
    raise RuntimeError("Définir ADMIN_PASSWORD_HASH (ou ADMIN_PASSWORD) dans .env.")

ALGO = "HS256"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def verify_credentials(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username.encode(), settings.admin_username.encode())
    try:
        ph.verify(ADMIN_HASH if user_ok else _DUMMY_HASH, password)
        return user_ok
    except (VerifyMismatchError, InvalidHashError):
        return False


def issue_session(response: Response) -> None:
    csrf = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": settings.admin_username, "csrf": csrf, "exp": exp}, settings.jwt_secret, ALGO)
    max_age = settings.jwt_expire_minutes * 60
    response.set_cookie("session", token, max_age=max_age, httponly=True,
                        samesite="strict", secure=settings.cookie_secure, path="/")
    # Lisible par le JS (double-submit) : renvoyé dans l'en-tête X-CSRF-Token
    response.set_cookie("csrf", csrf, max_age=max_age, httponly=False,
                        samesite="strict", secure=settings.cookie_secure, path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie("session", path="/")
    response.delete_cookie("csrf", path="/")


def require_admin(request: Request, x_csrf_token: str | None = Header(default=None)) -> str:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Non authentifié")
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session invalide ou expirée")
    if request.method not in SAFE_METHODS:
        if not x_csrf_token or not hmac.compare_digest(x_csrf_token, claims.get("csrf", "")):
            raise HTTPException(403, "Jeton CSRF invalide")
    return claims["sub"]


class RateLimiter:
    """Fenêtre glissante en mémoire, par IP."""

    def __init__(self, limit: int, window: int) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 5000:  # purge anti-fuite mémoire
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                del self.hits[k]
        return True


api_limiter = RateLimiter(limit=300, window=60)
login_limiter = RateLimiter(limit=5, window=60)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
