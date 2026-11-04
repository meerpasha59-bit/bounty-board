# Running Switch without Docker

Bounty Board was developed on a headless Linux ARM64 box with no Docker, no root and no display, so Switch's `just up` stack and Switch Console couldn't run there. This is how `switch-core` ran natively instead. Use the normal Docker quickstart if you can.

## 1. User-space PostgreSQL 16
```bash
# micromamba needs no root
micromamba create -y -p ./.pg -c conda-forge postgresql=16
./.pg/bin/initdb -D ./pgdata -U postgres --auth=md5 --pwfile=<(echo "$DB_OWNER_PASSWORD")
./.pg/bin/pg_ctl -D ./pgdata -l pg.log start
PGPASSWORD=$DB_OWNER_PASSWORD ./.pg/bin/psql -h 127.0.0.1 -U postgres -c "create database switch"
PGPASSWORD=$DB_OWNER_PASSWORD ./.pg/bin/psql -h 127.0.0.1 -U postgres -c "create role switch_app login password '$DB_PASSWORD'"
```
`switch-core` connects as the restricted role (`DB_USER`) at runtime and runs its Alembic migrations as the owner (`DB_OWNER_USER`) on startup.

## 2. switch-core
```bash
git clone https://github.com/sandbox-quantum/switch && cd switch/core
uv sync --frozen --no-dev
set -a; . ./switch.env; set +a
.venv/bin/python -m switch_core.main      # serves http://127.0.0.1:8000
```
`switch.env` holds the same variables as the Docker stack's `.env`:
`DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_OWNER_USER, DB_OWNER_PASSWORD, SECRET_KEYS, GATEWAY_ADMIN_EMAIL, GATEWAY_ADMIN_PASSWORD, AGENT_REGISTRATION_TOKEN, ID_SERVER_NAME=localhost, GATEWAY_COOKIE_SECURE=false, GATEWAY_PUBLIC_URL`.
Generate the secrets with the repo's `deploy/local/generate-env-secrets.sh`.

Check: `curl http://127.0.0.1:8000/health` → `{"status":"ok"}`.

## 3. Chat app
Without Docker there is no bundled Mattermost, so use Switch's own chat rooms (`scripts/room.py new …`) or connect Telegram with `scripts/connect_telegram.py`. Switch's Telegram bridge uses a webhook, so the server needs a public HTTPS address in `GATEWAY_PUBLIC_URL` — a free Cloudflare quick tunnel (`cloudflared tunnel --url http://127.0.0.1:8000`) works for a demo.

## Gotchas we hit
- Put the virtualenvs on a local disk. On a slow network filesystem `switch_core` took minutes just to import.
- If an HTTP proxy is set in the environment, keep it away from localhost (`trust_env=False` in the clients here).
- Agents added to a room after they connect must claim it (`connection/subscribe`); `switch_client.py` re-checks `list_rooms` every ~30 s.
