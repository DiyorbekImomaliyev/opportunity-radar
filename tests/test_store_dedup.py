"""Storage (SPEC §6) and deduplication (SPEC §7)."""

from datetime import UTC, datetime, timedelta

from eventparser.models import Kind
from eventparser.relevance import find_duplicates, is_near_duplicate, similarity
from eventparser.store import SCHEMA_VERSION, Store
from tests.conftest import NOW, item

DAY = datetime(2026, 10, 25, 15, tzinfo=UTC)


def test_schema_version_set():
    """R-DB-4"""
    s = Store(":memory:")
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_upsert_preserves_first_seen_and_sent_at():
    """R-DB-1, R-DB-2"""
    s = Store(":memory:")
    it = item(title="AI Workshop", summary="v1")
    assert s.upsert(it, NOW) is True
    s.mark_sent([it.uid], NOW)
    later = NOW + timedelta(days=1)
    assert s.upsert(item(title="AI Workshop", summary="v2 updated"), later) is False
    got = s.get(it.uid)
    assert got.first_seen == NOW and got.sent_at == NOW and got.item.summary == "v2 updated"
    row = s.db.execute("SELECT last_seen FROM items").fetchone()
    assert row[0] == later.isoformat()


def test_roundtrip_all_fields():
    s = Store(":memory:")
    it = item(kind=Kind.HACKATHON, starts_at=DAY, ends_at=DAY + timedelta(days=2), location="Istanbul",
              is_online=False, organizer="GDG", tags=["AI"], published_at=NOW)
    s.upsert(it, NOW)
    assert s.get(it.uid).item == it


def test_unsent_excludes_sent_and_duplicates():
    s = Store(":memory:")
    a, b, c = item(url="https://x.y/a"), item(url="https://x.y/b"), item(url="https://x.y/c")
    for i in (a, b, c):
        s.upsert(i, NOW)
    s.mark_sent([a.uid], NOW)
    s.mark_duplicate(b.uid, c.uid)
    assert [x.uid for x in s.unsent()] == [c.uid]


def test_source_health_tracks_failures_and_silence():
    """R-DB-3, R-OBS-2"""
    s = Store(":memory:")
    for i, (ok, n) in enumerate([(True, 5), (True, 4), (False, 0), (False, 0), (False, 0)]):
        rid = s.start_run(NOW + timedelta(days=i))
        s.record_source(rid, "src", ok, n, None if ok else "boom", 10)
    h = s.source_health("src")
    assert h.consecutive_failures == 3 and not h.last_ok and h.last_error == "boom"
    assert h.last_success == NOW + timedelta(days=1)

    rid = s.start_run(NOW + timedelta(days=9))
    s.record_source(rid, "src", True, 0, None, 10)
    h = s.source_health("src")
    assert h.went_silent and h.consecutive_failures == 0
    assert s.source_health("never") is None


def test_similarity():
    assert similarity("Tech Talk: Deploy a RAG AI Agent in Python", "Tech Talk - Deploy a RAG AI agent in Python!") == 1.0
    assert similarity("AI Workshop", "Data Workshop") == 0.0  # short titles must match exactly
    assert similarity("Google Cloud Summit Istanbul 2026", "Google Cloud Summit Istanbul 2026 | Register") >= 0.85
    assert similarity("AI Tinkerers Istanbul Session 3", "AI Tinkerers Istanbul Session 4") < 0.85


def test_near_duplicate_requires_same_day_and_kind():
    """R-DEDUP-2"""
    a = item(source="gdg", title="Deploy a RAG AI Agent with ADK", starts_at=DAY)
    b = item(source="meetup", url="https://meetup.com/x", title="Deploy a RAG AI Agent with ADK", starts_at=DAY + timedelta(hours=1))
    assert is_near_duplicate(a, b)
    assert not is_near_duplicate(a, b.model_copy(update={"starts_at": DAY + timedelta(days=1)}))
    assert not is_near_duplicate(a, b.model_copy(update={"kind": Kind.HACKATHON}))
    assert not is_near_duplicate(a, b.model_copy(update={"starts_at": None}))


