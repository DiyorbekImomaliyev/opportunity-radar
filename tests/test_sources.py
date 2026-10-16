"""Source adapters against captured fixtures (SPEC §3). HTTP is mocked with respx."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from eventparser.models import Kind
from eventparser.sources import REGISTRY
from eventparser.sources.events import dates_from_listing, iter_typed, ld_blocks
from eventparser.sources.linkedin import extract_posts, is_auth_wall, page_name
from tests.conftest import NOW, TURKEY_TZ, fixture_text, make_fetcher, run


def fetch(type_: str, params: dict, name: str = "src"):
    src = REGISTRY[type_](name, params, now=NOW)

    async def go():
        async with make_fetcher() as fx:
            return await src.fetch(fx)

    return src, run(go())


# ------------------------------------------------------------------ wordpress (TSTP)

def test_dates_from_listing_maps_each_event_to_its_own_date():
    """R-SRC-1: listing page date enrichment picks the date next to each link."""
    dates = dates_from_listing(fixture_text("tstp_listing.html"), "https://techpark.example/events/")
    assert "19 October 2026" in dates["https://techpark.example/events/ai-tinkerers-istanbul-session-4"]
    assert "30 July" in dates["https://techpark.example/events/build-for-tstp-hackathon-internship-edition"]
    assert "15 - 17 October 2026" in dates["https://techpark.example/events/tstp-at-sarajevo-2026"]
    # Recurring event without a calendar date must not borrow a neighbour's date.
    assert "https://techpark.example/events/pitch-to-the-300" not in dates


@respx.mock
def test_wordpress_tstp_events():
    """R-SRC-1"""
    respx.get(url__startswith="https://techpark.example/wp-json/wp/v2/events").respond(
        200, text=fixture_text("tstp_events.json"), headers={"content-type": "application/json"})
    respx.get("https://techpark.example/events/").respond(200, text=fixture_text("tstp_listing.html"))
    src, items = fetch("wordpress", {
        "base_url": "https://techpark.example", "post_type": "events",
        "listing_url": "https://techpark.example/events/", "default_location": "TSTP, Istanbul, Turkey",
    })
    assert len(items) == 20 and src.skipped == 0
    by_title = {i.title: i for i in items}
    t = by_title["AI Tinkerers Istanbul – Session 4"]
    assert t.kind == Kind.EVENT and t.location == "TSTP, Istanbul, Turkey"
    assert t.starts_at == datetime(2026, 10, 19, 18, 0, tzinfo=TURKEY_TZ)
    assert "Scale AI" in t.summary
    pitch = by_title["Pitch to The 300"]
    assert pitch.starts_at is None and "investors" in pitch.summary.lower()
    hack = next(i for i in items if "Internship Edition" in i.title)
    assert hack.kind == Kind.HACKATHON
    assert hack.ends_at.astimezone(TURKEY_TZ).date().isoformat() == "2026-08-02"


@respx.mock
def test_wordpress_listing_failure_is_a_warning_not_an_error():
    """R-SRC-9: enrichment is best-effort."""
    respx.get(url__startswith="https://techpark.example/wp-json/").respond(200, text=fixture_text("tstp_events.json"))
    respx.get("https://techpark.example/events/").respond(404)
    src, items = fetch("wordpress", {"base_url": "https://techpark.example", "post_type": "events",
                                     "listing_url": "https://techpark.example/events/"})
    assert len(items) == 20 and src.warnings


@respx.mock
def test_wordpress_jobs():
    respx.get(url__startswith="https://techpark.example/wp-json/wp/v2/job").respond(200, text=fixture_text("tstp_jobs.json"))
    _, items = fetch("wordpress", {"base_url": "https://techpark.example", "post_type": "job", "kind": "job"})
    assert {i.title for i in items} == {"Communications Manager", "Finance Manager"}
    assert all(i.kind == Kind.JOB and i.published_at for i in items)


# ------------------------------------------------------------------ bevy (GDG)

@respx.mock
def test_bevy_gdg_istanbul():
    """R-SRC-2"""
    respx.get(url__regex=r"https://gdg\.community\.dev/api/event_slim/for_chapter/657/").respond(
        200, text=fixture_text("bevy_gdg_istanbul.json"))
    _, items = fetch("bevy", {"chapter_id": 657, "default_location": "Istanbul, Turkey"})
    assert len(items) == 1
    ev = items[0]
    assert ev.title.startswith("Tech Talk: Deploy a RAG AI Agent")
    assert ev.starts_at == datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
    assert ev.is_online is False and ev.location == "Istanbul, Turkey"
    assert ev.organizer == "GDG Istanbul" and ev.url.startswith("https://gdg.community.dev/")


@respx.mock
def test_bevy_virtual_event_is_online():
    data = json.loads(fixture_text("bevy_gdg_istanbul.json"))
    data["results"][0]["audience_type"] = "VIRTUAL"
    respx.get(url__regex=r".*/for_chapter/1/.*").respond(200, json=data)
    _, items = fetch("bevy", {"chapter_id": 1, "default_location": "Istanbul, Turkey"})
    assert items[0].is_online is True and items[0].location == "Online"


@respx.mock
def test_bevy_bad_shape_raises():
    respx.get(url__regex=r".*/for_chapter/1/.*").respond(200, json={"detail": "nope"})
    with pytest.raises(ValueError):
        fetch("bevy", {"chapter_id": 1})


# ------------------------------------------------------------------ JSON-LD

@pytest.mark.parametrize("fixture, min_events", [
    ("meetup_istanbul_ai.html", 10), ("eventbrite_istanbul_ai.html", 15), ("devevents_tr.html", 2),
])
def test_jsonld_extracts_events_from_real_pages(fixture, min_events):
    """R-SRC-3: nested lists / itemListElement are walked."""
    events = [e for b in ld_blocks(fixture_text(fixture)) for e in iter_typed(b, ("Event",))]
    assert len(events) >= min_events


@respx.mock
def test_jsonld_source_maps_fields():
    """R-SRC-3"""
    respx.get("https://www.meetup.com/find/").respond(200, text=fixture_text("meetup_istanbul_ai.html"))
    respx.get("https://www.eventbrite.com/d/turkey--istanbul/ai/").respond(200, text=fixture_text("eventbrite_istanbul_ai.html"))
    src, items = fetch("jsonld", {"urls": ["https://www.meetup.com/find/", "https://www.eventbrite.com/d/turkey--istanbul/ai/"],
                                  "default_location": "Istanbul, Turkey"})
    assert len(items) >= 25
    gdg = next(i for i in items if "Deploy a RAG AI Agent" in i.title)
    assert gdg.starts_at == datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
    assert gdg.url.startswith("https://www.meetup.com/")
    assert all(i.starts_at is None or i.starts_at.tzinfo for i in items)


@respx.mock
def test_jsonld_one_url_failing_is_partial():
    respx.get("https://a.example/").respond(200, text=fixture_text("meetup_istanbul_ai.html"))
    respx.get("https://b.example/").respond(404)
    src, items = fetch("jsonld", {"urls": ["https://a.example/", "https://b.example/"]})
    assert items and any("b.example" in w for w in src.warnings)


@respx.mock
def test_jsonld_all_urls_failing_raises():
    respx.get("https://b.example/").respond(404)
    with pytest.raises(RuntimeError):
        fetch("jsonld", {"urls": ["https://b.example/"]})


def test_jsonld_invalid_block_is_skipped():
    html = ('<script type="application/ld+json">{broken</script>'
            '<script type="application/ld+json">{"@type":"Event","name":"X","url":"https://x.y","startDate":"2026-11-01"}</script>')
    assert [e["name"] for b in ld_blocks(html) for e in iter_typed(b, ("Event",))] == ["X"]


def test_jsonld_relative_urls_resolved_against_page():
    """R-SRC-3: TurkeyLiving publishes url "/event/…" (seen live 2026-10-15)."""
    from eventparser.sources.events import event_to_fields
    f = event_to_fields({"@type": "Event", "name": "Expo", "url": "/event/expo-2026", "startDate": "2026-10-20"},
                        tz=TURKEY_TZ, default_kind=Kind.EVENT, default_location=None,
                        page_url="https://www.cityliving.example/en/events")
    assert f["url"] == "https://www.cityliving.example/event/expo-2026"


def test_jsonld_attendance_mode_and_virtual_location():
    from eventparser.sources.events import event_to_fields
    online = event_to_fields(
        {"@type": "Event", "name": "Online AI Hackathon", "url": "https://x.y",
         "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode",
         "location": {"@type": "VirtualLocation", "url": "https://zoom.us"}},
        tz=TURKEY_TZ, default_kind=Kind.EVENT, default_location="Istanbul, Turkey")
    assert online["is_online"] is True and online["location"] == "Online" and online["kind"] == Kind.HACKATHON
    place = event_to_fields(
        {"@type": "Event", "name": "Meetup", "url": "https://x.y",
         "location": {"@type": "Place", "name": "TSTP", "address": {"addressLocality": "Istanbul", "addressCountry": "QA"}}},
        tz=TURKEY_TZ, default_kind=Kind.EVENT, default_location=None)
    assert place["location"] == "TSTP, Istanbul, QA"


# ------------------------------------------------------------------ devpost

@respx.mock
def test_devpost_paginates_and_maps():
    """R-SRC-4"""
    route = respx.get(url__startswith="https://devpost.com/api/hackathons")
    route.side_effect = [
        httpx.Response(200, text=fixture_text("devpost_p1.json")),
        httpx.Response(200, json={"hackathons": [], "meta": {}}),
    ]
    _, items = fetch("devpost", {"max_pages": 5})
    assert route.call_count == 2 and len(items) == 9
    amazon = next(i for i in items if "Amazon" in i.title)
    assert amazon.kind == Kind.HACKATHON and amazon.is_online is True
    assert amazon.ends_at.date().isoformat() == "2026-10-23"
    assert "$138,000" in amazon.summary and "Machine Learning/AI" in amazon.tags


@respx.mock
def test_devpost_theme_filter():
    respx.get(url__startswith="https://devpost.com/api/hackathons").side_effect = [
        httpx.Response(200, text=fixture_text("devpost_p1.json")), httpx.Response(200, json={"hackathons": []}),
    ]
    _, items = fetch("devpost", {"themes": ["machine learning/ai"]})
    assert items and all(any(t.lower() == "machine learning/ai" for t in i.tags) for i in items)


# ------------------------------------------------------------------ linkedin jobs

@respx.mock
def test_linkedin_jobs_parses_and_dedupes_cards():
    """R-SRC-5"""
    route = respx.get(url__startswith="https://www.linkedin.com/jobs-guest/")
    route.respond(200, text=fixture_text("li_jobs.html"))
    _, items = fetch("linkedin_jobs", {"keywords": ["intern"], "locations": ["Turkey"], "max_pages": 1})
    assert len(items) == 3  # 4 cards, one duplicate id
    ai = next(i for i in items if i.title == "AI Research Intern")
    assert ai.url == "https://www.linkedin.com/jobs/view/4012345678"
    assert ai.organizer == "NCRI" and ai.location == "Istanbul, Istanbul, Turkey"
    assert ai.published_at.date().isoformat() == "2026-10-14"
    q = route.calls[0].request.url.params
    assert q["keywords"] == "intern" and q["location"] == "Turkey" and q["f_TPR"] == "r259200"


@respx.mock
def test_linkedin_jobs_remote_filter_marks_online():
    respx.get(url__startswith="https://www.linkedin.com/jobs-guest/").respond(200, text=fixture_text("li_jobs.html"))
    _, items = fetch("linkedin_jobs", {"keywords": ["AI intern"], "locations": ["Worldwide"],
                                       "filters": {"f_WT": "2"}, "max_pages": 1})
    assert all(i.is_online for i in items)


@respx.mock
def test_linkedin_jobs_stops_paginating_on_short_page():
    route = respx.get(url__startswith="https://www.linkedin.com/jobs-guest/")
    route.respond(200, text=fixture_text("li_jobs.html"))  # 3 unique < page_size 10
    fetch("linkedin_jobs", {"keywords": ["intern"], "max_pages": 5})
    assert route.call_count == 1


@respx.mock
def test_linkedin_jobs_all_queries_failing_raises():
    respx.get(url__startswith="https://www.linkedin.com/jobs-guest/").respond(400)
    with pytest.raises(RuntimeError):
        fetch("linkedin_jobs", {"keywords": ["a", "b"]})


def test_linkedin_jobs_rotate_queries_daily():
    """R-SRC-5: max_queries runs a slice of (keyword, location) pairs per day and covers all of
    them over consecutive days."""
    from eventparser.sources.linkedin import LinkedInJobsSource
    params = {"keywords": ["a", "b", "c"], "locations": ["X", "Y"], "max_queries": 4}
    seen = set()
    for day in range(2):
        src = LinkedInJobsSource("s", params, now=NOW + timedelta(days=day))
        today = src.queries()
        assert len(today) == 4
        seen |= set(today)
    assert len(seen) == 6
    assert len(LinkedInJobsSource("s", {"keywords": ["a"], "locations": ["X"]}, now=NOW).queries()) == 1


@respx.mock
def test_linkedin_job_details_tag_visa_and_skip_known_jobs():
    """R-SRC-5: up to `details` new jobs are opened; visa/citizenship lines become tags and lead
    the summary; jobs already in the store are not opened again."""
    from eventparser.sources.linkedin import LinkedInJobsSource
    respx.get(url__startswith="https://www.linkedin.com/jobs-guest/jobs/api/seeMore").respond(
        200, text=fixture_text("li_jobs.html"))
    details = {
        "4012345678": "<div class='show-more-less-html__markup'>Python, SQL. We offer visa sponsorship and relocation.</div>",
        "4012345679": "<div class='show-more-less-html__markup'>This role is open to UAE Nationals only.</div>",
    }
    route = respx.get(url__regex=r".*/jobPosting/\d+").mock(
        side_effect=lambda req: httpx.Response(200, text=details[str(req.url).rsplit("/", 1)[1]]))
    src = LinkedInJobsSource("s", {"keywords": ["intern"], "locations": ["UAE"], "max_pages": 1, "details": 5}, now=NOW)
    third = "https://www.linkedin.com/jobs/view/4012345680"
    known_uid = next(i.uid for i in run(_plain_items()) if i.url == third)
    src.known = lambda uid: uid == known_uid

    async def go():
        async with make_fetcher() as fx:
            return await src.fetch(fx)

    by_id = {i.url.rsplit("/", 1)[1]: i for i in run(go())}
    assert route.call_count == 2  # the known third job was not opened
    assert "visa:sponsored" in by_id["4012345678"].tags
    assert by_id["4012345678"].summary.startswith("Eligibility: We offer visa sponsorship and relocation. — NCRI")
    assert "visa:restricted" in by_id["4012345679"].tags
    assert not any(t.startswith("visa:") for t in by_id["4012345680"].tags)


async def _plain_items():
    from eventparser.sources.linkedin import LinkedInJobsSource
    async with make_fetcher() as fx:
        return await LinkedInJobsSource("s", {"keywords": ["intern"], "locations": ["UAE"], "max_pages": 1}, now=NOW).fetch(fx)


def test_real_linkedin_job_detail_description():
    """R-SRC-5: the real guest job-posting page (MSD Gulf intern, Dubai) yields its description."""
    from eventparser.sources.jobs import visa_tags
    from eventparser.sources.linkedin import job_description
    text = job_description(fixture_text("li_job_detail_real.html"))
    assert text.startswith("Job Description As a member of the Commercial Operations") and "Monthly stipend" in text
    assert visa_tags(text) == []  # an empty "VISA Sponsorship" form field is not an offer


@respx.mock
def test_linkedin_jobs_drop_agency_reposts():
    """Same role posted under many branch names (MedTourEasy Dubai / Jaipur …, seen live) → one."""
    from eventparser.sources.linkedin import _dedup_reposts
    from tests.conftest import item
    items = [item(url=f"https://x/{i}", title="Data Analyst Trainee", kind=Kind.JOB, organizer=f"MedTourEasy {c}")
             for i, c in enumerate(["Dubai", "Jaipur", "AP"])]
    items.append(item(url="https://x/9", title="Data Analyst Trainee", kind=Kind.JOB, organizer="Careem"))
    assert [i.organizer for i in _dedup_reposts(items)] == ["MedTourEasy Dubai", "Careem"]


@pytest.mark.parametrize(("text", "tags"), [
    ("We offer visa sponsorship and relocation support.", ["visa:sponsored"]),
    ("We sponsor work visas for international hires.", ["visa:sponsored"]),
    ("We are unable to sponsor visas for this role.", ["visa:restricted"]),
    ("Visa sponsorship is not available.", ["visa:restricted"]),
    ("Candidates must have the right to work in the UK.", ["visa:restricted"]),
    ("A valid work permit is required.", ["visa:restricted"]),
    ("This internship is open to UAE Nationals only.", ["visa:restricted"]),
    ("Only EU citizens may apply.", ["visa:restricted"]),
    ("Part of the Emiratisation programme.", ["visa:restricted"]),
    ("Saudi nationals only. Tamheer program.", ["visa:restricted"]),
    ("Great team, Python, SQL, monthly stipend.", []),
    # application-form fields (Workday): an empty label means nothing, an answer counts
    ("Employee Status Intern Relocation VISA Sponsorship Travel Requirements Not Applicable", []),
    ("VISA Sponsorship: No Travel Requirements: 10%", ["visa:restricted"]),
    ("VISA Sponsorship: Yes Travel Requirements: None", ["visa:sponsored"]),
    ("Visa Sponsorship Available. Apply now", ["visa:sponsored"]),
])
def test_visa_tags(text, tags):
    """R-SRC-5, R-SRC-11: offers vs restrictions; a restriction wins."""
    from eventparser.sources.jobs import visa_tags
    assert visa_tags(text) == tags


# ------------------------------------------------------------------ linkedin posts

def test_extract_posts_company_feed_cards():
    """R-SRC-6 strategy 2"""
    posts = extract_posts(fixture_text("li_company.html"), NOW)
    assert len(posts) == 3
    first = posts[0]
    assert "AI Agents Hackathon in Istanbul" in first["text"]
    assert "activity-7251000000000000001" in first["url"]
    assert first["published"] == NOW - timedelta(days=2)
    assert posts[1]["published"] == NOW - timedelta(weeks=3)


def test_extract_posts_profile_links():
    """R-SRC-6 strategy 3"""
    posts = extract_posts(fixture_text("li_profile.html"), NOW)
    assert len(posts) == 2
    assert "Demo Day" in posts[0]["text"] and posts[0]["published"] == NOW - timedelta(days=1)


def test_extract_posts_jsonld_first():
    """R-SRC-6 strategy 1"""
    html = ('<script type="application/ld+json">{"@context":"http://schema.org","@type":"DiscussionForumPosting",'
            '"url":"https://www.linkedin.com/posts/x_y-activity-7253000000000000001-AbCd",'
            '"articleBody":"Applications open for our AI fellowship","datePublished":"2026-10-14T09:00:00Z"}</script>')
    posts = extract_posts(html, NOW)
    assert posts == [{"url": "https://www.linkedin.com/posts/x_y-activity-7253000000000000001-AbCd",
                      "text": "Applications open for our AI fellowship",
                      "published": datetime(2026, 10, 14, 9, tzinfo=UTC)}]


def test_page_name_and_auth_wall():
    assert page_name(fixture_text("li_company.html"), "https://www.linkedin.com/company/microsoft/") == "Microsoft"
    assert page_name(fixture_text("li_profile.html"), "https://www.linkedin.com/in/jane/") == "Jane Founder"
    assert page_name("<html></html>", "https://www.linkedin.com/company/turkey-foundation/") == "Turkey Foundation"
    assert is_auth_wall("https://www.linkedin.com/authwall?trk=x", "")
    assert is_auth_wall("https://www.linkedin.com/company/x/", fixture_text("li_authwall.html"))
    assert not is_auth_wall("https://www.linkedin.com/company/microsoft/", fixture_text("li_company.html"))


@respx.mock
def test_linkedin_posts_source_partial_auth_wall():
    """R-SRC-6: an auth-walled page is reported; other pages still produce posts."""
    respx.get("https://www.linkedin.com/company/microsoft/").respond(200, text=fixture_text("li_company.html"))
    respx.get("https://www.linkedin.com/company/tiny-startup/").respond(
        302, headers={"Location": "https://www.linkedin.com/authwall?trk=x"})
    respx.get(url__startswith="https://www.linkedin.com/authwall").respond(200, text=fixture_text("li_authwall.html"))
    src, items = fetch("linkedin_posts", {"pages": ["https://www.linkedin.com/company/microsoft/",
                                                     "https://www.linkedin.com/company/tiny-startup/"]})
    assert len(items) == 3
    assert items[0].kind == Kind.POST and items[0].organizer == "Microsoft"
    assert items[0].title.startswith("Microsoft: Register now for the Microsoft AI Agents Hackathon")
    assert any("tiny-startup" in w and "auth wall" in w for w in src.warnings)


def test_summarize_errors_groups_identical_reasons():
    from eventparser.sources.base import summarize_errors
    errs = [("p1", "blocked"), ("p2", "blocked"), ("p3", "blocked"), ("p4", "HTTP 404")]
    assert summarize_errors(errs, "page") == "blocked (3 pages); p4: HTTP 404"


@respx.mock
def test_linkedin_posts_all_walled_raises():
    respx.get("https://www.linkedin.com/company/x/").respond(999)
    with pytest.raises(RuntimeError, match="999"):
        fetch("linkedin_posts", {"pages": ["https://www.linkedin.com/company/x/"]})


# ------------------------------------------------------------------ rss

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>TF News</title>
<item><title>AI week at Tech District</title><link>https://www.foundation.example/news/ai-week</link>
<description>&lt;p&gt;Workshops all week&lt;/p&gt;</description><pubDate>Tue, 13 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


@respx.mock
def test_rss():
    """R-SRC-7"""
    respx.get("https://feed.example/rss").respond(200, text=RSS)
    _, items = fetch("rss", {"url": "https://feed.example/rss"})
    assert len(items) == 1
    assert items[0].organizer == "TF News" and items[0].summary == "Workshops all week"
    assert items[0].published_at == datetime(2026, 10, 13, 8, tzinfo=UTC)


def test_malformed_record_is_skipped_not_fatal():
    """R-SRC-9"""
    src = REGISTRY["bevy"]("x", {"chapter_id": 1}, now=NOW)
    assert src._item({"title": "", "static_url": "https://x.y"}) is None
    assert src._item({"title": "ok", "static_url": "not-a-url"}) is None
    assert src.skipped == 2


# ------------------------------------------------------------------ test sources more

@respx.mock
def test_tribe_events_university():
    """R-SRC-10: The Events Calendar REST API (real EXUatar response)."""
    respx.get(url__startswith="https://www.university.example.edu/wp-json/tribe/events/v1/events").respond(
        200, text=fixture_text("tribe_exu.json"))
    src, items = fetch("tribe_events", {"base_url": "https://www.university.example.edu"})
    assert len(items) >= 10 and src.skipped == 0
    assert all(i.kind in (Kind.EVENT, Kind.HACKATHON) and i.starts_at and i.starts_at.tzinfo for i in items)
    math = next(i for i in items if "Math Circle" in i.title)
    assert "Example University" in math.location and "Admission" in math.tags
    assert all("&#" not in i.title for i in items)


@respx.mock
def test_tribe_paginates_and_all_day_events():
    page1 = {"events": [{"title": "AI &amp; Startups Day", "url": "https://s.example/e/1", "all_day": True,
                         "start_date": "2026-11-02 00:00:00", "end_date": "2026-11-03 23:59:59",
                         "venue": {"venue": "QNCC", "city": "Istanbul"}, "categories": [{"name": "Business"}],
                         "cost": "Free"}], "total_pages": 2}
    page2 = {"events": [{"title": "Online Hackathon", "url": "https://s.example/e/2", "is_virtual": True,
                         "utc_start_date": "2026-11-05 15:00:00", "utc_end_date": "2026-11-06 15:00:00",
                         "venue": []}], "total_pages": 2}
    route = respx.get(url__startswith="https://s.example/wp-json/tribe/events/v1/events")
    route.side_effect = [respx.MockResponse(200, json=page1), respx.MockResponse(200, json=page2)]
    _, items = fetch("tribe_events", {"base_url": "https://s.example"})
    assert route.call_count == 2
    day, hack = items
    assert day.title == "AI & Startups Day" and day.location == "QNCC, Istanbul" and "Cost: Free" in day.summary
    assert day.starts_at.isoformat() == "2026-11-01T21:00:00+00:00"  # midnight Istanbul
    assert day.ends_at.isoformat() == "2026-11-03T20:59:00+00:00"
    assert hack.kind == Kind.HACKATHON and hack.is_online and hack.location == "Online"
    assert hack.starts_at == datetime(2026, 11, 5, 15, tzinfo=UTC)


@respx.mock
def test_rss_event_feed_uses_pubdate_as_event_date():
    """R-SRC-7: Turkey Foundation's events.xml puts the event date in pubDate."""
    respx.get("https://www.foundation.example/events.xml").respond(200, text=fixture_text("tf_events.xml"))
    _, items = fetch("rss", {"url": "https://www.foundation.example/events.xml", "kind": "event",
                             "date_is_event_start": True, "default_location": "Tech District, Istanbul"})
    earthna = next(i for i in items if i.title == "Earthna Summit")
    assert earthna.kind == Kind.EVENT and earthna.starts_at.year == 2027 and earthna.published_at is None
    assert earthna.location == "Tech District, Istanbul"


