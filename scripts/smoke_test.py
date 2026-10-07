import asyncio, os, tempfile
os.environ.update(DATA_DIR=tempfile.mkdtemp(), JWT_SECRET="x" * 48, ADMIN_PASSWORD="supersecretpass1", COOKIE_SECURE="false")
from fastapi.testclient import TestClient
from app.main import app
from app.twitch import ChatMessage, TwitchService

counter = iter(range(100, 200))
async def fake_resolve(self, login): return str(next(counter)), login
sent, bans = [], []
async def fake_say(self, login, text): sent.append((login, text))
async def fake_ban(self, bid, uid, reason, dur, ac): bans.append((bid, uid, dur)); return True
async def fake_del(self, bid, mid): return True
TwitchService.resolve_user, TwitchService.say, TwitchService.ban, TwitchService.delete_message = fake_resolve, fake_say, fake_ban, fake_del

def msg(user, text): return ChatMessage(user, "u1", text, "m1", False, False, False, False, 0)

with TestClient(app) as c:
    assert c.get("/api/channels").status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "supersecretpass1"}).status_code == 200
    H = {"X-CSRF-Token": c.cookies["csrf"]}
    a = c.post("/api/channels", json={"login": "Alpha"}, headers=H).json(); b = c.post("/api/channels", json={"login": "beta_"}, headers=H).json()
    print(a, b)
    assert c.post("/api/channels", json={"login": "alpha"}, headers=H).status_code == 409
    A, B = a["id"], b["id"]
    # isolation des configs
    c.put(f"/api/channels/{A}/modules/commands/enabled", json={"enabled": True}, headers=H)
    r = c.post(f"/api/channels/{A}/commands", json={"name": "hi", "response": "Salut {user} sur {channel}", "cooldown_global": 0, "cooldown_user": 0}, headers=H); assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert c.get(f"/api/channels/{B}/commands").json() == []
    assert c.put(f"/api/channels/{B}/commands/{cid}", json={"name": "x", "response": "y"}, headers=H).status_code == 404   # pas d'accès inter-chaînes
    assert c.post(f"/api/channels/{B}/commands", json={"name": "hi", "response": "autre"}, headers=H).status_code == 201     # même nom OK dans une autre chaîne
    reg = app.state.registry
    c.put(f"/api/channels/{A}/modules/protect/enabled", json={"enabled": True}, headers=H)
    cfg = c.get(f"/api/channels/{A}/modules").json()[2]["config"]; cfg["blacklist"] = [{"pattern": "spam", "regex": False}]
    assert c.put(f"/api/channels/{A}/modules/protect/config", json=cfg, headers=H).status_code == 200
    portal = c.portal if hasattr(c, "portal") else None
    async def run():
        await reg.by_id[A].manager.dispatch(msg("bob", "!hi"))
        await reg.by_id[B].manager.dispatch(msg("bob", "!hi"))       # commands désactivé sur B
        await reg.by_id[A].manager.dispatch(msg("bob", "du spam ici"))
        await reg.by_id[B].manager.dispatch(msg("bob", "du spam ici"))  # protect désactivé sur B
    c.portal.call(run)
    print(sent, bans)
    assert sent[0] == ("alpha", "Salut bob sur alpha") and len(sent) == 2 and ("warn" or True)
    assert len(c.get(f"/api/channels/{B}/logs").json()) < len(c.get(f"/api/channels/{A}/logs").json())
    assert c.put(f"/api/channels/{B}/enabled", json={"enabled": False}, headers=H).json()["enabled"] is False
    assert "beta_" not in reg.by_login
    assert c.delete(f"/api/channels/{B}", headers=H).status_code == 204
    assert [x["login"] for x in c.get("/api/channels").json()] == ["alpha"]
    assert c.get(f"/api/channels/{B}/commands").status_code == 404
print("OK")
