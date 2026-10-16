"""Prioritisation and the digest (SPEC §6–7). Delivery lives in telegram.py."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from .config import Profile
from .models import Item, Kind
from .sources.jobs import VISA_TAG
from .relevance import is_home, notable
from .textutil import local_tz, truncate

MAX_MESSAGE = 4000
ABOUT_MAX = 160
TITLE_MAX = 300  # keeps every card far below the message limit, with its link
CLOSING_SOON = timedelta(days=3)


class Section(Enum):
    CLOSING = "🔴 Closing soon"
    HOME = "home"  # title comes from profile.home_label
    HACKATHONS = "🏆 Hackathons & competitions"
    JOBS = "💼 Internships & programs"
    INTERNATIONAL = "🌍 International & remote"
    ONLINE = "🌐 Online events"
    POSTS = "💬 LinkedIn posts"
    EXPLORE = "🎲 Something different"

    def title(self, profile: Profile) -> str:
        return profile.home_label if self is Section.HOME else self.value


@dataclass
class Entry:
    uid: str
    item: Item
    score: float
    about: str | None = None  # set when the AI judged the item
    reasons: list[str] = field(default_factory=list)
    explore: bool = False  # the daily "something different" pick (R-BOT-7)


@dataclass
class SourceStatus:
    name: str
    ok: bool
    error: str | None = None
    consecutive_failures: int = 0
    went_silent: bool = False


@dataclass
class Card:
    """One opportunity = one Telegram message, with mark buttons under it (R-DIG-5)."""
    uid: str
    kind: Kind
    text: str


@dataclass
class Digest:
    title: str
    header: str
    cards: list[Card] = field(default_factory=list)
    footer: str = ""

    @property
    def included(self) -> list[str]:
        """uids in the digest (marked as sent after delivery)."""
        return [c.uid for c in self.cards]

    @property
    def item_count(self) -> int:
        return len(self.cards)

    def parts(self) -> list[tuple[str, Card | None]]:
        """Every message in order — header, one per card, footer (merged into the header when
        there are no cards) — with the card it shows, if any."""
        if not self.cards:
            return [(m, None) for m in _pack([b for b in (self.header, self.footer) if b])]
        return [*((m, None) for m in _pack([self.header])), *((c.text, c) for c in self.cards),
                *((m, None) for m in _pack([self.footer] if self.footer else []))]

    @property
    def messages(self) -> list[str]:
        return [text for text, _ in self.parts()]


# ------------------------------------------------------------------ prioritisation

class Tier(Enum):
    """What is most worth the owner's time comes first."""

    IN_PERSON_HOME = 1  # events, hackathons, programs you can attend and network at
    INTERNSHIP = 2
    POST = 3  # followed LinkedIn pages announcing something to join
    ONLINE = 4  # online hackathons and events: notable organisers first, capped
    INTERNATIONAL = 5  # jobs/programs abroad (sponsorship, relocation) and remote jobs
    OTHER = 6


def is_international(item: Item) -> bool:
    return "international" in item.tags or (item.kind == Kind.JOB and item.is_online is True)


def tier_for(entry: Entry, profile: Profile) -> Tier:
    item = entry.item
    if is_international(item) and item.kind in (Kind.JOB, Kind.PROGRAM):
        return Tier.INTERNATIONAL
    if item.kind == Kind.POST:
        return Tier.POST
    if item.kind == Kind.JOB:
        return Tier.INTERNSHIP
    if item.is_online:
        return Tier.ONLINE
    if is_home(item, profile):
        return Tier.IN_PERSON_HOME
    if item.kind == Kind.PROGRAM:
        return Tier.INTERNSHIP
    return Tier.OTHER


def _rank(entry: Entry) -> tuple[float, float]:
    d = entry.item.deadline
    return (-entry.score, d.timestamp() if d else float("inf"))


QUOTA_NAMES = {
    Tier.IN_PERSON_HOME: "in_person", Tier.INTERNSHIP: "jobs", Tier.POST: "posts",
    Tier.ONLINE: "online", Tier.INTERNATIONAL: "international", Tier.OTHER: "other",
}


