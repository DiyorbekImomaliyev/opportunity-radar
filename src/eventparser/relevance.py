"""Deduplication, hard filters and rule scoring (SPEC §6)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache

from .config import Profile
from .models import Item, Kind
from .sources.jobs import RESTRICTED_TAG, VISA_TAG, restricts_foreigners
from .store import StoredItem
from .textutil import local_tz

OPPORTUNITY_WORDS = [
    "event", "hackathon", "workshop", "register", "registration", "apply", "application",
    "internship", "intern", "hiring", "webinar", "summit", "bootcamp", "competition",
    "deadline", "conference", "meetup", "challenge", "scholarship", "fellowship", "program",
    "programme", "join us", "sign up", "open call", "call for",
]


@lru_cache(maxsize=4096)
def _kw_re(kw: str) -> re.Pattern[str]:
    # Short acronyms (ai, vc, llm) must be written in capitals: "AI", not French "j'ai".
    if len(kw) <= 3 and kw.isalpha():
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw.upper())}s?(?![A-Za-z0-9])")
    body = r"\s+".join(re.escape(part) for part in kw.split())
    return re.compile(rf"(?<![a-z0-9]){body}(?:s|es)?(?![a-z0-9])", re.I)


def has_word(text: str, kw: str) -> bool:
    return bool(text) and bool(_kw_re(kw.lower()).search(text))


def any_word(text: str, kws: list[str]) -> str | None:
    return next((k for k in kws if has_word(text, k)), None)


@dataclass
class Assessment:
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    dropped: str | None = None  # hard-filter reason; None means candidate

    @property
    def candidate(self) -> bool:
        return self.dropped is None


def is_home(item: Item, profile: Profile) -> bool:
    """A known location decides; the title is only consulted when the location is unknown.

    ("TechPark at Sarajevo 2026" names a home venue but happens in Sarajevo.)
    """
    text = item.location if item.location else item.title
    return any_word(text, profile.locations) is not None


LANGUAGES = [
    "japanese", "german", "deutsch", "french", "arabic", "chinese", "mandarin", "cantonese", "korean",
    "spanish", "dutch", "italian", "portuguese", "russian", "turkish", "polish", "swedish", "hebrew",
]
_REQUIREMENT = r"(?:business|fluent|native|speaking|speaker|speakers|proficient|proficiency|required|level|n1|n2|c1|c2|b2)"


def required_language(title: str, spoken: list[str]) -> str | None:
    """A language the title explicitly requires ("Business Japanese", "German-speaking") that the
    owner doesn't speak. A language as a topic ("Arabic NLP Engineer") doesn't count."""
    for lang in LANGUAGES:
        if lang in spoken or (lang == "deutsch" and "german" in spoken):
            continue
        near = rf"(?:{_REQUIREMENT}[\s-]+(?:\w+[\s-]+)?{lang}|{lang}[\s-]+(?:\w+[\s-]+)?{_REQUIREMENT})"
        if re.search(rf"\b{near}\b", title, re.I):
            return lang.title()
    return None


def hard_filter(item: Item, profile: Profile, now: datetime) -> str | None:
    if kw := any_word(item.title, profile.exclude):
        return f"excluded keyword: {kw}"

    if item.kind in (Kind.EVENT, Kind.HACKATHON, Kind.PROGRAM):
        end = item.ends_at or item.starts_at
        if end and end < now:
            return "already over"
        if item.starts_at and item.starts_at > now + timedelta(days=profile.lookahead_days):
            return f"starts more than {profile.lookahead_days} days ahead"
        abroad_ok = profile.relocation_ok and "international" in item.tags and item.kind == Kind.PROGRAM
        if item.is_online is not True and item.location and not is_home(item, profile) and not abroad_ok:
            return f"in-person outside home region ({item.location})"
        if item.is_online and item.kind != Kind.HACKATHON and not profile.online_events:
            return "online event (profile: in-person only)"

    if item.kind == Kind.PROGRAM and not item.starts_at and item.published_at:
        if item.published_at < now - timedelta(days=profile.max_program_age_days):
            return f"program page published more than {profile.max_program_age_days} days ago"

    if item.kind in (Kind.POST, Kind.JOB) and item.published_at:
        if item.published_at < now - timedelta(days=profile.max_post_age_days):
            return f"published more than {profile.max_post_age_days} days ago"

    if item.kind == Kind.JOB:
        if kw := any_word(item.title, profile.job_exclude_keywords):
            return f"senior role ({kw})"
        if lang := required_language(item.title, profile.languages):
            return f"needs {lang}"
        if "level:entry" not in item.tags and not any_word(item.title, profile.job_title_keywords):
            return "job title is not entry-level/internship"
        abroad_ok = profile.relocation_ok and "international" in item.tags
        if item.is_online is not True and item.location and not is_home(item, profile) and not abroad_ok:
            return f"job outside home region ({item.location})"
        if profile.needs_visa_sponsorship and (RESTRICTED_TAG in item.tags or restricts_foreigners(item.title)):
            return "no visa sponsorship / nationals only"
    return None


def _matches(title: str, body: str, keywords: dict[str, float], title_factor: float, body_factor: float):
    points, hits = 0.0, []
    for kw, weight in keywords.items():
        if has_word(title, kw):
            points += title_factor * weight
            hits.append(kw)
        elif has_word(body, kw):
            points += body_factor * weight
            hits.append(kw)
    return points, hits


def notable(item: Item, profile: Profile) -> str | None:
    """The first notable organiser (Google, NVIDIA…) behind an item, written as the item writes it."""
    text = " ".join([item.organizer or "", item.title, item.summary, " ".join(item.tags)])
    for phrase in profile.notable_exclude:  # "Google Developer Group at FIU" is a club, not Google
        text = re.sub(rf"(?i)\b{re.escape(phrase)}s?\b", " ", text)
    for kw in profile.notable_organizers:
        if m := _kw_re(kw.lower()).search(text):
            return m.group(0)
    return None


def score(item: Item, profile: Profile) -> Assessment:
    """Rule score 0-10. `reasons` is what a reader sees, most important first."""
    title = item.title
    body = " ".join([item.summary, " ".join(item.tags), item.organizer or ""])
    interests = profile.interests
    if item.kind == Kind.JOB:  # "intern" says what kind of job it is, not what it is about
        interests = {k: w for k, w in interests.items() if not any_word(k, profile.job_title_keywords)}
    topic_pts, topics = _matches(title, body, interests, 2.0, 1.0)

    if item.kind != Kind.POST and not topics:
        return Assessment(0.0, ["no topic match"])

    format_pts, formats = _matches(title, body, profile.formats, 1.0, 0.5)
    total = topic_pts + format_pts
    reasons = [*topics, *formats]
    if org := notable(item, profile):
        total += 2
        reasons.insert(0, f"by {org}")

    if is_home(item, profile):
        total += 3
        reasons.append("local")
    if item.is_online and profile.remote_ok:
        total += 1
        reasons.append("online")
    if profile.needs_visa_sponsorship and VISA_TAG in item.tags:
        total += 2
        reasons.append("visa sponsorship")

    if item.kind == Kind.POST:
        total += 2
        reasons.append("followed page")
        opportunity = any_word(f"{title} {item.summary}", OPPORTUNITY_WORDS)
        if opportunity:
            reasons.append(f"mentions {opportunity}")
            total = max(total, profile.min_score)
        elif not topics:
            return Assessment(0.0, ["followed page, but no opportunity or topic"])

    return Assessment(min(total, 10.0), reasons)


def assess(item: Item, profile: Profile, now: datetime) -> Assessment:
    if reason := hard_filter(item, profile, now):
        return Assessment(0.0, [reason], dropped=reason)
    return score(item, profile)


# ------------------------------------------------------------------ near-duplicates

_STOP = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by", "from",
    "is", "are", "be", "your", "you", "our", "we", "it", "this", "that", "as", "x",
}
_TOKEN = re.compile(r"[a-z0-9]+")
THRESHOLD = 0.85
SAME_TIME_THRESHOLD = 0.6


@lru_cache(maxsize=8192)
def title_tokens(title: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN.findall(title.lower()) if t not in _STOP)


def similarity(a: str, b: str) -> float:
    """Token-set containment: share of the shorter title's tokens found in the longer one."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    if min(len(ta), len(tb)) < 3:  # too short to judge by containment
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _day(item: Item) -> date | None:
    return item.starts_at.astimezone(local_tz()).date() if item.starts_at else None


