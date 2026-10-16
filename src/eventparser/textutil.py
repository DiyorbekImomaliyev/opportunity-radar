"""Text cleaning, date extraction and place detection.

Dates seen on real sites, all supported:
  19 October 2026 | 6-9 PM          15 - 17 October 2026          6–7 October 2026
  31 Aug - 3 Sep 2026               30 July – 2 August 2026       Tuesday, 13 October 2026 | 10 AM to 2 PM
  23 September 2026, 9:30 am - 12:30 pm                           Aug 31 - Oct 23, 2026
  Dec 15, 2026 - Jan 10, 2027       Sep 5 - 20, 2026              2026-10-19
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

_local_tz = ZoneInfo("UTC")


def set_local_tz(name: str) -> None:
    """The owner's timezone (profile `timezone`): used for display, 'today', and naive dates."""
    global _local_tz
    _local_tz = ZoneInfo(name)


def local_tz() -> ZoneInfo:
    return _local_tz

_WS = re.compile(r"\s+")


def html_to_text(s: str | None, sep: str = " ") -> str:
    """Strip tags. `sep` joins text nodes: " " for block markup, "" for inline (e.g. "$<span>5</span>")."""
    if not s:
        return ""
    if "<" in s and ">" in s:
        s = BeautifulSoup(s, "html.parser").get_text(sep)
    return _WS.sub(" ", html.unescape(s)).strip()


_MD = [
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),        # **bold** / __bold__
    (re.compile(r"(?m)^\s*#{1,6}\s+"), ""),             # # headings
    (re.compile(r"\\([|*_#\[\]()-])"), r"\1"),            # escaped markdown chars
    (re.compile(r"\*\*"), ""),                           # stray **
]


