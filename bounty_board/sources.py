"""Contest sources and rules parsing. Free, public endpoints only."""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

IST = ZoneInfo("Asia/Kolkata")
UA = {"User-Agent": "bounty-board/0.1 (+https://github.com/meerpasha59-bit/bounty-board)"}

ZONES = {
    "pacific": "America/Los_Angeles", "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "eastern": "America/New_York", "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "central": "America/Chicago", "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "mountain": "America/Denver", "mt": "America/Denver",
    "gmt": "UTC", "utc": "UTC", "bst": "Europe/London", "uk": "Europe/London",
    "ist": "Asia/Kolkata", "india": "Asia/Kolkata", "cet": "Europe/Berlin", "cest": "Europe/Berlin",
    "sgt": "Asia/Singapore", "jst": "Asia/Tokyo",
}
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"


@dataclass
class Contest:
    title: str
    url: str
    prize: str
    deadline_text: str
    time_left: str
    source: str
    org: str = ""
    themes: list[str] = field(default_factory=list)
    registrations: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _http() -> httpx.Client:
    # trust_env=True here: outbound internet may need the sandbox proxy.
    return httpx.Client(timeout=30, headers=UA, follow_redirects=True)


def text_of(page_html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html, flags=re.S | re.I)
    t = re.sub(r"<(br|p|li|h\d|div|tr)[^>]*>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def scan_devpost(limit: int = 5) -> list[Contest]:
    """Open online Devpost hackathons, soonest deadline first."""
    with _http() as c:
        r = c.get(
            "https://devpost.com/api/hackathons",
            params={"status[]": "open", "order_by": "submission-deadline", "challenge_type[]": "online"},
        )
        r.raise_for_status()
        items = r.json().get("hackathons", [])
    out = []
    for h in items:
        if h.get("invite_only"):
            continue
        prize = re.sub(r"<[^>]+>", "", h.get("prize_amount") or "").strip() or "n/a"
        out.append(
            Contest(
                title=h["title"],
                url=h["url"].rstrip("/") + "/",
                prize=prize,
                deadline_text=h.get("submission_period_dates", ""),
                time_left=h.get("time_left_to_submission", ""),
                source="Devpost",
                org=h.get("organization_name") or "",
                themes=[t["name"] for t in h.get("themes", [])],
                registrations=h.get("registrations_count") or 0,
            )
        )
    # Bigger prizes first among those still open; keep the list short for chat.
    def prize_value(c: Contest) -> int:
        digits = re.sub(r"[^\d]", "", c.prize)
        return int(digits) if digits else 0

    out.sort(key=prize_value, reverse=True)
    return out[:limit]


_DATE_RE = re.compile(
    rf"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?((?:{MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+\d{{4}})"
    r"\s*(?:\(|at\s+|,\s*)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)?\s*"
    r"([A-Za-z]+(?:\s+(?:time|standard time|daylight time))?)?",
    re.I,
)


def _parse_dt(date_s: str, time_s: str | None, zone_s: str | None) -> datetime | None:
    date_s = re.sub(r"\.", "", date_s.replace(",", ""))
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            d = datetime.strptime(" ".join(date_s.split()[:3]).replace("Sept", "Sep"), fmt)
            break
        except ValueError:
            d = None
    if d is None:
        return None
    hh, mm = 23, 59
    if time_s:
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", time_s.strip(), re.I)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2) or 0)
            ap = (m.group(3) or "").lower().replace(".", "")
            if ap == "pm" and hh < 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
    tz = ZoneInfo("UTC")
    if zone_s:
        key = zone_s.lower().split()[0]
        if key in ZONES:
            tz = ZoneInfo(ZONES[key])
    return d.replace(hour=hh, minute=mm, tzinfo=tz)


@dataclass
class RulesReport:
    url: str
    deadline_local: str = ""
    deadline_ist: str = ""
    deadline_iso: str = ""
    hours_left: float | None = None
    india_ok: str = "unclear"
    age: str = ""
    team: str = ""
    flags: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    eligibility_excerpt: str = ""
    raw_text: str = ""


def check_rules(contest_url: str, now: datetime | None = None) -> RulesReport:
    now = now or datetime.now(IST)
    base = contest_url.rstrip("/")
    rules_url = base + "/rules" if "devpost.com" in base else base
    with _http() as c:
        r = c.get(rules_url)
        if r.status_code >= 400 and rules_url != base:
            rules_url = base
            r = c.get(base)
        r.raise_for_status()
        txt = text_of(r.text)
    rep = RulesReport(url=rules_url, raw_text=txt[:20000])
    low = txt.lower()

    # Deadline: the end of the submission period, else the latest date on the page.
    seg = txt
    i = low.find("submission period")
    if i >= 0:
        seg = txt[i : i + 400]
    found = [_parse_dt(m.group(1), m.group(2), m.group(3)) for m in _DATE_RE.finditer(seg)]
    found = [f for f in found if f]
    if i >= 0 and len(found) >= 2:
        dl = found[1]
    elif found:
        dl = max(found)
    else:
        dl = None
    if dl:
        rep.deadline_local = dl.strftime("%a %d %b %Y %H:%M %Z")
        ist = dl.astimezone(IST)
        rep.deadline_ist = ist.strftime("%a %d %b %Y, %I:%M %p IST")
        rep.deadline_iso = ist.isoformat()
        rep.hours_left = round((ist - now).total_seconds() / 3600, 1)

    # Eligibility
    j = low.find("eligib")
    elig = txt[j : j + 2500] if j >= 0 else ""
    rep.eligibility_excerpt = elig[:600]
    el = elig.lower()
    not_open = el.split("is not open to", 1)[1] if "is not open to" in el else ""
    if "india" in not_open[:1500]:
        rep.india_ok = "NO - India is listed as excluded"
    elif "india" in el and "not open" not in el:
        rep.india_ok = "yes - India named as eligible"
    elif elig:
        rep.india_ok = "yes - India not on the excluded list"
    m = re.search(r"(at least|minimum of|over)\s+(the age of majority|\d{2}\s*(years)?)", el)
    if m:
        rep.age = m.group(0)
    if "team" in el:
        m = re.search(r"teams?\s+(?:of|up to)\s+(?:up to\s+)?(\w+)", el)
        rep.team = f"teams allowed ({m.group(0)})" if m else "teams allowed"
    for word, flag in (
        ("entry fee", "mentions an entry fee"),
        ("purchase", "mentions a purchase - check it is 'no purchase necessary'"),
        ("kyc", "KYC needed to receive a prize"),
        ("tax form", "tax form (W-8BEN) needed for prize"),
        ("w-8", "tax form (W-8BEN) needed for prize"),
        ("crypto", "crypto payout possible"),
    ):
        if word in low and flag not in rep.flags:
            rep.flags.append(flag)
    for word, item in (
        ("video", "demo video"),
        ("repository", "code repository"),
        ("github", "code repository"),
        ("readme", "README"),
        ("deployed", "deployed / testable app"),
        ("testing", "testing access for judges"),
        ("slides", "slides / pitch deck"),
    ):
        if word in low and item not in rep.deliverables:
            rep.deliverables.append(item)
    return rep