@respx.mock
def test_startup_grind_istanbul_on_bevy():
    """R-SRC-2: Bevy adapter works for Startup Grind (different base_url, chapter 222)."""
    respx.get(url__startswith="https://www.startupgrind.com/api/event_slim/for_chapter/222/").respond(
        200, text=fixture_text("bevy_startupgrind_istanbul.json"))
    _, items = fetch("bevy", {"chapter_id": 222, "base_url": "https://www.startupgrind.com",
                              "default_location": "Istanbul, Turkey", "organizer": "Startup Grind Istanbul"})
    assert items and items[0].organizer == "Startup Grind Istanbul" and items[0].is_online is False
    assert any("Built to Scale" in i.title for i in items)


@respx.mock
def test_devpost_min_registrations_skips_tiny_online_hackathons():
    """R-SRC-4: online hackathons with fewer sign-ups than `min_registrations` are skipped."""
    respx.get(url__startswith="https://devpost.com/api/hackathons").side_effect = [
        httpx.Response(200, text=fixture_text("devpost_p1.json")), httpx.Response(200, json={"hackathons": []})]
    src, items = fetch("devpost", {"min_registrations": 1000})
    assert items and all(int(i.summary.split(" registered")[0].split("· ")[-1].replace(",", "")) >= 1000
                         for i in items if i.is_online)
    assert src.below_threshold > 0


