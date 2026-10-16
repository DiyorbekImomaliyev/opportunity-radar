"""Regressions: bugs found by reviewing live output and failure paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import respx

from eventparser.config import redact
from eventparser.digest import MAX_MESSAGE, _pack
from eventparser.models import Kind, canonical_url
from eventparser.relevance import assess, has_word
from eventparser.sources.events import dedupe_parts, event_to_fields, ld_date
from eventparser.store import Store
from tests.conftest import NOW, TURKEY_TZ, item, make_fetcher, run


def test_redact_replaces_every_configured_secret(monkeypatch):
    """R-CFG-2"""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSECRET123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TGSECRET")
    assert redact("key=AIzaSECRET123 /bot123:TGSECRET/") == "key=*** /bot***/"


@respx.mock
def test_4xx_errors_keep_the_server_explanation():
    from eventparser.http import FetchError
    respx.post("https://api.telegram.org/botX/sendMessage").respond(
        400, json={"ok": False, "description": "Bad Request: can't parse entities"})
    try:
        run(make_fetcher().post_json("https://api.telegram.org/botX/sendMessage", {}))
    except FetchError as e:
        assert "can't parse entities" in str(e)
    else:
        raise AssertionError("expected FetchError")


def test_oversized_block_never_cut_through_html():
    """R-DIG-5"""
    huge = "<b>" + "x" * (MAX_MESSAGE + 500) + "</b>"
    (msg,) = _pack([huge])
    assert len(msg) <= MAX_MESSAGE and "<" not in msg


def test_prune_removes_stale_unsent_only():
    s = Store(":memory:")
    past = item(url="https://x.y/past", starts_at=NOW - timedelta(days=90))
    old_post = item(url="https://x.y/post", kind=Kind.POST, published_at=NOW - timedelta(days=90))
    future = item(url="https://x.y/future", starts_at=NOW + timedelta(days=10))
    sent_past = item(url="https://x.y/sent", starts_at=NOW - timedelta(days=90))
    for it in (past, old_post, future, sent_past):
        s.upsert(it, NOW)
    s.mark_sent([sent_past.uid], NOW)
    assert s.prune(NOW) == 2
    assert s.get(future.uid) and s.get(sent_past.uid)
    assert s.get(past.uid) is None and s.get(old_post.uid) is None
    # items not seen for 60 days are forgotten too
    assert s.prune(NOW + timedelta(days=61)) == 1


def test_address_parts_deduped():
    assert dedupe_parts("Istanbul, Istanbul, Istanbul, Istanbul Municipality, QA") == "Istanbul, Istanbul Municipality, QA"


# ------------------------------------------------------------------ test live review regressions

def test_eventbrite_domains_and_slugs_collapse():
    a = canonical_url("https://www.eventbrite.com/e/meet-vcs-across-the-globe-tickets-1987654321098?aff=ebdssbdestsearch")
    b = canonical_url("https://www.eventbrite.sg/e/meet-vcs-across-the-globe-in-person-tickets-1987654321098")
    assert a == b == "https://eventbrite.com/e/1987654321098"


def test_turkey_country_code_and_towns_are_home(profile):
    profile.locations += ["qa", "al wakra", "mesaieed"]
    ev = item(title="researchED Istanbul workshop", starts_at=NOW + timedelta(days=30), is_online=False,
              location="Mesaieed International School, Umm Said, Al Wakra Municipality, QA")
    assert assess(ev, profile, NOW).candidate


def test_jsonld_midnight_is_a_local_date():
    assert ld_date("2026-12-03T00:00:00.000+00:00", TURKEY_TZ) == datetime(2026, 12, 3, tzinfo=TURKEY_TZ)
    assert ld_date("2026-10-27T14:00:00.000Z", TURKEY_TZ) == datetime(2026, 10, 27, 14, tzinfo=UTC)
    f = event_to_fields({"@type": "Event", "name": "Conf", "url": "https://x.y",
                         "startDate": "2026-12-03T00:00:00.000+00:00", "endDate": "2026-12-04"},
                        tz=TURKEY_TZ, default_kind=Kind.EVENT, default_location=None)
    assert f["ends_at"] == datetime(2026, 12, 4, 23, 59, tzinfo=TURKEY_TZ)


def test_short_acronyms_must_be_capitalised():
    assert has_word("Build AI agents", "ai") and has_word("Meet VCs in Istanbul", "vc")
    assert not has_word("Ce film, j'ai adoré", "ai")
    assert not has_word("a vc of sorts", "vc")
    assert has_word("Machine learning", "machine learning")  # long phrases stay case-insensitive


def test_old_undated_program_pages_are_not_alerts(profile):
    old = item(kind=Kind.PROGRAM, title="AI Accelerator", published_at=NOW - timedelta(days=60), location="Istanbul")
    new = item(kind=Kind.PROGRAM, title="AI Accelerator", published_at=NOW - timedelta(days=3), location="Istanbul")
    assert "program page" in assess(old, profile, NOW).dropped
    assert assess(new, profile, NOW).candidate
