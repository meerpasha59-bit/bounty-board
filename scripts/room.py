"""Drive a Switch internal room as the signed-in person (local dry run / demo).
  python scripts/room.py new NAME        create a room with the three agents
  python scripts/room.py say ROOM TEXT   post as the person
  python scripts/room.py dump ROOM FILE  save the room timeline as JSON
"""
import json, os, sys, uuid
from pathlib import Path
import httpx

BASE = os.environ.get("SWITCH_URL", "http://127.0.0.1:8000") + "/gateway"
KEYS = Path(__file__).resolve().parent.parent / "data" / "agents.json"

def gw():
    c = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    c.post("/auth/login", json={"email": os.environ["GATEWAY_ADMIN_EMAIL"],
                                "password": os.environ["GATEWAY_ADMIN_PASSWORD"]}).raise_for_status()
    return c

def main():
    cmd, *a = sys.argv[1:]
    c = gw(); keys = json.loads(KEYS.read_text())
    if cmd == "new":
        r = c.post("/chats", json={"requestId": str(uuid.uuid4()), "name": a[0],
                                   "agentIds": [v["id"] for v in keys.values()]})
        print(r.status_code, r.text[:300])
    elif cmd == "say":
        text = " ".join(a[1:])
        mention = [v["id"] for n, v in keys.items() if f"@{n}" in text]
        r = c.post(f"/chats/{a[0]}/messages", json={"requestId": str(uuid.uuid4()), "body": text,
                                                     "mentionAgentIds": mention})
        print(r.status_code)
    elif cmd == "dump":
        d = c.get(f"/chats/{a[0]}/messages").json()
        msgs = d.get("messages", d.get("items", [])) if isinstance(d, dict) else d
        Path(a[1]).write_text(json.dumps(msgs, indent=1, ensure_ascii=False))
        for m in msgs:
            print("──", m["sender"]["name"], f"({m['sender']['kind']})", m.get("sentAt"))
            print(m.get("body"))

if __name__ == "__main__":
    main()
