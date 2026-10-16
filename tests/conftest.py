from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from eventparser.config import Profile
from eventparser.http import Fetcher
from eventparser.models import Item, Kind
from eventparser.textutil import set_local_tz

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_EXAMPLES = Path(__file__).parents[1] / "config"

# The captured fixtures come from sites in Istanbul, so tests run in that timezone.
TURKEY_TZ = ZoneInfo("Europe/Istanbul")

# Fixtures were captured on 2026-10-15; tests run "as of" that morning.
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_seconds: float) -> None:
    return None


def make_fetcher(**kw) -> Fetcher:
    """A Fetcher that never really sleeps (fast tests)."""
    kw.setdefault("sleep", _no_sleep)
    kw.setdefault("host_delay", 0)
    kw.setdefault("host_delays", {"linkedin.com": 0})
    return Fetcher(**kw)


def item(**kw) -> Item:
    defaults = dict(source="test", kind=Kind.EVENT, title="AI Workshop", url="https://example.com/e/1")
    defaults.update(kw)
    return Item(**defaults)


@pytest.fixture
def profile() -> Profile:
    return Profile(
        about="Istanbul-based, into AI and investment.",
        timezone="Europe/Istanbul",
        home_label="🏠 Near you",
        explore=0,  # tested on its own; other tests count exact digest contents
        interests={"ai": 3, "hackathon": 3, "internship": 3, "intern": 3, "investment": 3, "startup": 2},
        formats={"workshop": 2, "conference": 1},
        locations=["turkey", "istanbul", "kadikoy", "tstp"],
        exclude=["sold out"],
        min_score=3,
    )


@pytest.fixture(autouse=True)
def _local_tz():
    set_local_tz("Europe/Istanbul")
    yield
    set_local_tz("UTC")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    """Tests never read or write the owner's private config: they get the public examples."""
    import shutil

    from eventparser import config

    real = config.CONFIG_DIR
    d = tmp_path_factory.mktemp("config")
    for name in ("profile", "sources"):
        shutil.copy(real / f"{name}.example.yaml", d / f"{name}.yaml")
        shutil.copy(real / f"{name}.example.yaml", d / f"{name}.example.yaml")
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(config, "OVERRIDES_PATH", d / "overrides.yaml")


@pytest.fixture(autouse=True)
def no_real_bot_restart(monkeypatch):
    """Tests never restart the owner's running bot; they record the request instead."""
    from eventparser import updates

    calls: list[bool] = []
    monkeypatch.setattr(updates, "restart_bot", lambda: calls.append(True))
    return calls
