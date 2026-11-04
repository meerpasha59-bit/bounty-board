"""Minimal Switch agent-protocol client: SSE down, HTTP up.

Implements the contract in Switch's docs/official/internals/agent-protocol.md:
register once with the registration token, open one SSE stream per agent
(scope=all), heartbeat every 2 s with the cursor, and call operations at
POST /agents/{id}/ops/{name}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("bounty_board.switch")

EventHandler = Callable[["SwitchAgent", dict[str, Any]], Awaitable[None]]


def _client(base_url: str, token: str | None = None, timeout: float | None = 30) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # trust_env=False: a corporate/sandbox HTTP proxy must not swallow localhost.
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, trust_env=False)


async def register_agent(
    base_url: str,
    registration_token: str,
    name: str,
    description: str,
    display_name: str,
    agent_type: str = "opencode",
) -> dict[str, str]:
    """POST /agents/register-known. Returns {id, api_key}; the key is shown once."""
    async with _client(base_url, registration_token) as c:
        r = await c.post(
            "/agents/register-known",
            json={
                "agent_type": agent_type,
                "name": name,
                "description": description,
                "display_name": display_name,
                "options": {},
                "overwrite": True,
            },
        )
        r.raise_for_status()
        return r.json()


class SwitchAgent:
    def __init__(self, base_url: str, agent_id: str, api_key: str, name: str):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self.name = name
        self.connection_id = str(uuid.uuid4())
        self.cursor: int | None = None
        self.rooms: list[str] = []
        self._http = _client(self.base_url, api_key)

    # ── operations ──────────────────────────────────────────────────────
    async def op(self, name: str, _bind: bool = False, **args: Any) -> Any:
        headers = {"X-Switch-Connection-Id": self.connection_id} if _bind else {}
        r = await self._http.post(f"/agents/{self.agent_id}/ops/{name}", json=args, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.name}: op {name} -> {r.status_code} {r.text[:300]}")
        return r.json().get("result")

    async def say(self, room_id: str, body: str) -> None:
        """Post into a room without needing a claimed connection."""
        r = await self._http.post(
            f"/agents/{self.agent_id}/message", json={"room_id": room_id, "content": body}
        )
        if r.status_code >= 400:
            log.error("%s: post failed %s %s", self.name, r.status_code, r.text[:300])

    async def history(self, room_id: str, limit: int = 60) -> list[dict[str, Any]]:
        """Flattened room timeline (oldest first) via the read_context operation."""
        ctx = await self.op("read_context", limit=limit, room_id=room_id)
        out: list[dict[str, Any]] = []
        for t in (ctx or {}).get("threads", []):
            out.append(t["root"])
            out.extend(t.get("replies", []))
        out.sort(key=lambda e: e.get("timestamp") or 0)
        return out

    async def room_ids(self) -> list[str]:
        rooms = await self.op("list_rooms") or []
        return sorted(r["room_id"] for r in rooms if not r.get("archived"))

    async def _claim_new_rooms(self) -> None:
        """Rooms the agent is added to later (a new Telegram group) get claimed too."""
        try:
            for rid in await self.room_ids():
                if rid not in self.rooms:
                    r = await self._http.post(
                        f"/agents/{self.agent_id}/connection/subscribe",
                        json={"connection_id": self.connection_id, "room_id": rid, "takeover": True},
                    )
                    log.info("%s claimed new room %s -> %s", self.name, rid, r.status_code)
                    self.rooms.append(rid)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s room refresh failed: %s", self.name, exc)

    # ── stream + heartbeat ──────────────────────────────────────────────
    async def _beat(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(2)
            n += 1
            if n % 15 == 0:
                await self._claim_new_rooms()
            body: dict[str, Any] = {"connection_id": self.connection_id}
            if self.cursor is not None:
                body["cursor"] = self.cursor
            try:
                r = await self._http.post(f"/agents/{self.agent_id}/connection/beat", json=body)
                if r.status_code in (404, 409):
                    return  # reopen the stream
                if r.status_code == 200:
                    self.rooms = r.json().get("rooms", self.rooms)
            except httpx.HTTPError as exc:
                log.warning("%s beat error: %s", self.name, exc)

    async def run(self, on_event: EventHandler) -> None:
        params = {
            "connection_id": self.connection_id,
            "scope": "all",
            "filter": "all",
            "client": "bounty-board",
            "client_version": "0.1.0",
        }
        while True:
            headers = {"Accept": "text/event-stream"}
            if self.cursor is not None:
                headers["Last-Event-ID"] = str(self.cursor)
            else:
                params["start_from"] = "head"
            beat: asyncio.Task | None = None
            # Claim every room on the open URL so catch-up covers them and the
            # server knows a live session is answering there.
            try:
                params["rooms"] = ",".join(await self.room_ids())
            except Exception as exc:  # noqa: BLE001
                log.warning("%s list_rooms failed: %s", self.name, exc)
            if not params.get("rooms"):
                params.pop("rooms", None)
            try:
                async with _client(self.base_url, self.api_key, timeout=None) as sc:
                    async with sc.stream(
                        "GET", f"/agents/{self.agent_id}/events", params=params, headers=headers
                    ) as resp:
                        if resp.status_code != 200:
                            log.error("%s stream open %s: %s", self.name, resp.status_code, await resp.aread())
                            await asyncio.sleep(5)
                            continue
                        beat = asyncio.create_task(self._beat())
                        event, data = None, []
                        async for line in resp.aiter_lines():
                            if line.startswith(":"):
                                continue
                            if line.startswith("event:"):
                                event = line[6:].strip()
                            elif line.startswith("data:"):
                                data.append(line[5:].strip())
                            elif line.startswith("id:"):
                                try:
                                    self.cursor = int(line[3:].strip())
                                except ValueError:
                                    pass
                            elif line == "" and data:
                                payload = json.loads("\n".join(data))
                                event, data = event or payload.get("type"), []
                                if event == "connection_state":
                                    self.rooms = payload.get("rooms", [])
                                    log.info("%s connected, rooms=%s", self.name, self.rooms)
                                elif event == "evicted":
                                    log.warning("%s evicted: %s", self.name, payload)
                                    break
                                elif event in ("message", "command", "room_join"):
                                    asyncio.create_task(self._safe(on_event, payload))
                                event = None
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                log.warning("%s stream error: %s", self.name, exc)
            finally:
                if beat:
                    beat.cancel()
            await asyncio.sleep(2)

    async def _safe(self, on_event: EventHandler, payload: dict[str, Any]) -> None:
        try:
            await on_event(self, payload)
        except Exception:  # noqa: BLE001 - one bad event must not kill the agent
            log.exception("%s handler failed", self.name)
