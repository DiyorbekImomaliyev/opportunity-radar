"""CLI, logging, concurrency bound and the once-a-day schedule guard (SPEC §4, §10, §11)."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
from datetime import UTC, datetime, time, timedelta

import respx

from eventparser.cli import setup_logging, should_run, single_instance
from eventparser.config import Profile, SourceConfig
from eventparser.pipeline import MAX_CONCURRENT_SOURCES, fetch_all, fetch_source
from eventparser.sources.base import Source, register
from eventparser.store import Store
from tests.conftest import NOW, make_fetcher, run
from tests.test_pipeline import Outbox, go, mock_sites


@register
class _SlowSource(Source):
    type = "_test_slow"
    active = 0
    peak = 0

    async def fetch(self, fx):
        cls = type(self)
        cls.active += 1
        cls.peak = max(cls.peak, cls.active)
        await asyncio.sleep(0.01)
        cls.active -= 1
        return []


def test_sources_run_concurrently_but_bounded():
    """R-NET-1"""
    cfgs = [SourceConfig(name=f"s{i}", type="_test_slow") for i in range(15)]
    results = run(fetch_all(cfgs, make_fetcher(), NOW))
    assert len(results) == 15 and all(r.ok for r in results)
    assert 1 < _SlowSource.peak <= MAX_CONCURRENT_SOURCES


def test_source_log_line_is_structured_json(tmp_path):
    """R-OBS-1"""
    setup_logging(tmp_path)
    with respx.mock:
        respx.get(url__regex=r".*for_chapter/9/.*").respond(500)
        run(fetch_source(SourceConfig(name="gdg-x", type="bevy", params={"chapter_id": 9}), make_fetcher(), NOW))
    logging.shutdown()
    lines = [json.loads(line) for line in (tmp_path / "radar.log").read_text().splitlines()]
    rec = next(r for r in lines if r.get("source") == "gdg-x")
    assert rec["ok"] is False and rec["items"] == 0 and "duration_ms" in rec and "500" in rec["error"]
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler))
    assert handler.maxBytes == 1_000_000 and handler.backupCount == 5


def test_profile_normalises_keywords():
    """R-REL-1"""
    p = Profile(interests={" Machine Learning ": 3}, locations=["Istanbul "], exclude=["SOLD OUT"])
    assert p.interests == {"machine learning": 3.0} and p.locations == ["istanbul"] and p.exclude == ["sold out"]


@respx.mock
def test_below_min_score_is_stored_not_sent(profile, tmp_path):
    """R-REL-5"""
    mock_sites()
    store = Store(":memory:")
    result = go(store, profile, tmp_path, deliver=Outbox())
    low = store.db.execute(
        "SELECT uid, score FROM items WHERE score IS NOT NULL AND score < ? AND sent_at IS NULL", (profile.min_score,)
    ).fetchall()
    assert low, "fixtures should contain irrelevant items (e.g. game nights)"
    assert not {r["uid"] for r in low} & set(result.digest.included)


# ------------------------------------------------------------------ test schedule

# 2026-10-15 06:00 UTC = 09:00 Istanbul
MORNING = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


def store_with_delivery(at: datetime, delivered: bool, scheduled: bool = True) -> Store:
    s = Store(":memory:")
    rid = s.start_run(at)
    s.finish_run(rid, at, "ok", {"delivered": delivered, "scheduled": scheduled})
    return s


def test_runs_when_due():
    """R-SCHED-1"""
    assert should_run(Store(":memory:"), MORNING, time(7), lambda: True) == (True, "due")


def test_not_before_uses_istanbul_time():
    early = datetime(2026, 10, 15, 3, 30, tzinfo=UTC)  # 06:30 Istanbul
    ok, why = should_run(Store(":memory:"), early, time(7), lambda: True)
    assert not ok and "before 07:00" in why


def test_only_once_per_istanbul_day():
    s = store_with_delivery(MORNING, delivered=True)
    ok, why = should_run(s, MORNING + timedelta(hours=5), time(7), lambda: True)
    assert not ok and "already delivered today" in why
    # 21:30 UTC is already the next day in Istanbul (00:30) → due again
    assert should_run(s, datetime(2026, 10, 15, 21, 30, tzinfo=UTC), time(0), lambda: True)[0]


def test_manual_run_does_not_cancel_the_daily_digest():
    """R-SCHED-1: a manual `radar run` at 00:19 must not stop the 07:00 scheduled digest."""
    s = store_with_delivery(datetime(2026, 10, 15, 21, 19, tzinfo=UTC), delivered=True, scheduled=False)
    assert should_run(s, MORNING + timedelta(hours=1), time(7), lambda: True)[0]


def test_failed_delivery_does_not_count():
    s = store_with_delivery(MORNING, delivered=False)
    assert should_run(s, MORNING + timedelta(minutes=30), time(7), lambda: True)[0]


def test_offline_waits_for_next_check():
    ok, why = should_run(Store(":memory:"), MORNING, time(7), lambda: False)
    assert not ok and "offline" in why


def test_single_instance_lock(tmp_path):
    """R-SCHED-2: overlapping launches never run twice."""
    lock = tmp_path / "run.lock"
    with single_instance(lock) as first:
        assert first
        import subprocess
        import sys
        code = ("import sys; from pathlib import Path; from eventparser.cli import single_instance\n"
                f"with single_instance(Path({str(lock)!r})) as got: sys.exit(0 if not got else 1)")
        assert subprocess.run([sys.executable, "-c", code]).returncode == 0  # second process is refused
    with single_instance(lock) as again:
        assert again  # released afterwards