def prioritize(entries: list[Entry], profile: Profile) -> tuple[list[Entry], list[Entry]]:
    """(chosen, deferred), at most `max_items`.

    Pass 1 fills each kind's quota (in person, jobs, posts, online) with its best items, so one
    kind can't take the whole digest. Pass 2 gives leftover slots to the best remaining items in
    tier order. Online items never exceed `max_online`; notable organisers lead the online tier."""
    def key(e: Entry):
        tier = tier_for(e, profile)
        # Online, a big organiser is the quality signal; in person, the score decides.
        lead = tier == Tier.ONLINE and notable(e.item, profile) is None
        # AI-checked items come before unchecked ones: rule scores run high (often 10), so the two
        # scales can't be compared directly (seen live when the AI was over quota).
        return (tier.value, lead, e.about is None, *_rank(e))

    ranked = sorted(entries, key=key)
    chosen: list[Entry] = []
    taken: set[int] = set()
    per_kind: dict[str, int] = {}
    online = 0

    def take(i: int, e: Entry, kind: str) -> None:
        nonlocal online
        chosen.append(e)
        taken.add(i)
        per_kind[kind] = per_kind.get(kind, 0) + 1
        online += kind == "online"

    for i, e in enumerate(ranked):  # pass 1: quotas
        kind = QUOTA_NAMES[tier_for(e, profile)]
        if len(chosen) < profile.max_items and per_kind.get(kind, 0) < profile.quotas.get(kind, 0) \
                and not (kind == "online" and online >= profile.max_online):
            take(i, e, kind)
    for i, e in enumerate(ranked):  # pass 2: leftover slots, best tier first
        kind = QUOTA_NAMES[tier_for(e, profile)]
        if i in taken or len(chosen) >= profile.max_items or (kind == "online" and online >= profile.max_online):
            continue
        take(i, e, kind)
    deferred = [e for i, e in enumerate(ranked) if i not in taken]
    return chosen, deferred


def section_for(entry: Entry, profile: Profile, now: datetime) -> Section:
    if entry.explore:
        return Section.EXPLORE
    item = entry.item
    if item.kind not in (Kind.POST, Kind.JOB):
        d = item.deadline
        if d and now <= d <= now + CLOSING_SOON:
            return Section.CLOSING
    if item.kind == Kind.POST:
        return Section.POSTS
    if item.kind in (Kind.JOB, Kind.PROGRAM) and is_international(item):
        return Section.INTERNATIONAL
    if item.kind in (Kind.JOB, Kind.PROGRAM):
        return Section.JOBS
    if item.kind == Kind.HACKATHON:
        return Section.HACKATHONS
    if is_home(item, profile) and item.is_online is not True:
        return Section.HOME
    return Section.ONLINE


# ------------------------------------------------------------------ formatting

def fmt_day(dt: datetime, with_time: bool = True) -> str:
    local = dt.astimezone(local_tz())
    s = f"{local:%a} {local.day} {local:%b}"
    if with_time and (local.hour, local.minute) != (0, 0):
        s += f", {local:%H:%M}"
    return s


def _ago(dt: datetime, now: datetime) -> str:
    delta = now - dt
    if delta < timedelta(hours=1):
        return "just now"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def when_text(item: Item, now: datetime) -> str | None:
    if item.kind == Kind.HACKATHON and item.ends_at:
        return f"⏳ Deadline {fmt_day(item.ends_at, with_time=False)}"
    if item.kind in (Kind.JOB, Kind.POST):
        return f"🕒 Posted {_ago(item.published_at, now)}" if item.published_at else None
    if item.starts_at:
        s = f"📅 {fmt_day(item.starts_at)}"
        if item.ends_at:
            tz = local_tz()
            if item.ends_at.astimezone(tz).date() > item.starts_at.astimezone(tz).date():
                s += f" → {fmt_day(item.ends_at, with_time=False)}"
        return s
    return None


