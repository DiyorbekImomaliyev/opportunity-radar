"""Event-site adapters: JSON-LD pages, Bevy, Devpost, RSS, The Events Calendar, WordPress (SPEC §3)."""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup

from ..http import Fetcher
from ..models import Item, Kind, canonical_url, uid_for
from ..textutil import (
    clean_text,
    html_to_text,
    parse_date_text,
    parse_iso,
    resolve_location,
    span_to_datetimes,
    truncate,
)
from .base import Source, SourceParams, register, summarize_errors

_LD_BLOCK = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)


class JsonLdParams(SourceParams):
    urls: list[str]
    kind: Kind = Kind.EVENT
    default_location: str | None = None
    timezone: str | None = None  # default: profile timezone


def _types(obj: dict) -> list[str]:
    t = obj.get("@type")
    return [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def iter_typed(obj: Any, suffixes: tuple[str, ...]) -> Iterator[dict]:
    """Depth-first walk yielding dicts whose @type ends with one of `suffixes`."""
    if isinstance(obj, list):
        for x in obj:
            yield from iter_typed(x, suffixes)
    elif isinstance(obj, dict):
        if any(t.endswith(suffixes) for t in _types(obj)):
            yield obj
            return
        for v in obj.values():
            if isinstance(v, (list, dict)):
                yield from iter_typed(v, suffixes)


def ld_blocks(html: str) -> Iterator[Any]:
    for raw in _LD_BLOCK.findall(html):
        try:
            yield json.loads(raw.strip())
        except json.JSONDecodeError:
            # Some sites emit raw control characters inside strings.
            try:
                yield json.loads(raw.strip(), strict=False)
            except json.JSONDecodeError:
                continue


def _name(v: Any) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return v.get("name")
    return v if isinstance(v, str) else None


def _location(loc: Any) -> tuple[str | None, bool | None]:
    if isinstance(loc, list):
        parts = [_location(x) for x in loc]
        text = " / ".join(p[0] for p in parts if p[0])
        online = any(p[1] for p in parts) or None
        return text or None, online
    if isinstance(loc, str):
        return loc, None
    if not isinstance(loc, dict):
        return None, None
    if any(t == "VirtualLocation" for t in _types(loc)):
        return "Online", True
    addr = loc.get("address")
    if isinstance(addr, dict):
        addr = ", ".join(
            str(addr[k]) for k in ("streetAddress", "addressLocality", "addressRegion", "addressCountry")
            if isinstance(addr.get(k), str) and addr.get(k)
        )
    bits = [b for b in (loc.get("name"), addr if isinstance(addr, str) else None) if b]
    return dedupe_parts(", ".join(bits)) or None, None


def dedupe_parts(text: str) -> str:
    """'London, London, London, Greater London, UK' -> 'London, Greater London, UK'."""
    seen: set[str] = set()
    parts = []
    for part in (p.strip() for p in text.split(",")):
        if part and part.lower() not in seen:
            seen.add(part.lower())
            parts.append(part)
    return ", ".join(parts)


_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:?00)?$")


def ld_date(value: Any, tz: ZoneInfo):
    """schema.org dates; a bare midnight timestamp means "that day" in the event's timezone."""
    if not isinstance(value, str):
        return None
    if m := _MIDNIGHT.match(value.strip()):
        return parse_iso(m.group(1), tz)
    return parse_iso(value, tz)


def _end_of_day_if_date_only(value: Any, tz: ZoneInfo):
    dt = ld_date(value, tz)
    if dt is not None and isinstance(value, str) and (len(value.strip()) == 10 or _MIDNIGHT.match(value.strip())):
        return dt + timedelta(hours=23, minutes=59)
    return dt


