"""The three Bounty Board agents and their room behaviour.

Every hand-off between agents goes through the Switch room: Scout's list is
read back by Rules Checker from the room history (read_context), and Builder
acts only on an explicit approval typed by a person in the chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import llm
from .sources import IST, check_rules, scan_devpost
from .switch_client import SwitchAgent

log = logging.getLogger("bounty_board.agents")

DATA = Path(os.environ.get("BOUNTY_DATA", Path(__file__).resolve().parent.parent / "data"))
STATE = DATA / "state.json"
PLANS = DATA / "plans"

SCOUT, RULES, BUILDER = "bb-scout", "bb-rules", "bb-builder"
LIST_LINE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s+[—-]\s+(https?://\S+)", re.M)


# ── shared state (survives restarts; the room is the source of truth) ──────
def load() -> dict[str, Any]:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"rooms": {}, "reminders": [], "last_daily": ""}


def save(st: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2, default=str))


def room_state(st: dict[str, Any], room_id: str) -> dict[str, Any]:
    return st["rooms"].setdefault(room_id, {"contests": [], "rules": {}, "drafts": {}, "approved": {}})


def clean(body: str) -> str:
    return re.sub(r"@[\w.-]+", "", body or "").strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


class Board:
    def __init__(self, agents: dict[str, SwitchAgent]):
        self.agents = agents
        self.agent_names = {a.name for a in agents.values()}
        self.lock = asyncio.Lock()

    # Who is a person? Switch marks platform posts with sender_kind; agents in
    # this room are told apart by name (the three of us) and by their agent ids.
    async def is_person(self, agent: SwitchAgent, room_id: str, payload: dict[str, Any]) -> bool:
        if payload.get("sender_kind") == "platform":
            return False
        name = (payload.get("sender_name") or "").lower()
        if name in self.agent_names or name in {"scout", "rules checker", "builder"}:
            return False
        sender = payload.get("sender") or ""
        if any(a.agent_id in sender for a in self.agents.values()):
            return False
        return "switch-agent" not in sender

    # ── dispatch ───────────────────────────────────────────────────────
    async def on_event(self, agent: SwitchAgent, ev: dict[str, Any]) -> None:
        if ev.get("type") != "message":
            if ev.get("type") == "room_join" and agent.name == SCOUT and ev["payload"].get("listening"):
                await agent.say(ev["room_id"], self.help_text())
            return
        p = ev["payload"]
        if not p.get("addressed"):
            return
        room = ev["room_id"]
        text = clean(p.get("body", ""))
        if not await self.is_person(agent, room, p):
            return
        log.info("%s <- %s: %s", agent.name, p.get("sender_name"), text)
        if agent.name == SCOUT:
            await self.scout(agent, room, text)
        elif agent.name == RULES:
            await self.rules(agent, room, text)
        elif agent.name == BUILDER:
            await self.builder(agent, room, text, p.get("sender_name") or "you")

    def help_text(self) -> str:
        return (
            "🗂 Bounty Board is open. Three of us work here, and nothing moves without your OK:\n"
            f"• @{SCOUT} scan — today's best open contests (I also post one list every morning, 09:00 IST)\n"
            f"• @{RULES} check 2 — eligibility for India, deliverables and the deadline in IST\n"
            f"• @{BUILDER} approve 2 — draft a build plan; then '@{BUILDER} ok 2' to lock it and set reminders"
        )

    # ── Scout ──────────────────────────────────────────────────────────
    async def scout(self, agent: SwitchAgent, room: str, text: str) -> None:
        if text.lower().startswith("help"):
            await agent.say(room, self.help_text())
            return
        await self.post_scan(agent, room)

    async def post_scan(self, agent: SwitchAgent, room: str) -> None:
        try:
            contests = await asyncio.to_thread(scan_devpost, 5)
        except Exception as exc:  # noqa: BLE001
            await agent.say(room, f"Couldn't reach Devpost just now ({exc.__class__.__name__}). I'll retry on the next scan.")
            return
        async with self.lock:
            st = load()
            room_state(st, room)["contests"] = [c.to_dict() for c in contests]
            save(st)
        today = datetime.now(IST).strftime("%a %d %b")
        lines = [f"🔭 Open contests, {today} (biggest prize first):"]
        for i, c in enumerate(contests, 1):
            lines.append(f"{i}. {c.title} — {c.url}\n   💰 {c.prize} · ⏳ {c.time_left} · {c.deadline_text}")
        lines.append(f"Next: '@{RULES} check N' and I'll hand it to Rules Checker.")
        await agent.say(room, "\n".join(lines))

    async def daily_loop(self) -> None:
        agent = self.agents[SCOUT]
        hour = int(os.environ.get("SCOUT_HOUR_IST", "9"))
        while True:
            now = datetime.now(IST)
            st = load()
            if now.hour >= hour and st.get("last_daily") != now.date().isoformat() and agent.rooms:
                st["last_daily"] = now.date().isoformat()
                save(st)
                for room in agent.rooms:
                    await self.post_scan(agent, room)
            await asyncio.sleep(60)

    # ── Rules Checker ──────────────────────────────────────────────────
    async def find_contest(self, agent: SwitchAgent, room: str, n: int) -> dict[str, Any] | None:
        """Read Scout's latest list back out of the Switch room history."""
        try:
            for entry in reversed(await agent.history(room, 80)):
                if "Open contests" in (entry.get("body") or ""):
                    for m in LIST_LINE.finditer(entry["body"]):
                        if int(m.group(1)) == n:
                            return {"title": m.group(2).strip(), "url": m.group(3)}
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("read_context failed: %s", exc)
        cs = room_state(load(), room)["contests"]
        return cs[n - 1] if 0 < n <= len(cs) else None

    async def rules(self, agent: SwitchAgent, room: str, text: str) -> None:
        url_m = re.search(r"https?://\S+", text)
        num_m = re.search(r"\b(\d{1,2})\b", text)
        if url_m:
            contest, n = {"title": url_m.group(0), "url": url_m.group(0)}, 0
        elif num_m:
            n = int(num_m.group(1))
            contest = await self.find_contest(agent, room, n)
            if not contest:
                await agent.say(room, f"I don't see #{n} on Scout's latest list. Ask @{SCOUT} to scan first.")
                return
        else:
            await agent.say(room, f"Tell me which one: '@{RULES} check 2' or paste a contest link.")
            return
        await agent.say(room, f"📜 Reading the official rules for {contest['title']}…")
        try:
            rep = await asyncio.to_thread(check_rules, contest["url"])
        except Exception as exc:  # noqa: BLE001
            await agent.say(room, f"Couldn't open the rules page ({exc.__class__.__name__}). Link: {contest['url']}")
            return
        summary = await llm.complete(
            "You check hackathon rules for an Indian solo developer. In 3 short bullet points, say: "
            "can a resident of India enter and receive a prize, anything that costs money or needs KYC, "
            "and the exact deliverables. Only use this rules text, and say 'not stated' when unsure.\n\n"
            + rep.raw_text[:8000],
            timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
        )
        left = ""
        if rep.hours_left is not None:
            left = f" ({rep.hours_left / 24:.1f} days left)" if rep.hours_left > 0 else " (CLOSED)"
        lines = [
            f"✅ Rules check — {contest['title']}",
            f"⏰ Deadline: {rep.deadline_ist or 'not found on the rules page'}{left}",
            f"   (official: {rep.deadline_local})" if rep.deadline_local else "",
            f"🇮🇳 India: {rep.india_ok}",
            f"👤 Age: {rep.age}" if rep.age else "",
            f"👥 {rep.team}" if rep.team else "",
            f"📦 Deliverables: {', '.join(rep.deliverables) or 'not stated'}",
            f"⚠️ Watch: {'; '.join(rep.flags)}" if rep.flags else "⚠️ Watch: nothing flagged",
        ]
        if summary:
            lines.append("🧠 Summary (OpenCode):\n" + summary[:900])
        lines.append(f"Source: {rep.url}")
        verdict_ok = not rep.india_ok.startswith("NO") and (rep.hours_left or 1) > 0
        if verdict_ok and n:
            lines.append(f"👉 Your call: '@{BUILDER} approve {n}' to get a build plan, or skip it.")
        elif not verdict_ok:
            lines.append("🛑 I'd skip this one.")
        async with self.lock:
            st = load()
            rs = room_state(st, room)
            rs["rules"][str(n or contest["url"])] = {
                "title": contest["title"], "url": contest["url"], "deadline_iso": rep.deadline_iso,
                "deadline_ist": rep.deadline_ist, "deliverables": rep.deliverables, "india_ok": rep.india_ok,
            }
            save(st)
        await agent.say(room, "\n".join(l for l in lines if l))

    # ── Builder ────────────────────────────────────────────────────────
    async def builder(self, agent: SwitchAgent, room: str, text: str, who: str) -> None:
        low = text.lower()
        num_m = re.search(r"\b(\d{1,2})\b", low)
        n = num_m.group(1) if num_m else None
        if low.startswith(("reminders", "status")):
            await self.list_reminders(agent, room)
            return
        if not n:
            await agent.say(room, f"Which contest? e.g. '@{BUILDER} approve 2'.")
            return
        st = load()
        rs = room_state(st, room)
        info = rs["rules"].get(n)
        if low.startswith("approve"):
            if not info:
                await agent.say(room, f"Rules Checker hasn't vetted #{n} yet. '@{RULES} check {n}' first — I only build what's been checked.")
                return
            draft = await self.draft_plan(info, feedback="")
            rs["drafts"][n] = draft
            save(st)
            await agent.say(room, f"🛠 Draft plan for {info['title']} (approved by {who}):\n{draft}\n\n"
                                  f"Reply '@{BUILDER} ok {n}' to lock it in and set reminders, or "
                                  f"'@{BUILDER} change {n}: …' to redo it.")
        elif low.startswith("change"):
            if n not in rs["drafts"]:
                await agent.say(room, f"No draft for #{n} yet. '@{BUILDER} approve {n}' first.")
                return
            fb = text.split(":", 1)[1].strip() if ":" in text else text
            draft = await self.draft_plan(info, feedback=fb, previous=rs["drafts"][n])
            rs["drafts"][n] = draft
            save(st)
            await agent.say(room, f"🛠 Revised plan for {info['title']}:\n{draft}\n\nReply '@{BUILDER} ok {n}' to lock it in.")
        elif low.startswith(("ok", "yes", "lock", "go")):
            if n not in rs["drafts"]:
                await agent.say(room, f"Nothing to lock for #{n}. '@{BUILDER} approve {n}' first.")
                return
            path = PLANS / slug(info["title"]) / "PLAN.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {info['title']}\n\nRules: {info['url']}\nDeadline: {info['deadline_ist']}\n"
                            f"Approved in chat by {who} on {datetime.now(IST):%d %b %Y %H:%M} IST\n\n{rs['drafts'][n]}\n")
            rs["approved"][n] = str(path)
            added = []
            if info.get("deadline_iso"):
                dl = datetime.fromisoformat(info["deadline_iso"])
                for hrs in (48, 6):
                    when = dl - timedelta(hours=hrs)
                    if when > datetime.now(IST):
                        st["reminders"].append({"room": room, "when": when.isoformat(), "title": info["title"],
                                                "hours": hrs, "url": info["url"], "sent": False})
                        added.append(when.strftime("%a %d %b %I:%M %p IST"))
            save(st)
            rem = ("Reminders set: " + ", ".join(added)) if added else "No reminders (deadline unknown or too close)."
            await agent.say(room, f"🔒 Locked. Plan saved as {path.parent.name}/PLAN.md. {rem}")
        else:
            await agent.say(room, f"I understand 'approve {n}', 'change {n}: …', 'ok {n}' and 'reminders'.")

    async def draft_plan(self, info: dict[str, Any], feedback: str, previous: str = "") -> str:
        prompt = (
            "Draft a 6-step solo build plan for this hackathon, one line per step, each with a target day "
            "relative to the deadline. End with a 'Submit by' line 4 hours before the deadline (IST).\n"
            f"Contest: {info['title']}\nRules: {info['url']}\nDeadline: {info['deadline_ist']}\n"
            f"Deliverables: {', '.join(info.get('deliverables') or []) or 'not stated'}\n"
        )
        if previous:
            prompt += f"\nPrevious draft:\n{previous}\nChange requested: {feedback}\n"
        out = await llm.complete(prompt, timeout=float(os.environ.get("LLM_TIMEOUT", "60")))
        if out:
            return out[:1500]
        return template_plan(info, feedback)

    async def list_reminders(self, agent: SwitchAgent, room: str) -> None:
        rem = [r for r in load()["reminders"] if r["room"] == room and not r["sent"]]
        if not rem:
            await agent.say(room, "No reminders pending.")
            return
        await agent.say(room, "⏰ Pending reminders:\n" + "\n".join(
            f"• {datetime.fromisoformat(r['when']):%a %d %b %I:%M %p} IST — {r['title']} ({r['hours']} h left)" for r in rem))

    async def reminder_loop(self) -> None:
        agent = self.agents[BUILDER]
        while True:
            async with self.lock:
                st = load()
                due = [r for r in st["reminders"] if not r["sent"] and datetime.fromisoformat(r["when"]) <= datetime.now(IST)]
                for r in due:
                    r["sent"] = True
                save(st)
            for r in due:
                await agent.say(r["room"], f"⏰ {r['hours']} hours left for {r['title']}. Rules: {r['url']}")
            await asyncio.sleep(30)