def is_near_duplicate(a: Item, b: Item) -> bool:
    if a.kind != b.kind:
        return False
    da, db = _day(a), _day(b)
    if (da is None) != (db is None) or (da is not None and da != db):
        return False
    sim = similarity(a.title, b.title)
    # Same start to the minute is strong evidence: "AI Tinkerers London – Session 4" (venue site)
    # and "AI Tinkerers London - Round 4" (organiser site) at 18:00 are one event.
    if a.starts_at and a.starts_at == b.starts_at and sim >= SAME_TIME_THRESHOLD:
        return True
    return sim >= THRESHOLD


def find_duplicates(
    new_uids: set[str], active: list[StoredItem], priority: dict[str, int]
) -> list[tuple[str, str]]:
    """Return (duplicate_uid, keep_uid) pairs involving at least one newly seen item.

    The item from the higher-priority source (earlier in sources.yaml) is kept; an item that
    was already sent is always kept so nothing is delivered twice.
    """
    def rank(s: StoredItem) -> tuple[int, int, str]:
        return (0 if s.sent_at else 1, priority.get(s.item.source, 10_000), s.first_seen.isoformat())

    pairs: list[tuple[str, str]] = []
    dropped: set[str] = set()
    new = [s for s in active if s.uid in new_uids]
    for s in new:
        if s.uid in dropped:
            continue
        for other in active:
            if other.uid == s.uid or other.uid in dropped or other.item.source == s.item.source:
                continue
            if not is_near_duplicate(s.item, other.item):
                continue
            keep, drop = (s, other) if rank(s) < rank(other) else (other, s)
            if drop.sent_at:  # never retract something already delivered
                continue
            pairs.append((drop.uid, keep.uid))
            dropped.add(drop.uid)
            if drop is s:
                break
    return pairs