def where_text(item: Item) -> str | None:
    if item.kind == Kind.POST:
        return None
    if item.is_online:
        return "🌐 Online"
    return f"📍 {item.location}" if item.location else None


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def render_entry(entry: Entry, now: datetime) -> str:
    item = entry.item
    title = truncate(item.title, TITLE_MAX)
    lines = [f'<b><a href="{html.escape(item.url, quote=True)}">{_esc(title)}</a></b>']
    visa = "🛂 Visa sponsorship" if VISA_TAG in item.tags else None
    meta = " · ".join(x for x in (when_text(item, now), where_text(item), visa) if x)
    if meta:
        lines.append(_esc(meta))
    about = truncate(entry.about or truncate(item.summary, ABOUT_MAX), ABOUT_MAX)
    if about:
        lines.append(_esc(about))
    if entry.about is not None:
        why = f"AI rated {entry.score:.0f}/10"
    else:
        why = " · ".join([r for r in entry.reasons if not r.startswith("ai:")][:4])
    lines.append(f"<i>{_esc(item.source + (' · ' + why if why else ''))}</i>")
    return "\n".join(lines)


def render_health(statuses: list[SourceStatus]) -> str:
    if not statuses:
        return ""
    ok = sum(1 for s in statuses if s.ok)
    lines = [f"<i>⚙️ Sources: {ok}/{len(statuses)} OK</i>"]
    for s in statuses:
        if not s.ok:
            flag = f" ⚠️ failing {s.consecutive_failures} runs in a row" if s.consecutive_failures >= 3 else ""
            lines.append(_esc(f"• {s.name}: {truncate(s.error or 'failed', 160)}{flag}"))
        elif s.went_silent:
            lines.append(_esc(f"• {s.name}: returned 0 items (site layout may have changed)"))
    return "\n".join(lines)


def _pack(blocks: list[str], limit: int = MAX_MESSAGE) -> list[str]:
    """Join blocks into messages ≤ limit, splitting only between blocks."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:  # never cut through a tag: fall back to escaped plain text
            plain = html.unescape(re.sub(r"<[^>]+>", "", block))
            block = _esc(truncate(plain, limit - 50))
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = block
    if current:
        messages.append(current)
    return messages


def card_text(entry: Entry, profile: Profile, now: datetime) -> str:
    """A card: its section as a small label, then the entry; never longer than one message."""
    text = f"<i>{_esc(section_for(entry, profile, now).title(profile))}</i>\n{render_entry(entry, now)}"
    return _pack([text])[0]


def build_digest(
    entries: list[Entry], profile: Profile, now: datetime, statuses: list[SourceStatus],
    notes: list[str] | None = None,
) -> Digest:
    buckets: dict[Section, list[Entry]] = {s: [] for s in Section}
    for entry in entries:
        buckets[section_for(entry, profile, now)].append(entry)

    local = now.astimezone(local_tz())
    title = f"🎯 Opportunity Radar — {local:%a} {local.day} {local:%b %Y}"
    count = len(entries)
    if count:
        counts = " · ".join(f"{s.title(profile)} {len(buckets[s])}" for s in Section if buckets[s])
        subtitle = (f"{count} new {'opportunity' if count == 1 else 'opportunities'}\n{_esc(counts)}\n"
                    "<i>Under each: ✅ registered/applied · ⭐ remind me · ❌ not for me · 🚫 can't apply</i>")
    else:
        subtitle = "No new opportunities today."

    cards = [Card(e.uid, e.item.kind, card_text(e, profile, now))
             for section in Section for e in sorted(buckets[section], key=_rank)]
    footer = "\n".join([*(f"<i>{_esc(n)}</i>" for n in notes or []), render_health(statuses)]).strip()
    return Digest(title=title, header=f"<b>{_esc(title)}</b>\n{subtitle}", cards=cards, footer=footer)


# ------------------------------------------------------------------ preview

def write_preview(digest: Digest, path: Path) -> None:
    """The digest as a local HTML page (dry runs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n<hr>\n".join(f"<div style='white-space:pre-wrap'>{m}</div>" for m in digest.messages)
    path.write_text(
        f"<!doctype html><meta charset='utf-8'><title>{html.escape(digest.title)}</title>"
        "<body style='font:15px/1.45 -apple-system,sans-serif;max-width:640px;margin:24px auto;padding:0 16px'>"
        f"{body}</body>"
    )