def clean_text(s: str | None) -> str:
    """Plain, readable text: tags stripped, Unicode 'fancy' letters folded (𝗦𝗮𝘁 → Sat), Markdown removed."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for pat, repl in _MD:
        s = pat.sub(repl, s)
    return html_to_text(s)


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    if " " in cut[n // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


# ---------------------------------------------------------------- dates

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?(?![a-z])"
)
_D = r"(\d{1,2})(?:st|nd|rd|th)?"
_Y = r"(20\d{2})"
_DASH = r"\s*(?:-|to|until)\s*"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 30 July - 2 August 2026 / 31 Aug - 3 Sep 2026 / 28 Dec 2026 - 2 Jan 2027
    ("dmy_dmy", re.compile(rf"(?<!\d){_D}\s+{_MON},?\s*{_Y}?{_DASH}{_D}\s+{_MON},?\s*{_Y}", re.I)),
    # 15 - 17 October 2026
    ("dd_my", re.compile(rf"(?<!\d){_D}{_DASH}{_D}\s+{_MON},?\s*{_Y}", re.I)),
    # Aug 31 - Oct 23, 2026 / Dec 15, 2026 - Jan 10, 2027
    ("mdy_mdy", re.compile(rf"(?<![a-z]){_MON}\s+{_D},?\s*{_Y}?{_DASH}{_MON}\s+{_D},?\s*{_Y}", re.I)),
    # Sep 5 - 20, 2026
    ("md_dy", re.compile(rf"(?<![a-z]){_MON}\s+{_D}{_DASH}{_D},?\s*{_Y}", re.I)),
    ("dmy", re.compile(rf"(?<!\d){_D}\s+{_MON},?\s*{_Y}", re.I)),
    ("mdy", re.compile(rf"(?<![a-z]){_MON}\s+{_D},?\s*{_Y}", re.I)),
    ("iso", re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")),
]

_TIME = re.compile(
    r"^[\s|,·@:\-]*(?:from\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
    r"(?:\s*(?:-|to)\s*\d{1,2}(?::\d{2})?\s*(am|pm))?",
    re.I,
)


@dataclass(frozen=True)
class DateSpan:
    start: date
    end: date | None = None
    start_time: time | None = None


def _mon(s: str) -> int:
    return _MONTHS[s[:3].lower()]


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _span_from_match(kind: str, g: tuple[str | None, ...]) -> tuple[date, date | None] | None:
    if kind == "dmy_dmy":
        d1, m1, y1, d2, m2, y2 = g
        m1i, m2i, y2i = _mon(m1), _mon(m2), int(y2)
        y1i = int(y1) if y1 else (y2i - 1 if m1i > m2i else y2i)
        return _safe_date(y1i, m1i, int(d1)), _safe_date(y2i, m2i, int(d2))
    if kind == "dd_my":
        d1, d2, m, y = g
        return _safe_date(int(y), _mon(m), int(d1)), _safe_date(int(y), _mon(m), int(d2))
    if kind == "mdy_mdy":
        m1, d1, y1, m2, d2, y2 = g
        m1i, m2i, y2i = _mon(m1), _mon(m2), int(y2)
        y1i = int(y1) if y1 else (y2i - 1 if m1i > m2i else y2i)
        return _safe_date(y1i, m1i, int(d1)), _safe_date(y2i, m2i, int(d2))
    if kind == "md_dy":
        m, d1, d2, y = g
        return _safe_date(int(y), _mon(m), int(d1)), _safe_date(int(y), _mon(m), int(d2))
    if kind == "dmy":
        d, m, y = g
        return _safe_date(int(y), _mon(m), int(d)), None
    if kind == "mdy":
        m, d, y = g
        return _safe_date(int(y), _mon(m), int(d)), None
    if kind == "iso":
        y, m, d = g
        return _safe_date(int(y), int(m), int(d)), None
    return None


def _parse_time(rest: str) -> time | None:
    m = _TIME.match(rest[:40])
    if not m:
        return None
    hour, minute, ampm, end_ampm = m.group(1), m.group(2), m.group(3), m.group(4)
    ampm = (ampm or end_ampm or "").lower()
    if not ampm:
        return None  # a bare number is too ambiguous to be a time
    h = int(hour)
    if not 1 <= h <= 12:
        return None
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return time(h, int(minute or 0))


def parse_date_text(text: str | None) -> DateSpan | None:
    """Find the first date or date range in free text."""
    if not text:
        return None
    s = _WS.sub(" ", html.unescape(text).replace("–", "-").replace("—", "-"))
    best: tuple[int, int, str, re.Match[str]] | None = None
    for kind, pat in _PATTERNS:
        for m in pat.finditer(s):
            key = (m.start(), -(m.end() - m.start()))
            if best is None or key < (best[0], best[1]):
                best = (key[0], key[1], kind, m)
            break  # earliest of this pattern is enough
    if best is None:
        return None
    _, _, kind, m = best
    span = _span_from_match(kind, m.groups())
    if span is None or span[0] is None:
        return None
    start, end = span
    if end is not None and end < start:
        end = None
    return DateSpan(start=start, end=end, start_time=_parse_time(s[m.end():]))


def span_to_datetimes(span: DateSpan, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """(starts_at, ends_at) in UTC. Without an explicit end, the event ends at the end of its day."""
    tz = tz or local_tz()
    start = datetime.combine(span.start, span.start_time or time(0, 0), tz)
    end = datetime.combine(span.end or span.start, time(23, 59), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


_REL = re.compile(
    r"(\d+)\s*(mo|months?|yrs?|years?|w|weeks?|d|days?|h|hrs?|hours?|m|mins?|minutes?)\b", re.I
)


def parse_relative_age(text: str | None, now: datetime) -> datetime | None:
    """LinkedIn-style ages: '2d', '3w', '5h', '1mo', '1yr', '30m', '2 days ago'."""
    if not text:
        return None
    t = text.strip().lower()
    if "just now" in t or t == "now":
        return now
    m = _REL.search(t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("mo"):
        delta = timedelta(days=30 * n)
    elif unit.startswith("y"):
        delta = timedelta(days=365 * n)
    elif unit.startswith("w"):
        delta = timedelta(weeks=n)
    elif unit.startswith("d"):
        delta = timedelta(days=n)
    elif unit.startswith("h"):
        delta = timedelta(hours=n)
    else:
        delta = timedelta(minutes=n)
    return now - delta


def parse_iso(s: str | None, default_tz: ZoneInfo | None = None) -> datetime | None:
    """ISO-8601 → aware UTC datetime. Naive values get default_tz (UTC if not given)."""
    if not s:
        return None
    s = s.strip()
    try:
        if len(s) == 10:
            dt = datetime.combine(date.fromisoformat(s), time(0, 0))
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz or UTC)
    return dt.astimezone(UTC)


# ------------------------------------------------------------------ places

PLACES = [
    # Middle East
    "Doha", "Qatar", "Abu Dhabi", "Dubai", "Sharjah", "UAE", "Riyadh", "Jeddah", "Saudi Arabia",
    "Bahrain", "Manama", "Kuwait", "Muscat", "Oman", "Cairo", "Egypt", "Amman", "Jordan", "Beirut",
    "Lebanon", "Istanbul", "Turkey", "Türkiye", "Ankara", "Baku", "Azerbaijan", "Tashkent",
    "Uzbekistan", "Almaty", "Kazakhstan",
    # Europe
    "London", "Manchester", "Paris", "Berlin", "Munich", "Hamburg", "Amsterdam", "Brussels", "Lisbon",
    "Madrid", "Barcelona", "Rome", "Milan", "Italy", "Spain", "France", "Germany", "Portugal",
    "Netherlands", "Switzerland", "Geneva", "Zurich", "Davos", "Vienna", "Austria", "Stockholm",
    "Helsinki", "Oslo", "Copenhagen", "Dublin", "Ireland", "Warsaw", "Prague", "Budapest", "Athens",
    "Sarajevo", "Bosnia", "Belgrade", "Zagreb", "United Kingdom", "Europe",
    # Americas
    "San Francisco", "Silicon Valley", "Palo Alto", "New York", "NYC", "Boston", "Austin",
    "Las Vegas", "Los Angeles", "Seattle", "Chicago", "Miami", "Washington DC", "Toronto",
    "Montreal", "Vancouver", "Canada", "Mexico City", "São Paulo", "Sao Paulo", "Brazil", "USA",
    "United States",
    # Asia / Africa / Oceania
    "Singapore", "Hong Kong", "Tokyo", "Japan", "Seoul", "Korea", "Beijing", "Shanghai", "Shenzhen",
    "China", "Taipei", "Bangalore", "Bengaluru", "Mumbai", "Delhi", "India", "Karachi", "Lahore",
    "Islamabad", "Pakistan", "Jakarta", "Kuala Lumpur", "Malaysia", "Bangkok", "Manila", "Sydney",
    "Melbourne", "Australia", "Nairobi", "Kenya", "Lagos", "Nigeria", "Kigali", "Rwanda",
    "Johannesburg", "Cape Town", "South Africa", "Casablanca", "Morocco", "Tunis", "Algiers",
]

_ALT = "|".join(re.escape(p) for p in sorted(PLACES, key=len, reverse=True))
_PLACE = re.compile(rf"(?<![A-Za-z])({_ALT})(?![A-Za-z])")
# In running text a place only counts when used as a location: "in Paris", "to Sarajevo",
# "ADNEC Centre, Abu Dhabi" — not "investors from Europe".
_PLACE_LOCATIVE = re.compile(rf"(?:\b(?:in|to|at)\s+(?:the\s+)?|,\s*)({_ALT})(?![A-Za-z])")
# An organisation announcing that *it* is travelling to someone else's event.
_TRAVEL = re.compile(
    r"\b(heading to|headed to|head(?:ing)? over to|will be attending|is attending|are attending"
    r"|we[’']re attending|will be travell?ing to|flying to)\b",
    re.I,
)

ELSEWHERE = "Elsewhere (travel announcement)"


def _home_words(default: str) -> set[str]:
    return {w.strip().lower() for w in default.split(",") if w.strip()}


def _away(match: re.Match[str] | None, home: set[str]) -> str | None:
    return match.group(1) if match and match.group(1).lower() not in home else None


def resolve_location(title: str, text: str, default: str | None) -> str | None:
    """The default location unless the item says the event is elsewhere.

    1. a non-home place in the title ("VivaTech Paris 2026")
    2. a non-home place used as a location in the text ("will be in San Francisco")
    3. a travel phrase whose destination doesn't mention home ("headed to SYNC'26")
    Otherwise the default stands: when unsure, keep the item (missing an event costs more).
    """
    if default is None:
        return None
    home = _home_words(default)
    for m in (*_PLACE.finditer(title), *_PLACE_LOCATIVE.finditer(text or "")):
        if place := _away(m, home):
            return place
    for source in (title, text or ""):
        if m := _TRAVEL.search(source):
            destination = source[m.end() : m.end() + 60].lower()
            return default if any(h in destination for h in home) else ELSEWHERE
    return default