def event_to_fields(
    ev: dict, *, tz: ZoneInfo, default_kind: Kind, default_location: str | None, page_url: str = ""
) -> dict:
    location, online = _location(ev.get("location"))
    mode = str(ev.get("eventAttendanceMode") or "")
    if "Online" in mode or "Mixed" in mode:
        online = True
    elif "Offline" in mode:
        online = False if online is None else online
    title = clean_text(ev.get("name"))
    kind = Kind.HACKATHON if "hackathon" in title.lower() else default_kind
    return {
        "kind": kind,
        "title": title,
        # Relative URLs ("/event/…") are valid schema.org: resolve them against the page.
        "url": urljoin(page_url, ev.get("url") or "") if ev.get("url")
        else (ev.get("@id") if str(ev.get("@id", "")).startswith("http") else ""),
        "summary": ev.get("description") or "",
        "starts_at": ld_date(ev.get("startDate"), tz),
        "ends_at": _end_of_day_if_date_only(ev.get("endDate"), tz),
        "location": location or (None if online else default_location),
        "is_online": online,
        "organizer": _name(ev.get("organizer")),
    }


@register
class JsonLdSource(Source):
    type = "jsonld"
    Params = JsonLdParams
    p: JsonLdParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        tz = self.tz
        items: list[Item | None] = []
        errors: list[tuple[str, str]] = []
        for url in self.p.urls:
            try:
                html = await fx.get_text(url)
            except Exception as e:
                errors.append((url, str(e)))
                continue
            for block in ld_blocks(html):
                for ev in iter_typed(block, ("Event",)):
                    items.append(
                        self.make(**event_to_fields(
                            ev, tz=tz, default_kind=self.p.kind, default_location=self.p.default_location,
                            page_url=url,
                        ))
                    )
        if errors and len(errors) == len(self.p.urls):
            raise RuntimeError(summarize_errors(errors, "URL"))
        if errors:
            self.warnings.append(summarize_errors(errors, "URL"))
        return self.collect(items)


# ------------------------------------------------------------------ bevy

class BevyParams(SourceParams):
    chapter_id: int
    base_url: str = "https://gdg.community.dev"
    default_location: str | None = None
    page_size: int = 50
    organizer: str | None = None  # override, e.g. Startup Grind chapters are named after the city only


@register
class BevySource(Source):
    type = "bevy"
    Params = BevyParams
    p: BevyParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        url = (
            f"{self.p.base_url.rstrip('/')}/api/event_slim/for_chapter/{self.p.chapter_id}/"
            f"?status=Live&include_cohosted_events=true&page_size={self.p.page_size}"
        )
        data = await fx.get_json(url)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise ValueError("unexpected Bevy response (no 'results' list)")
        return self.collect([self._item(r) for r in results if isinstance(r, dict)])

    def _item(self, r: dict) -> Item | None:
        audience = (r.get("audience_type") or "").upper()
        online = True if audience == "VIRTUAL" or r.get("is_virtual_event") else (
            False if audience == "IN_PERSON" else None
        )
        title = r.get("title") or ""
        tags = [t for t in (r.get("event_type_title"),) if t]
        return self.make(
            kind=Kind.HACKATHON if "hackathon" in title.lower() else Kind.EVENT,
            title=title,
            url=r.get("static_url") or r.get("url") or "",
            summary=r.get("description_short") or r.get("description") or "",
            starts_at=parse_iso(r.get("start_date")),
            ends_at=parse_iso(r.get("end_date")),
            location="Online" if online else self.p.default_location,
            is_online=online,
            organizer=self.p.organizer or r.get("chapter_title"),
            tags=tags,
        )


# ------------------------------------------------------------------ devpost

API = "https://devpost.com/api/hackathons?status[]=upcoming&status[]=open&page={page}"


class DevpostParams(SourceParams):
    max_pages: int = 25
    themes: list[str] = []  # keep only hackathons with any of these themes (case-insensitive)
    min_registrations: int = 0  # skip online hackathons with fewer sign-ups (a quality signal)