def test_find_duplicates_keeps_higher_priority_source():
    """R-DEDUP-2: the source listed first in sources.yaml wins."""
    s = Store(":memory:")
    gdg = item(source="gdg", url="https://gdg.dev/e/1", title="Deploy a RAG AI Agent with ADK", starts_at=DAY)
    meetup = item(source="meetup", url="https://meetup.com/e/1", title="Deploy a RAG AI Agent with ADK", starts_at=DAY)
    s.upsert(meetup, NOW)
    s.upsert(gdg, NOW)
    pairs = find_duplicates({gdg.uid, meetup.uid}, s.active_items(), {"gdg": 0, "meetup": 5})
    assert pairs == [(meetup.uid, gdg.uid)]


def test_find_duplicates_never_retracts_sent_item():
    s = Store(":memory:")
    old = item(source="meetup", url="https://meetup.com/e/1", title="Deploy a RAG AI Agent with ADK", starts_at=DAY)
    s.upsert(old, NOW)
    s.mark_sent([old.uid], NOW)
    new = item(source="gdg", url="https://gdg.dev/e/1", title="Deploy a RAG AI Agent with ADK", starts_at=DAY)
    s.upsert(new, NOW)
    pairs = find_duplicates({new.uid}, s.active_items(), {"gdg": 0, "meetup": 5})
    assert pairs == [(new.uid, old.uid)]  # the new copy is the duplicate; nothing sent twice


def test_same_source_items_are_not_deduped():
    s = Store(":memory:")
    a = item(source="eventbrite", url="https://e.com/1", title="AI Night Istanbul Networking", starts_at=DAY)
    b = item(source="eventbrite", url="https://e.com/2", title="AI Night Istanbul Networking", starts_at=DAY)
    s.upsert(a, NOW)
    s.upsert(b, NOW)
    assert find_duplicates({a.uid, b.uid}, s.active_items(), {}) == []


def test_migrates_v1_database_without_losing_items(tmp_path):
    """R-DB-4: an existing v1 database gains the ai_version column and keeps its rows."""
    import sqlite3

    path = tmp_path / "old.db"
    s = Store(path)
    it = item(title="AI Workshop")
    s.upsert(it, NOW)
    s.commit()
    s.db.execute("ALTER TABLE items DROP COLUMN ai_version")
    s.db.execute("PRAGMA user_version = 1")
    s.db.commit()
    s.close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 1

    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    tables = {r[0] for r in s.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"marks", "kv"} <= tables  # v3 tables added on the way
    s.set_score(it.uid, 8, ["ai"], "about", "v-abc")
    got = s.get(it.uid)
    assert got.item.title == "AI Workshop" and got.ai_version == "v-abc"


def test_same_start_time_and_similar_title_is_one_event():
    """R-DEDUP-2: 'Session 4' on the venue's site and 'Round 4' on the organiser's, both 18:00."""
    at = datetime(2026, 10, 19, 15, tzinfo=UTC)
    a = item(source="tstp", title="AI Tinkerers Istanbul – Session 4", starts_at=at)
    b = item(source="aitinkerers", url="https://x.y/r4", title="AI Tinkerers Istanbul - Round 4", starts_at=at)
    assert is_near_duplicate(a, b)
    later = b.model_copy(update={"starts_at": at + timedelta(hours=2)})
    assert not is_near_duplicate(a, later)  # different time → needs the stricter title match


def test_upsert_keeps_details_read_once():
    """R-SRC-5: visa tags and the eligibility summary read from a job's page survive later
    refreshes from the search card, which lack them."""
    store = Store(":memory:")
    card = item(url="https://www.linkedin.com/jobs/view/1", kind=Kind.JOB, title="Data Intern", summary="Acme · Dubai")
    detailed = card.model_copy(update={"summary": "Eligibility: UAE nationals only. — Acme · Dubai",
                                       "tags": ["visa:restricted"]})
    store.upsert(detailed, NOW)
    store.upsert(card, NOW)
    s = store.get(card.uid)
    assert s.item.summary.startswith("Eligibility: UAE nationals only.") and "visa:restricted" in s.item.tags