# ------------------------------------------------------------------ real LinkedIn pages

def test_real_linkedin_company_page_all_posts_merged_and_deduped():
    """R-SRC-6: a real page had JSON-LD for 2 posts but feed cards for 10 (and reshares
    duplicating text). Strategies are merged by id and text, keeping exact dates."""
    posts = extract_posts(fixture_text("li_company_real.html"), NOW)
    assert len(posts) == 10
    assert len({p["text"][:60] for p in posts}) == 10
    assert all(p["published"] for p in posts)
    assert page_name(fixture_text("li_company_real.html"), "https://www.linkedin.com/company/microsoft/") == "Microsoft"


def test_real_linkedin_jobs_fragment():
    """R-SRC-5: the real guest jobs response parses fully."""
    from eventparser.sources.linkedin import parse_job_cards
    cards = parse_job_cards(fixture_text("li_jobs_real.html"))
    assert len(cards) == 10
    assert all(c["job_id"].isdigit() and c["title"] and c["url"].endswith(c["job_id"]) for c in cards)
    assert all(c["posted"] for c in cards)


@respx.mock
def test_linkedin_posts_retries_999_once_after_a_pause():
    """R-SRC-6: LinkedIn's 999 refusals are often momentary; refused pages get one later retry."""
    route = respx.get("https://www.linkedin.com/company/microsoft/")
    route.side_effect = [httpx.Response(999), httpx.Response(200, text=fixture_text("li_company_real.html"))]
    src, items = fetch("linkedin_posts", {"pages": ["https://www.linkedin.com/company/microsoft/"], "retry_pause": 0})
    assert route.call_count == 2 and len(items) == 10 and not src.warnings


