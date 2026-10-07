"""Génère les tokens Twitch (flux Authorization Code) à lancer sur VOTRE PC (pas sur le Pi).

Prérequis : application créée sur https://dev.twitch.tv/console/apps avec l'URL de redirection
http://localhost:3000 . Connectez-vous dans le navigateur avec le COMPTE QUI SERA LE BOT
(le diffuseur lui-même, ou un compte bot modérateur de la chaîne).

    python scripts/get_token.py <CLIENT_ID> <CLIENT_SECRET>
"""
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

SCOPES = "chat:read chat:edit moderator:manage:banned_users moderator:manage:chat_messages"
REDIRECT = "http://localhost:3000"

if len(sys.argv) != 3:
    raise SystemExit(__doc__)
client_id, client_secret = sys.argv[1:3]
state = secrets.token_urlsafe(16)
result: dict = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] == state and "code" in q:
            result["code"] = q["code"][0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("OK, vous pouvez fermer cet onglet.".encode() if result else b"Echec.")

    def log_message(self, *_):
        pass


url = "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode({
    "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES, "state": state})
print("Ouverture du navigateur...\n", url)
webbrowser.open(url)
server = HTTPServer(("localhost", 3000), Handler)
while "code" not in result:
    server.handle_request()

data = urllib.parse.urlencode({
    "client_id": client_id, "client_secret": client_secret, "code": result["code"],
    "grant_type": "authorization_code", "redirect_uri": REDIRECT}).encode()
with urllib.request.urlopen(urllib.request.Request("https://id.twitch.tv/oauth2/token", data=data)) as r:
    tok = json.load(r)
print("\n# À copier dans .env :")
print(f"TWITCH_BOT_TOKEN={tok['access_token']}")
print(f"TWITCH_REFRESH_TOKEN={tok['refresh_token']}")