@register
class DevpostSource(Source):
    type = "devpost"
    Params = DevpostParams
    p: DevpostParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        items: list[Item | None] = []
        wanted = {t.lower() for t in self.p.themes}
        self.below_threshold = 0
        for page in range(1, self.p.max_pages + 1):
            data = await fx.get_json(API.format(page=page))
            hacks = data.get("hackathons") if isinstance(data, dict) else None
            if not hacks:
                break
            for h in hacks:
                if not isinstance(h, dict):
                    continue
                themes = [t.get("name", "") for t in h.get("themes") or [] if isinstance(t, dict)]
                if wanted and not wanted & {t.lower() for t in themes}:
                    continue
                online = (h.get("displayed_location") or {}).get("icon") == "globe"
                if online and int(h.get("registrations_count") or 0) < self.p.min_registrations:
                    self.below_threshold += 1
                    continue
                items.append(self._item(h, themes))
        return self.collect(items)

    def _item(self, h: dict, themes: list[str]) -> Item | None:
        loc = (h.get("displayed_location") or {}).get("location") or None
        online = (loc or "").strip().lower() == "online" or (h.get("displayed_location") or {}).get("icon") == "globe"
        prize = html_to_text(h.get("prize_amount"), sep="")
        starts = ends = None
        if span := parse_date_text(h.get("submission_period_dates")):
            starts, ends = span_to_datetimes(span, ZoneInfo("UTC"))
        bits = [
            h.get("organization_name"),
            f"Prizes {prize}" if prize and prize not in ("$0", "0") else None,
            f"{h['registrations_count']:,} registered" if isinstance(h.get("registrations_count"), int) else None,
            h.get("time_left_to_submission"),
            ", ".join(themes) if themes else None,
        ]
        return self.make(
            kind=Kind.HACKATHON,
            title=h.get("title") or "",
            url=h.get("url") or "",
            summary=" · ".join(b for b in bits if b),
            starts_at=starts,
            ends_at=ends,
            location=loc,
            is_online=online,
            organizer=h.get("organization_name"),
            tags=themes,
        )


# ------------------------------------------------------------------ rss

class RssParams(SourceParams):
    url: str
    kind: Kind = Kind.POST
    organizer: str | None = None
    # Some event feeds put the event's date (not the publish date) in pubDate.
    date_is_event_start: bool = False
    default_location: str | None = None
    # Programs: open each article and put its eligibility section first, so the AI can tell who
    # may apply (feed teasers often omit it, e.g. "RISE: students from different countries").
    fetch_eligibility: bool = False


_ELIGIBILITY_HEADING = re.compile(r"eligib|who can apply|requirements", re.I)
_REQUIREMENT_WORDS = re.compile(
    r"\b(?:must|should|required?|eligible|open to|applicants?|citizens?|nationals?|enrolled|only|at least)\b", re.I)


def eligibility_section(html: str, limit: int = 600) -> str:
    """Text under an 'Eligibility' / 'Who can apply' heading of an article ('' if none).

    Pages often repeat the heading (table of contents, then the section), so every match is read
    and the fullest one wins."""
    soup = BeautifulSoup(html, "html.parser")
    best = ""
    for h in soup.find_all(["h2", "h3", "h4", "strong", "b"]):
        if not (_ELIGIBILITY_HEADING.search(h.get_text()) and len(h.get_text(strip=True)) < 60):
            continue
        parts: list[str] = []
        for el in h.find_all_next(["p", "li", "h2", "h3"], limit=16):
            if el.name in ("h2", "h3") and not _ELIGIBILITY_HEADING.search(el.get_text()):
                break  # next section
            if el.name in ("p", "li") and (text := el.get_text(" ", strip=True)):
                parts.append(text)
            if sum(map(len, parts)) > limit:
                break
        rules = [t for t in parts if _REQUIREMENT_WORDS.search(t)] or parts  # drop the sales pitch
        if len(" ".join(rules)) > len(best):
            best = " ".join(rules)
    return truncate(best, limit)