@respx.mock
def test_linkedin_experience_filter_marks_entry_level():
    """R-SRC-5: with f_E (1/2/3) LinkedIn vouches for the level; jobs get the level:entry tag."""
    respx.get(url__startswith="https://www.linkedin.com/jobs-guest/").respond(200, text=fixture_text("li_jobs_real.html"))
    _, entry = fetch("linkedin_jobs", {"keywords": ["data analyst"], "filters": {"f_E": "2,3"}, "max_pages": 1})
    _, any_level = fetch("linkedin_jobs", {"keywords": ["data analyst"], "max_pages": 1})
    assert entry and all("level:entry" in i.tags for i in entry)
    assert all("level:entry" not in i.tags for i in any_level)


def test_page_name_decodes_double_escaped_entities():
    """LinkedIn showed 'Turkey Science &amp;amp; Technology Park' in og:title (seen live)."""
    html = '<meta property="og:title" content="Turkey Science &amp;amp; Technology Park (TSTP) | LinkedIn">'
    assert page_name(html, "https://x/") == "Turkey Science & Technology Park (TSTP)"
    assert page_name("", "https://www.linkedin.com/company/turkey-science-%26-technology-park/") == "Turkey Science & Technology Park"


# ------------------------------------------------------------------ job boards

@pytest.mark.parametrize("text, expected", [
    ("We offer visa sponsorship and relocation support.", True),
    ("Relocation package included for international hires.", True),
    ("Unfortunately we cannot offer visa sponsorship for this role.", False),
    ("No visa sponsorship available.", False),
    ("Great team, flexible hours.", False),
])
def test_offers_visa_respects_negation(text, expected):
    """R-SRC-11: 'no visa sponsorship' must not count as an offer."""
    from eventparser.sources.jobs import offers_visa
    assert offers_visa(text) is expected


