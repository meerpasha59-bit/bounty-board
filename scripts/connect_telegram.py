"""Connect a Telegram bot to your Switch server (same as Console > Chat apps > Telegram).

  TELEGRAM_BOT_TOKEN=... python scripts/connect_telegram.py

Then add the bot to a Telegram group, and Switch turns that group into a room.
Run `python -m bounty_board rooms` to find it and `python -m bounty_board attach ROOM_ID`
to put the three agents in it. The token is read from the environment and never printed.
"""
import os
import httpx

token = os.environ["TELEGRAM_BOT_TOKEN"]
me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=30).json()
if not me.get("ok"):
    raise SystemExit("Telegram rejected the token")
user = me["result"]["username"]
g = httpx.Client(base_url=os.environ.get("SWITCH_URL", "http://127.0.0.1:8000") + "/gateway", timeout=60, trust_env=False)
g.post("/auth/login", json={"email": os.environ["GATEWAY_ADMIN_EMAIL"],
                            "password": os.environ["GATEWAY_ADMIN_PASSWORD"]}).raise_for_status()
r = g.post("/collaborations", json={
    "bridge_type": "telegram", "display_name": "Bounty Board Telegram",
    "connection_config": {"bot_token": token, "bot_username": user},
    "set_as_default": True, "channel_creation_enabled": False,
})
print(r.status_code, f"bot @{user}", "connected" if r.status_code < 300 else r.text[:300].replace(token, "***"))