def _entry_date(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        if st := entry.get(key):
            return datetime.fromtimestamp(calendar.timegm(st), UTC)
    return parse_iso(entry.get("published") or entry.get("updated"))


@register
class RssSource(Source):
    type = "rss"
    Params = RssParams
    p: RssParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        feed = feedparser.parse(await fx.get_text(self.p.url))
        if feed.bozo and not feed.entries:
            raise ValueError(f"not a valid feed: {feed.bozo_exception}")
        organizer = self.p.organizer or feed.feed.get("title")
        items = []
        for e in feed.entries:
            when = _entry_date(e)
            summary = html_to_text(e.get("summary") or e.get("description") or "")
            # Read each article once: the store keeps the eligibility text on later refreshes.
            if self.p.fetch_eligibility and e.get("link") and not self.known(uid_for(e["link"])):
                try:
                    if rules := eligibility_section(await fx.get_text(e["link"])):
                        summary = f"Eligibility: {rules} — {summary}"
                except Exception as err:  # best effort: the teaser is still useful
                    self.warnings.append(f"eligibility not read for {e['link']}: {err}")
            items.append(self.make(
                kind=self.p.kind,
                title=e.get("title") or "",
                url=e.get("link") or "",
                summary=summary,
                starts_at=when if self.p.date_is_event_start else None,
                published_at=None if self.p.date_is_event_start else when,
                location=self.p.default_location,
                organizer=organizer,
                tags=[t.get("term") for t in e.get("tags") or [] if t.get("term")],
            ))
        return self.collect(items)


# ------------------------------------------------------------------ the events calendar (WordPress plugin)

class TribeParams(SourceParams):
    base_url: str
    max_pages: int = 3
    per_page: int = 50
    kind: Kind = Kind.EVENT
    default_location: str | None = None
    timezone: str | None = None  # default: profile timezone


def _venue(v) -> str | None:
    if not isinstance(v, dict):
        return None
    parts = [v.get("venue"), v.get("address"), v.get("city")]
    seen: list[str] = []
    for p in (html_to_text(x) for x in parts if isinstance(x, str)):
        if p and p.lower() not in (s.lower() for s in seen):
            seen.append(p)
    return ", ".join(seen) or None


@register
class TribeEventsSource(Source):
    type = "tribe_events"
    Params = TribeParams
    p: TribeParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        base = self.p.base_url.rstrip("/")
        items: list[Item | None] = []
        for page in range(1, self.p.max_pages + 1):
            data = await fx.get_json(
                f"{base}/wp-json/tribe/events/v1/events?per_page={self.p.per_page}&page={page}"
            )
            events = data.get("events") if isinstance(data, dict) else None
            if not isinstance(events, list):
                raise ValueError("unexpected Events Calendar response (no 'events' list)")
            items.extend(self._item(e) for e in events if isinstance(e, dict))
            if page >= int(data.get("total_pages") or 1):
                break
        return self.collect(items)

    def _item(self, e: dict) -> Item | None:
        tz = self.tz
        if e.get("all_day"):
            start = parse_iso((e.get("start_date") or "")[:10], tz)
            end_day = parse_iso((e.get("end_date") or e.get("start_date") or "")[:10], tz)
            end = (
                datetime.combine(end_day.astimezone(tz).date(), time(23, 59), tz) if end_day else None
            )
        else:
            # utc_* fields are unambiguous; local fields are the site's timezone
            start = parse_iso((e.get("utc_start_date") or "").replace(" ", "T") or None) or parse_iso(
                (e.get("start_date") or "").replace(" ", "T") or None, tz)
            end = parse_iso((e.get("utc_end_date") or "").replace(" ", "T") or None) or parse_iso(
                (e.get("end_date") or "").replace(" ", "T") or None, tz)
        virtual = bool(e.get("is_virtual"))
        cost = html_to_text(e.get("cost"))
        summary = html_to_text(e.get("excerpt") or e.get("description"))
        if cost:
            summary = f"{summary} Cost: {cost}".strip()
        organizers = e.get("organizer") if isinstance(e.get("organizer"), list) else []
        title = html_to_text(e.get("title"))
        return self.make(
            kind=Kind.HACKATHON if "hackathon" in title.lower() else self.p.kind,
            title=title,
            url=e.get("url") or "",
            summary=summary,
            starts_at=start,
            ends_at=end,
            location="Online" if virtual else (_venue(e.get("venue")) or self.p.default_location),
            is_online=True if virtual else None,
            organizer=html_to_text(organizers[0].get("organizer")) if organizers and isinstance(organizers[0], dict) else None,
            tags=[html_to_text(c.get("name")) for c in e.get("categories") or [] if isinstance(c, dict) and c.get("name")],
        )


# ------------------------------------------------------------------ wordpress

class WordPressParams(SourceParams):
    base_url: str
    post_type: str = "posts"
    kind: Kind = Kind.EVENT
    per_page: int = 20
    listing_url: str | None = None
    default_location: str | None = None
    timezone: str | None = None  # default: profile timezone


def dates_from_listing(html: str, page_url: str = "") -> dict[str, str]:
    """canonical item URL -> the date text shown next to it on a listing page."""
    soup = BeautifulSoup(html, "html.parser")
    page_key = canonical_url(page_url) if page_url else None
    anchors = [(canonical_url(a["href"]), a) for a in soup.find_all("a", href=True)]
    anchors = [(k, a) for k, a in anchors if k != page_key]
    found: dict[str, str] = {}
    blocked: set[str] = set()
    # Closest context wins: the link's own text first, then each ancestor level in turn.
    # An ancestor that also links to a *different* item (same path prefix) is a list of cards,
    # not this item's card — climbing stops there so no item borrows a neighbour's date.
    for depth in range(4):
        for key, a in anchors:
            if key in found or key in blocked:
                continue
            node = a
            for _ in range(depth):
                node = node.parent if node is not None else None
            if node is None:
                continue
            if depth and _links_to_other_item(node, key):
                blocked.add(key)
                continue
            text = node.get_text(" ", strip=True)
            if text and parse_date_text(text):
                found[key] = text
    return found


def _links_to_other_item(node, key: str) -> bool:
    prefix = key.rsplit("/", 1)[0] + "/"
    for a in node.find_all("a", href=True):
        other = canonical_url(a["href"])
        if other != key and other.startswith(prefix):
            return True
    return False


def wp_tags(class_list) -> list[str]:
    """'tag-investors-2' -> 'investors' (WordPress de-duplicates slugs with -2, -3…)."""
    out: list[str] = []
    for c in class_list or []:
        if isinstance(c, str) and c.startswith("tag-"):
            tag = re.sub(r"-\d+$", "", c[4:]).replace("-", " ")
            if tag and tag not in out:
                out.append(tag)
    return out


@register
class WordPressSource(Source):
    type = "wordpress"
    Params = WordPressParams
    p: WordPressParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        base = self.p.base_url.rstrip("/")
        api = (
            f"{base}/wp-json/wp/v2/{self.p.post_type}"
            f"?per_page={self.p.per_page}&_fields=id,date,link,title,excerpt,class_list"
        )
        posts = await fx.get_json(api)
        if not isinstance(posts, list):
            raise ValueError(f"unexpected WordPress response: {type(posts).__name__}")

        listing_dates: dict[str, str] = {}
        if self.p.listing_url:
            try:
                listing_dates = dates_from_listing(await fx.get_text(self.p.listing_url), self.p.listing_url)
            except Exception as e:  # enrichment is best-effort
                self.warnings.append(f"listing page unavailable: {e}")

        return self.collect([self._item(p, listing_dates) for p in posts if isinstance(p, dict)])

    def _item(self, post: dict, listing_dates: dict[str, str]) -> Item | None:
        tz = self.tz
        link = post.get("link") or ""
        title = html_to_text((post.get("title") or {}).get("rendered"))
        excerpt = html_to_text((post.get("excerpt") or {}).get("rendered"))
        published = parse_iso(post.get("date"), tz)

        starts = ends = None
        if self.p.kind in (Kind.EVENT, Kind.HACKATHON, Kind.PROGRAM):
            span = parse_date_text(listing_dates.get(canonical_url(link))) if link else None
            if span:
                starts, ends = span_to_datetimes(span, tz)

        kind = self.p.kind
        if kind == Kind.EVENT and "hackathon" in title.lower():
            kind = Kind.HACKATHON

        return self.make(
            kind=kind,
            title=title,
            url=link,
            summary=excerpt,
            starts_at=starts,
            ends_at=ends,
            published_at=published,
            location=resolve_location(title, excerpt, self.p.default_location),
            is_online=None,
            tags=wp_tags(post.get("class_list")),
        )