@respx.mock
def test_arbeitnow_maps_jobs_and_tags_visa_offers():
    """R-SRC-11"""
    respx.get("https://www.arbeitnow.com/api/job-board-api").respond(200, json={
        "data": [
            {"title": "Junior Data Scientist (m/w/d)", "company_name": "ACME", "location": "Berlin", "remote": False,
             "url": "https://www.arbeitnow.com/jobs/acme/jds", "tags": ["Data"], "job_types": ["Full Time"],
             "created_at": 1792050000, "description": "<p>We sponsor your work visa and help you relocate.</p>"},
            {"title": "Senior Engineer", "company_name": "B", "location": "Munich", "remote": True,
             "url": "https://www.arbeitnow.com/jobs/b/se", "tags": [], "job_types": [], "created_at": 1792050000,
             "description": "No visa sponsorship."},
        ],
        "links": {"next": None}})
    _, items = fetch("arbeitnow", {"tags": ["international"]})
    jds, se = items
    assert jds.kind == Kind.JOB and "visa:sponsored" in jds.tags and "international" in jds.tags
    assert jds.location == "Berlin" and jds.organizer == "ACME" and jds.published_at.year == 2026
    assert "visa:sponsored" not in se.tags and se.is_online is True


@respx.mock
def test_remote_jobs_keep_only_roles_open_where_you_live():
    """R-SRC-12: 'USA only' is dropped, worldwide/EMEA kept; long country lists are shortened."""
    respx.get("https://remotive.com/api/remote-jobs").respond(200, json={"jobs": [
        {"title": "Junior Data Analyst", "url": "https://remotive.com/1", "company_name": "A",
         "candidate_required_location": "Worldwide", "publication_date": "2026-10-15T10:00:00", "description": "x"},
        {"title": "Data Analyst", "url": "https://remotive.com/2", "company_name": "B",
         "candidate_required_location": "USA", "publication_date": "2026-10-15T10:00:00", "description": "x"},
        {"title": "Data Scientist", "url": "https://remotive.com/3", "company_name": "C",
         "candidate_required_location": "Europe, EMEA", "publication_date": "2026-10-15T10:00:00", "description": "x"},
    ]})
    _, items = fetch("remote_jobs", {"provider": "remotive", "search": "data", "allowed_regions": ["worldwide", "emea"]})
    assert [i.organizer for i in items] == ["A", "C"]
    assert all(i.is_online and i.kind == Kind.JOB and "remote" in i.tags for i in items)
    from eventparser.sources.jobs import _short_region
    many = ", ".join(["Algeria", "Armenia", "Austria", "Bahrain", "Belgium", "Turkey", "Spain"])
    assert _short_region(many, ["turkey"]) == "7 countries incl. Turkey"


