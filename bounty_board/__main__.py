"""Bounty Board CLI.

  python -m bounty_board register          register the three agents with Switch (once)
  python -m bounty_board rooms             list Switch rooms (and their chat app)
  python -m bounty_board attach ROOM_ID    add the three agents to a room
  python -m bounty_board test-room         create an internal room for a local dry run
  python -m bounty_board say ROOM_ID TEXT  post as the gateway admin (local testing only)
  python -m bounty_board run               run all three agents
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid

import httpx

from .agents import BUILDER, DATA, RULES, SCOUT, Board
from .switch_client import SwitchAgent

BASE = os.environ.get("SWITCH_URL", "http://127.0.0.1:8000")
KEYS = DATA / "agents.json"

AGENTS = {
    SCOUT: ("Scout", "Posts the best open hackathons and bounties every morning"),
    RULES: ("Rules Checker", "Reads official rules: India eligibility, deliverables, deadline in IST"),
    BUILDER: ("Builder", "Drafts build plans and sets deadline reminders, only after your approval"),
}


def gateway() -> httpx.Client:
    c = httpx.Client(base_url=BASE + "/gateway", timeout=30, trust_env=False)
    r = c.post("/auth/login", json={"email": os.environ["GATEWAY_ADMIN_EMAIL"],
                                    "password": os.environ["GATEWAY_ADMIN_PASSWORD"]})
    r.raise_for_status()
    return c


def agent_ids() -> dict[str, str]:
    return {n: v["id"] for n, v in json.loads(KEYS.read_text()).items()}


async def cmd_register() -> None:
    """Register the three agents as the signed-in gateway user, who then owns them."""
    DATA.mkdir(parents=True, exist_ok=True)
    g = gateway()
    out = {}
    for name, (display, desc) in AGENTS.items():
        r = g.post("/agents/register", json={"agent_type": "opencode", "name": name, "description": desc,
                                             "display_name": display, "options": {}, "overwrite": True})
        r.raise_for_status()
        res = r.json()
        out[name] = {"id": res["id"], "api_key": res["api_key"]}
        print(f"registered {name} -> {res['id']}")
    KEYS.write_text(json.dumps(out, indent=2))
    os.chmod(KEYS, 0o600)


def cmd_rooms() -> None:
    g = gateway()
    for r in g.get("/rooms").json():
        print(r.get("id"), "|", r.get("name"), "|", r.get("bridge_type") or "internal")


def cmd_attach(room_id: str) -> None:
    g = gateway()
    ids = list(agent_ids().values())
    r = g.post(f"/rooms/{room_id}/agents", json={"agent_ids": ids, "join_event_listeners": ids[:1]})
    print(r.status_code, r.text[:300])


def cmd_test_room() -> None:
    """A Switch chat owned by the gateway admin, for a dry run with no chat app."""
    g = gateway()
    r = g.post("/chats", json={"requestId": str(uuid.uuid4()), "name": "bounty-board-dryrun",
                               "agentIds": list(agent_ids().values())})
    print(r.status_code, r.text[:400])


def cmd_say(room_id: str, text: str) -> None:
    g = gateway()
    ids = agent_ids()
    mention = [ids[n] for n in ids if f"@{n}" in text]
    r = g.post(f"/chats/{room_id}/messages", json={"requestId": str(uuid.uuid4()), "body": text, "mentionAgentIds": mention})
    print(r.status_code, r.text[:200])


async def cmd_run() -> None:
    keys = json.loads(KEYS.read_text())
    agents = {n: SwitchAgent(BASE, v["id"], v["api_key"], n) for n, v in keys.items()}
    board = Board(agents)
    await asyncio.gather(
        *(a.run(board.on_event) for a in agents.values()),
        board.daily_loop(),
        board.reminder_loop(),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = sys.argv[1:] or ["run"]
    cmd = args[0]
    if cmd == "register":
        asyncio.run(cmd_register())
    elif cmd == "rooms":
        cmd_rooms()
    elif cmd == "attach":
        cmd_attach(args[1])
    elif cmd == "test-room":
        cmd_test_room()
    elif cmd == "say":
        cmd_say(args[1], " ".join(args[2:]))
    elif cmd == "run":
        asyncio.run(cmd_run())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
