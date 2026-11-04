"""Optional language model. Free backends only.

LLM_BACKEND=opencode  -> `opencode run -m $OPENCODE_MODEL` (OpenCode's free models)
LLM_BACKEND=openai    -> any OpenAI-compatible endpoint (LLM_BASE_URL, LLM_API_KEY,
                         LLM_MODEL), e.g. a free Google AI Studio Gemini key
LLM_BACKEND=none      -> deterministic templates only (default)

Every caller has a deterministic fallback, so the room keeps working when the
free tier is rate-limited.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil

import httpx

log = logging.getLogger("bounty_board.llm")


async def complete(prompt: str, timeout: float = 90) -> str | None:
    backend = os.environ.get("LLM_BACKEND", "none").lower()
    try:
        if backend == "opencode":
            return await _opencode(prompt, timeout)
        if backend == "openai":
            return await _openai(prompt, timeout)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM backend %s failed: %s", backend, exc)
    return None


async def _opencode(prompt: str, timeout: float) -> str | None:
    exe = os.environ.get("OPENCODE_BIN") or shutil.which("opencode")
    if not exe:
        return None
    model = os.environ.get("OPENCODE_MODEL", "opencode/big-pickle")
    proc = await asyncio.create_subprocess_exec(
        exe, "run", "-m", model, prompt,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=os.environ.get("OPENCODE_CWD", "/tmp"),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("opencode timed out (free tier busy?)")
        return None
    text = re.sub(r"\x1b\[[0-9;]*m", "", out.decode(errors="ignore"))
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines).strip()
    return text or None


async def _openai(prompt: str, timeout: float) -> str | None:
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"},
            json={
                "model": os.environ.get("LLM_MODEL", "gemini-2.0-flash"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