@respx.mock
def test_himalayas_entry_level_is_tagged():
    respx.get("https://himalayas.app/jobs/api").respond(200, json={"jobs": [
        {"title": "ML Engineer", "applicationLink": "https://himalayas.app/j/1", "companyName": "H",
         "seniority": ["Entry-level"], "locationRestrictions": [], "pubDate": 1792050000, "excerpt": "x"}]})
    _, (job,) = fetch("remote_jobs", {"provider": "himalayas", "search": "machine learning"})
    assert "level:entry" in job.tags and job.location == "Remote (worldwide)"


def test_source_tags_param_reaches_every_item():
    from eventparser.sources import REGISTRY
    src = REGISTRY["bevy"]("x", {"chapter_id": 1, "tags": ["international"]}, now=NOW)
    it = src._item({"title": "AI Night", "static_url": "https://x.y/1", "start_date": "2026-11-01T10:00:00Z"})
    assert "international" in it.tags


def test_eligibility_section_from_a_real_program_article():
    """R-SRC-7: the real RISE article states who may apply; the feed teaser didn't (seen live)."""
    from eventparser.sources.events import eligibility_section
    rules = eligibility_section(fixture_text("program_article_rise.html"), 900)
    assert "North America, Ireland or Britain" in rules and "undergraduate" in rules


