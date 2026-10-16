"""Model, canonical URLs, text/date parsing and place detection (SPEC §2, §3, §6)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time

import pytest
from pydantic import ValidationError

from eventparser.models import Kind, canonical_url, uid_for
from eventparser.textutil import (
    ELSEWHERE,
    html_to_text,
    parse_date_text,
    parse_iso,
    parse_relative_age,
    resolve_location,
    span_to_datetimes,
    truncate,
)
from tests.conftest import TURKEY_TZ, fixture_text, item


def test_item_cleans_fields():
    """R-MODEL-1: titles/summaries are plain text, whitespace collapsed, entities decoded."""
    it = item(title="  AI &amp; Data\n Summit ", summary="<p>Hello <b>Istanbul</b></p>" + "x" * 2000)
    assert it.title == "AI & Data Summit"
    assert it.summary.startswith("Hello Istanbul") and len(it.summary) <= 900


@pytest.mark.parametrize("bad", [dict(title="  "), dict(url="/relative"), dict(url="ftp://x"),
                                 dict(starts_at=datetime(2026, 1, 1))])
def test_item_rejects_invalid(bad):
    """R-MODEL-1: empty title, non-absolute URL, naive datetime are rejected."""
    with pytest.raises(ValidationError):
        item(**bad)


def test_datetimes_normalised_to_utc():
    """R-MODEL-1"""
    from datetime import UTC

    from tests.conftest import TURKEY_TZ
    it = item(starts_at=datetime(2026, 11, 3, 23, 59, tzinfo=TURKEY_TZ))
    assert it.starts_at.tzinfo == UTC and it.starts_at.hour == 20


def test_protocol_relative_url_is_fixed():
    assert item(url="//devpost.com/x").url == "https://devpost.com/x"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://WWW.Example.com/a/?utm_source=x&b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/e?fbclid=1&ref=tw", "https://example.com/e"),
        # LinkedIn jobs: slug, country subdomain and tracking all collapse to the job id
        ("https://tr.linkedin.com/jobs/view/software-intern-at-acme-4012345678?position=1&pageNum=0&refId=a&trackingId=b",
         "https://linkedin.com/jobs/view/4012345678"),
        ("https://www.linkedin.com/jobs/view/4012345678/", "https://linkedin.com/jobs/view/4012345678"),
        # LinkedIn posts: /posts/ slug and feed URN are the same post
        ("https://www.linkedin.com/posts/microsoft_ai-activity-7251234567890123456-AbCd?utm_source=share",
         "https://linkedin.com/feed/update/urn:li:activity:7251234567890123456"),
        ("https://www.linkedin.com/feed/update/urn:li:activity:7251234567890123456/",
         "https://linkedin.com/feed/update/urn:li:activity:7251234567890123456"),
    ],
)
def test_canonical_url(url, expected):
    """R-MODEL-3"""
    assert canonical_url(url) == expected


def test_uid_is_stable_and_canonical():
    """R-MODEL-2, R-DEDUP-1: same canonical URL → same uid."""
    a = uid_for("https://www.linkedin.com/jobs/view/intern-4012345678?trk=x")
    b = uid_for("https://tr.linkedin.com/jobs/view/4012345678")
    assert a == b and len(a) == 16
    assert item(url="https://example.com/x/").uid == item(url="https://example.com/x").uid


def test_deadline_prefers_end_for_hackathons():
    from datetime import UTC
    s, e = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 23, tzinfo=UTC)
    assert item(kind=Kind.HACKATHON, starts_at=s, ends_at=e).deadline == e
    assert item(kind=Kind.EVENT, starts_at=s, ends_at=e).deadline == s


# ------------------------------------------------------------------ test textutil

@pytest.mark.parametrize(
    "text, start, end, start_time",
    [
        ("19 October 2026 | 6-9 PM", date(2026, 10, 19), None, time(18, 0)),
        ("15 - 17 October 2026", date(2026, 10, 15), date(2026, 10, 17), None),
        ("6–7 October 2026", date(2026, 10, 6), date(2026, 10, 7), None),
        ("Tuesday, 13 October 2026 | 10 AM to 2 PM", date(2026, 10, 13), None, time(10, 0)),
        ("8 October 2026 | 6:45 PM PT", date(2026, 10, 8), None, time(18, 45)),
        ("1 October 2026 | 3 - 5 PM", date(2026, 10, 1), None, time(15, 0)),
        ("23 September 2026, 9:30 am - 12:30 pm", date(2026, 9, 23), None, time(9, 30)),
        ("22 September 2026, 6 - 8:30pm", date(2026, 9, 22), None, time(18, 0)),
        ("10 September 2026, 2:45 PM", date(2026, 9, 10), None, time(14, 45)),
        ("31 Aug - 3 Sep 2026", date(2026, 8, 31), date(2026, 9, 3), None),
        ("27 July – 19 August 2026", date(2026, 7, 27), date(2026, 8, 19), None),
        ("30 July – 2 August 2026", date(2026, 7, 30), date(2026, 8, 2), None),
        ("17 – 20 June 2026", date(2026, 6, 17), date(2026, 6, 20), None),
        ("22 September 2026", date(2026, 9, 22), None, None),
        # Devpost submission periods
        ("Aug 31 - Oct 23, 2026", date(2026, 8, 31), date(2026, 10, 23), None),
        ("Oct 01 - Nov 15, 2026", date(2026, 10, 1), date(2026, 11, 15), None),
        ("Dec 15, 2026 - Jan 10, 2027", date(2026, 12, 15), date(2027, 1, 10), None),
        ("Dec 15 - Jan 10, 2027", date(2026, 12, 15), date(2027, 1, 10), None),
        ("Sep 5 - 20, 2026", date(2026, 9, 5), date(2026, 9, 20), None),
        ("28 Dec 2026 - 2 Jan 2027", date(2026, 12, 28), date(2027, 1, 2), None),
        ("Join us on 2026-11-03 at TSTP", date(2026, 11, 3), None, None),
        ("Events AI Tinkerers Istanbul – Session 4 19 October 2026 | 6-9 PM", date(2026, 10, 19), None, time(18, 0)),
    ],
)
def test_parse_date_text_real_formats(text, start, end, start_time):
    """R-SRC-1, R-SRC-4: every date format seen on real sources parses."""
    span = parse_date_text(text)
    assert span is not None, text
    assert (span.start, span.end, span.start_time) == (start, end, start_time)


@pytest.mark.parametrize("text", ["Every Tuesday | 2-4pm", "", None, "Session 4", "Build #3", "may the best win"])
def test_parse_date_text_no_date(text):
    assert parse_date_text(text) is None


def test_invalid_calendar_date_is_rejected():
    assert parse_date_text("31 February 2026") is None


def test_span_to_datetimes_uses_turkey_time_and_end_of_day():
    span = parse_date_text("19 October 2026 | 6-9 PM")
    start, end = span_to_datetimes(span)
    assert start == datetime(2026, 10, 19, 18, 0, tzinfo=TURKEY_TZ)
    assert start.tzinfo == UTC
    # No explicit end → the event is still "current" until the end of that day.
    assert end == datetime(2026, 10, 19, 23, 59, tzinfo=TURKEY_TZ)


@pytest.mark.parametrize(
    "text, hours",
    [("2d", 48), ("3w", 24 * 21), ("5h", 5), ("1mo", 24 * 30), ("1yr", 24 * 365), ("30m", 0.5),
     ("2 days ago", 48), ("1 week ago", 24 * 7), ("Just now", 0)],
)
def test_parse_relative_age(text, hours):
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    assert (now - parse_relative_age(text, now)).total_seconds() == hours * 3600


def test_parse_iso_variants():
    assert parse_iso("2026-10-25T15:00:00Z") == datetime(2026, 10, 25, 15, tzinfo=UTC)
    assert parse_iso("2026-10-27T14:00:00.000Z") == datetime(2026, 10, 27, 14, tzinfo=UTC)
    # date-only in a local timezone
    assert parse_iso("2026-10-15", TURKEY_TZ) == datetime(2026, 10, 14, 21, tzinfo=UTC)
    assert parse_iso("not a date") is None
    assert parse_iso(None) is None


def test_clean_text_folds_fancy_unicode_and_markdown():
    from eventparser.textutil import clean_text
    assert clean_text("📅 **𝗦𝗮𝘁𝘂𝗿𝗱𝗮𝘆, 08:30 𝗔𝗠**") == "📅 Saturday, 08:30 AM"
    assert clean_text("# Turn Your Ideas\n**Online Event \\| Free**") == "Turn Your Ideas Online Event | Free"


def test_html_to_text_and_truncate():
    assert html_to_text("<p>AI &amp; <b>ML</b></p>\n\n<p>Istanbul</p>") == "AI & ML Istanbul"
    assert html_to_text("AI Tinkerers Istanbul &#8211; Session 4") == "AI Tinkerers Istanbul – Session 4"
    t = truncate("one two three four five six seven", 20)
    assert len(t) <= 20 and t.endswith("…") and " " not in t[-2:]


# ------------------------------------------------------------------ test places

DEFAULT = "TSTP, Istanbul, Turkey"


def captured_excerpts():
    return {html_to_text(e["title"]["rendered"]): html_to_text(e["excerpt"]["rendered"])
            for e in json.loads(fixture_text("tstp_events.json"))}


@pytest.mark.parametrize("title, expected", [
    ("TSTP at Sarajevo 2026", "Sarajevo"),
    ("TechCrunch Disrupt 2026", "San Francisco"),
    ("Alif x Turkey Science and Technology Park Mixer #SFTechWeek", "San Francisco"),
    ("AI Everything Abu Dhabi 2026", "Abu Dhabi"),
    ("GRAiCE-Med Global Summit 2026", "Italy"),
    ("VivaTech Paris 2026", "Paris"),
    ("SYNC 2026", ELSEWHERE),
    # genuinely local
    ("AI Tinkerers Istanbul – Session 4", DEFAULT),
    ("Pitch to The 300", DEFAULT),
    ("Google Cloud Summit Istanbul 2026", DEFAULT),
    ("Nestwork Insider", DEFAULT),
])
def test_real_feed_locations(title, expected):
    """A real events feed that also announces the organisation's trips abroad."""
    assert resolve_location(title, captured_excerpts()[title], DEFAULT) == expected


@pytest.mark.parametrize("title, text, expected", [
    ("Investor Day", "Meet investors from Europe and the USA here.", DEFAULT),  # not locative
    ("Web Summit", "Our team is heading to Web Summit Turkey next month.", DEFAULT),
    ("Expo", "We are heading to GITEX in Dubai.", "Dubai"),
    ("Anything", "No place at all", DEFAULT),
])
def test_rules(title, text, expected):
    assert resolve_location(title, text, DEFAULT) == expected


def test_home_comes_from_the_default_location_so_it_works_anywhere():
    london = "Tech Hub, London, UK"
    assert resolve_location("AI Night London", "", london) == london
    assert resolve_location("AI Night Istanbul", "", london) == "Istanbul"
    assert resolve_location("Demo day", "We are heading to London Tech Week.", london) == london


def test_no_default_means_no_guess():
    assert resolve_location("VivaTech Paris", "", None) is None


def test_places_are_case_sensitive_proper_nouns():
    assert resolve_location("We egypt-trot", "", DEFAULT) == DEFAULT
    assert resolve_location("Event in Egypt", "", DEFAULT) == "Egypt"
