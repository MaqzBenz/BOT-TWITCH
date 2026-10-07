# 🥥 CocoBot / CocoProtect

Bot Twitch modulaire + dashboard d'administration sécurisé, conçu pour un **Raspberry Pi (ARM64)** via Docker Compose.

**Stack** : Python 3.12 · FastAPI · TwitchIO 2 (chat IRC) + API Helix (modération) · SQLite (SQLAlchemy async, WAL) · dashboard vanilla JS/CSS (zéro dépendance front, SSE temps réel).

## Arborescence

```
.
├── docker-compose.yml        # service + volume persistant + restart: unless-stopped
├── Dockerfile                # multi-stage, linux/arm64, utilisateur non-root
├── requirements.txt
├── .env.example              # toutes les variables
├── scripts/get_token.py      # génération des tokens Twitch (sur votre PC)
└── app/
    ├── main.py               # FastAPI, lifespan, CSP/headers, rate limit global
    ├── run.py                # lance uvicorn sur APP_PORT
    ├── config.py             # settings (.env)
    ├── security.py           # Argon2, JWT cookie HttpOnly, CSRF, rate limiter
    ├── db.py                 # moteur SQLite + modèles
    ├── events.py             # bus SSE + journal d'audit
    ├── twitch.py             # connexion chat partagée (N chaînes) + Helix (ban/timeout/delete, stream)
    ├── channels.py           # registre des chaînes : un contexte + des modules indépendants par chaîne
    ├── hash_password.py      # génère ADMIN_PASSWORD_HASH
    ├── api/routes.py         # auth, modules, commandes, timers, logs, SSE
    ├── modules/
    │   ├── base.py           # contrat BaseModule (enabled, config, hooks)
    │   ├── manager.py        # activation/désactivation à chaud + persistance
    │   ├── protect.py        # Module 1 : CocoProtect
    │   ├── commands.py       # Module 2 : commandes
    │   ├── timers.py         # Module 3 : timers (temps ET messages)
    │   └── logs.py           # Module 4 : logs & audit
    └── static/               # index.html, login.html, app.css, app.js, login.js
```

## Installation pas à pas

### 1. Créer l'application Twitch
1. Allez sur <https://dev.twitch.tv/console/apps> → **Register Your Application**.
2. **OAuth Redirect URL** : `http://localhost:3000` · **Category** : Chat Bot · **Client Type** : *Confidential*.
3. Notez le **Client ID** puis générez un **Client Secret**.

### 2. Générer les tokens (sur votre PC, avec Python)
Connectez-vous au navigateur avec le **compte du bot** (ou le compte diffuseur). Si c'est un compte bot distinct, il doit être **modérateur** de la chaîne (`/mod nomdubot`).
```bash
python scripts/get_token.py <CLIENT_ID> <CLIENT_SECRET>
```
Le script affiche `TWITCH_BOT_TOKEN` et `TWITCH_REFRESH_TOKEN` (scopes : `chat:read chat:edit moderator:manage:banned_users moderator:manage:chat_messages`). Le refresh token permet le renouvellement automatique.

### 3. Configurer
```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # -> JWT_SECRET
docker compose build
docker compose run --rm cocobot python -m app.hash_password      # -> ADMIN_PASSWORD_HASH (entre quotes simples)
nano .env
```
Renseignez : `APP_PORT`, `JWT_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_BOT_TOKEN`, `TWITCH_REFRESH_TOKEN`. `TWITCH_CHANNEL` est facultatif (première chaîne seulement).

### Plusieurs chaînes
Un seul bot (un seul compte/token) rejoint autant de chaînes que vous voulez. Onglet **Chaînes** du dashboard : ajouter / activer / désactiver / retirer à chaud, sans redémarrage. Chaque chaîne possède sa **propre configuration** (modules, liste noire, commandes, timers, logs) ; le sélecteur en haut choisit la chaîne éditée. Sur chaque chaîne où vous voulez des sanctions, le compte du bot doit être **modérateur** (`/mod nomdubot`).

> Mise à jour depuis la v1 (mono-chaîne) : l'ancienne base est conservée sous `cocobot.v1.bak` et une base multi-chaînes est recréée (reconfigurer commandes/timers).

> ℹ️ **HTTP** : le dashboard est servi en HTTP (`COOKIE_SECURE=false` par défaut) sur un port de la plage **12000–13000** (`APP_PORT`, défaut `12080`). Utilisez-le sur votre réseau local ou via un VPN (Tailscale/WireGuard) ; ne l'exposez pas tel quel sur Internet.

### 4. Lancer
```bash
docker compose up -d
docker compose logs -f cocobot
```
Dashboard : `http://<ip-du-pi>:12080` (ou votre `APP_PORT`).

### Variante Portainer
Stacks → Add stack → *Repository* (URL Git, compose path `docker-compose.yml`) ou *Web editor*, puis renseignez les variables du `.env` dans **Environment variables**. Le compose n'a pas besoin de fichier `.env`.

### 5. Mise à jour / sauvegarde
```bash
git pull && docker compose up -d --build
docker run --rm -v bot_twitch_cocobot_data:/data -v "$PWD":/b alpine tar czf /b/cocobot-backup.tgz /data   # nom du volume : `docker volume ls`
```

## Sécurité implémentée
- Login local, mot de passe **Argon2id** ; comparaison à temps constant (pas d'énumération d'utilisateur).
- Session **JWT** en cookie `HttpOnly; SameSite=Strict; Secure` + **CSRF** (double-submit, en-tête `X-CSRF-Token` vérifié sur toute requête mutante).
- **Rate limiting** : 5 tentatives de login/min/IP, 300 requêtes API/min/IP (en mémoire ; derrière un reverse proxy, l'IP vue est celle du proxy → ajoutez un limiteur côté proxy).
- Validation stricte des entrées (Pydantic), regex de la liste noire compilées/validées.
- **CSP** stricte (pas de script/style inline, pas de CDN), `X-Frame-Options: DENY`, `no-store`.
- Aucun secret Twitch côté client ; conteneur non-root, `read_only`, `cap_drop: ALL`, `no-new-privileges`, RAM limitée à 256 Mo.

## Utilisation des modules
Chaque module s'active/se désactive à chaud via son interrupteur (état persistant en base, aucun redémarrage).

| Module | Détails |
|---|---|
| **CocoProtect** | Liste noire (texte ou `re:regex`), majuscules, répétitions, émotes, liens + whitelist de domaines. `!permit <user>` (mods) autorise un lien 60 s. Sanctions progressives : `warn → delete → timeout:60 → timeout:600 → ban` (éditable). |
| **Commandes** | Variables `{user} {touser} {args} {channel} {uptime} {game} {title} {viewers} {count}`, permissions, cooldowns global/utilisateur (mods exemptés). |
| **Timers** | Envoi si intervalle écoulé **ET** quota de messages atteint (option « uniquement en live »). |
| **Logs** | Flux SSE en direct : sanctions, commandes, timers, config, connexions. Rétention configurable. |

## Notes
- Le bot a besoin du statut **modérateur** (ou d'être le diffuseur) pour sanctionner ; sinon l'erreur Helix apparaît dans les logs.
- Le module Logs est activé par défaut ; les autres sont désactivés au premier lancement.
- Pour développer sans Docker : `pip install -r requirements.txt && python -m app.run` (avec un `.env` local, `DATA_DIR=./data`).