@respx.mock
def test_rss_fetch_eligibility_puts_rules_first():
    feed = ('<?xml version="1.0"?><rss version="2.0"><channel><title>Programs</title><item>'
            '<title>RISE Summer Internship 2027 in Germany | Fully Funded</title>'
            '<link>https://www.opportunitiescircle.com/rise-2027/</link>'
            '<description>For students from different countries.</description>'
            '<pubDate>Wed, 14 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>')
    respx.get("https://feed.example/rss").respond(200, text=feed)
    respx.get("https://www.opportunitiescircle.com/rise-2027/").respond(200, text=fixture_text("program_article_rise.html"))
    _, (it,) = fetch("rss", {"url": "https://feed.example/rss", "kind": "program", "fetch_eligibility": True})
    assert it.summary.startswith("Eligibility: ") and "North America" in it.summary
    assert it.summary.endswith("For students from different countries.")


@respx.mock
def test_rss_eligibility_is_read_once_per_article():
    """R-SRC-7: articles already in the store are not opened again (the store keeps their text)."""
    from eventparser.models import uid_for
    feed = ('<?xml version="1.0"?><rss version="2.0"><channel><title>P</title><item>'
            '<title>WHO Internship 2027 (PAID)</title><link>https://opps.example/who/</link>'
            '<description>Paid.</description><pubDate>Wed, 14 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>')
    respx.get("https://feed.example/rss").respond(200, text=feed)
    article = respx.get("https://opps.example/who/").respond(200, text=fixture_text("program_article_rise.html"))
    src = REGISTRY["rss"]("p", {"url": "https://feed.example/rss", "kind": "program", "fetch_eligibility": True}, now=NOW)
    src.known = lambda uid: uid == uid_for("https://opps.example/who/")

    async def go():
        async with make_fetcher() as fx:
            return await src.fetch(fx)

    (it,) = run(go())
    assert article.call_count == 0 and it.summary == "Paid."