def template_plan(info: dict[str, Any], feedback: str = "") -> str:
    """Deterministic plan used when no language model is configured.

    Steps get real target dates between today and a 4-hour buffer before the
    deadline. Feedback that mentions weekends (or weekdays/evenings) moves every
    step onto those days.
    """
    steps = ["Read the full rules and FAQ; register", "Pick the idea and write a one-page spec",
             "Build the core flow end to end", "README with setup + screenshots; licence"]
    if "demo video" in (info.get("deliverables") or []):
        steps.append("Record the demo video (under 3 min) and upload it")
    steps.append("Submit, then post about it")
    fb = (feedback or "").lower()
    if not info.get("deadline_iso"):
        lines = [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    else:
        submit_by = datetime.fromisoformat(info["deadline_iso"]) - timedelta(hours=4)
        today = datetime.now(IST).date()
        days = [today + timedelta(d) for d in range((submit_by.date() - today).days + 1)]
        if "weekend" in fb:
            days = [d for d in days if d.weekday() >= 5] or days
        elif "weekday" in fb:
            days = [d for d in days if d.weekday() < 5] or days
        if len(days) >= len(steps):
            picks = [days[round(i * (len(days) - 1) / (len(steps) - 1))] for i in range(len(steps))]
        else:
            picks = [days[min(i, len(days) - 1)] for i in range(len(steps))]
        lines = [f"{i}. {d:%a %d %b} — {s}" for i, (s, d) in enumerate(zip(steps, picks), 1)]
        lines.append(f"Submit by: {submit_by:%a %d %b %I:%M %p} IST (4 h buffer)")
        if "weekend" in fb:
            lines.append("(Every step moved to a Saturday or Sunday, as you asked.)")
    if feedback and "weekend" not in fb and "weekday" not in fb:
        lines.append(f"Note from you: {feedback}")
    return "\n".join(lines)
