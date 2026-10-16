"""End-to-end runs with mocked HTTP (SPEC §5, §9, §10, §11)."""

from datetime import timedelta

import httpx
import respx

from eventparser.config import SourceConfig
from eventparser.llm import Refiner
from eventparser.pipeline import run as run_pipeline
from eventparser.store import Store
from eventparser.telegram import DeliveryError
from tests.conftest import NOW, fixture_text, make_fetcher, run
from tests.test_llm_config import GEMINI, gemini_answer

SOURCES = [
    SourceConfig(name="gdg-istanbul", type="bevy", params={"chapter_id": 657, "default_location": "Istanbul, Turkey"}),
    SourceConfig(name="meetup-istanbul", type="jsonld", params={"urls": ["https://www.meetup.com/find/"],
                                                            "default_location": "Istanbul, Turkey"}),
    SourceConfig(name="devpost", type="devpost", params={"max_pages": 2}),
    SourceConfig(name="broken", type="jsonld", params={"urls": ["https://broken.example/"]}),
    SourceConfig(name="off", type="bevy", enabled=False, params={"chapter_id": 1}),
]


def mock_sites():
    respx.get(url__regex=r"https://gdg\.community\.dev/api/event_slim/for_chapter/657/").respond(
        200, text=fixture_text("bevy_gdg_istanbul.json"))
    respx.get("https://www.meetup.com/find/").respond(200, text=fixture_text("meetup_istanbul_ai.html"))
    respx.get(url__startswith="https://devpost.com/api/hackathons").side_effect = lambda req: (
        httpx.Response(200, text=fixture_text("devpost_p1.json")) if "page=1" in str(req.url)
        else httpx.Response(200, json={"hackathons": []}))
    respx.get("https://broken.example/").respond(500)
    return respx.get(url__regex=r".*for_chapter/1/.*").respond(200, json={"results": []})


class Outbox:
    """A fake Telegram: records the messages it was asked to deliver."""

    def __init__(self, fail=False):
        self.sent: list[list[str]] = []
        self.fail = fail

    async def __call__(self, fx, digest):
        if self.fail:
            raise DeliveryError("telegram down")
        self.sent.append(digest.messages)


def go(store, profile, tmp_path, **kw):
    kw.setdefault("deliver", Outbox())
    return run(run_pipeline(sources=SOURCES, profile=profile, store=store, now=kw.pop("now", NOW),
                            fetcher=make_fetcher(), data_dir=tmp_path, use_llm=kw.pop("use_llm", False), **kw))


@respx.mock
def test_full_run_isolates_failures_and_delivers(profile, tmp_path):
    """R-NET-4, R-DIG-1, R-DIG-4, R-OBS-3"""
    disabled_route = mock_sites()
    store, outbox = Store(":memory:"), Outbox()
    result = go(store, profile, tmp_path, deliver=outbox)

    by_name = {s.name: s for s in result.sources}
    assert set(by_name) == {"gdg-istanbul", "meetup-istanbul", "devpost", "broken"}  # disabled source skipped
    assert not disabled_route.called
    assert not by_name["broken"].ok and "500" in by_name["broken"].error
    assert all(by_name[n].ok and by_name[n].items for n in ("gdg-istanbul", "meetup-istanbul", "devpost"))

    assert result.delivered and result.exit_code == 0 and len(outbox.sent) == 1
    text = "\n".join(outbox.sent[0])
    assert "Deploy a RAG AI Agent" in text
    assert text.count("Deploy a RAG AI Agent") == 1  # GDG + Meetup copies deduped
    assert "Sources: 3/4 OK" in text and "broken" in text
    assert (tmp_path / "last_digest.html").exists()

    # Everything included is now marked sent → the next run sends nothing new.
    assert result.digest.included and all(store.get(u).sent_at for u in result.digest.included)
    # Next run: nothing already sent comes again (held-back items may now get their turn).
    second = go(store, profile, tmp_path, deliver=Outbox(), now=NOW + timedelta(hours=1))
    assert not set(second.digest.included) & set(result.digest.included)
    profile.max_online = 100
    go(store, profile, tmp_path, deliver=(out3 := Outbox()), now=NOW + timedelta(hours=2))
    rest = go(store, profile, tmp_path, deliver=(out4 := Outbox()), now=NOW + timedelta(hours=3))
    assert rest.digest.item_count == 0 and "No new opportunities today" in out4.sent[0][0]
    assert out3.sent


@respx.mock
def test_dedup_keeps_higher_priority_source(profile, tmp_path):
    """R-DEDUP-2: GDG is listed before Meetup, so the GDG link is the one sent."""
    mock_sites()
    store = Store(":memory:")
    result = go(store, profile, tmp_path)
    rag = [store.get(u).item for u in result.digest.included if "Deploy a RAG" in store.get(u).item.title]
    assert len(rag) == 1 and rag[0].source == "gdg-istanbul"


@respx.mock
def test_dry_run_marks_nothing(profile, tmp_path, capsys):
    """R-DIG-1, R-OUT-2"""
    mock_sites()
    store, outbox = Store(":memory:"), Outbox()
    result = go(store, profile, tmp_path, dry_run=True, deliver=outbox)
    assert not result.delivered and outbox.sent == []
    assert result.digest.item_count > 0 and all(store.get(u).sent_at is None for u in result.digest.included)
    assert "Opportunity Radar" in capsys.readouterr().out


@respx.mock
def test_no_telegram_config_means_dry_run(profile, tmp_path):
    """R-OUT-2"""
    mock_sites()
    result = run(run_pipeline(sources=SOURCES, profile=profile, store=Store(":memory:"), now=NOW,
                              fetcher=make_fetcher(), data_dir=tmp_path, use_llm=False))
    assert not result.delivered and result.exit_code == 0


@respx.mock
def test_delivery_failure_keeps_items_unsent_and_exits_2(profile, tmp_path):
    """R-OBS-3"""
    mock_sites()
    store = Store(":memory:")
    result = go(store, profile, tmp_path, deliver=Outbox(fail=True))
    assert result.exit_code == 2 and result.delivery_error
    assert all(store.get(u).sent_at is None for u in result.digest.included)


@respx.mock
def test_partial_delivery_marks_only_cards_that_arrived(profile, tmp_path):
    """R-OUT-1: Telegram failed after the first card → that card is sent, the rest stay for the next run."""
    mock_sites()
    store = Store(":memory:")

    async def half(fx, digest):
        raise DeliveryError("message 3/9: telegram down", delivered=digest.included[:1])

    result = go(store, profile, tmp_path, deliver=half)
    first, *rest = result.digest.included
    assert result.exit_code == 2 and rest
    assert store.get(first).sent_at is not None and all(store.get(u).sent_at is None for u in rest)


@respx.mock
def test_all_sources_failing_exits_2_but_still_reports(profile, tmp_path):
    """R-OBS-3, R-DIG-6"""
    respx.route().respond(500)
    outbox = Outbox()
    result = go(Store(":memory:"), profile, tmp_path, deliver=outbox)
    assert result.exit_code == 2
    assert "Sources: 0/4 OK" in outbox.sent[0][0]


@respx.mock
def test_llm_scores_and_summaries_used_once(profile, tmp_path):
    """R-REL-4: LLM verdicts replace rule scores; judged items aren't re-sent to the LLM."""
    mock_sites()
    store = Store(":memory:")
    route = respx.post(GEMINI.format("gemini-2.5-flash")).mock(side_effect=lambda r: gemini_answer(r, score=8))

    def refiner():
        return Refiner(profile, api_key="k", models=["gemini-2.5-flash"])

    result = go(store, profile, tmp_path, dry_run=True, use_llm=True, refiner=refiner())
    assert route.called and not result.llm_failures
    text = "\n".join(result.digest.messages)
    assert "Gemini: Tech Talk: Deploy a RAG AI Agent" in text and "AI rated 8/10" in text
    assert "🤖 AI picks: gemini-2.5-flash" in text
    first_calls = route.call_count
    go(store, profile, tmp_path, dry_run=True, use_llm=True, refiner=refiner(), now=NOW + timedelta(hours=1))
    assert route.call_count == first_calls  # nothing new to judge


@respx.mock
def test_llm_outage_still_delivers_rule_picks_with_note(profile, tmp_path):
    """R-REL-4 + R-DIG-4: AI down → same digest from rules, and the footer says so."""
    mock_sites()
    respx.post(url__startswith="https://generativelanguage.googleapis.com/").respond(503)
    outbox = Outbox()
    ref = Refiner(profile, api_key="k", models=["gemini-2.5-flash"])
    result = go(Store(":memory:"), profile, tmp_path, deliver=outbox, use_llm=True, refiner=ref)
    text = "\n".join(outbox.sent[0])
    assert result.delivered and result.digest.item_count > 0
    assert "AI scoring unavailable today" in text and "rule-based picks" in text


@respx.mock
def test_health_counts_consecutive_failures(profile, tmp_path):
    """R-DIG-4: a source failing 3 runs in a row is flagged in the digest."""
    mock_sites()
    store = Store(":memory:")
    for h in range(3):
        result = go(store, profile, tmp_path, now=NOW + timedelta(hours=h))
    assert "broken" in result.digest.messages[-1] and "failing 3 runs" in result.digest.messages[-1]


@respx.mock
def test_online_items_need_min_online_score(profile, tmp_path):
    profile.notable_organizers = ["amazon"]
    """R-REL-5: at 7/10 an online item is held back (bar 8), unless a notable organiser runs it."""
    from eventparser.relevance import notable

    mock_sites()
    respx.post(GEMINI.format("m")).mock(side_effect=lambda r: gemini_answer(r, score=7))
    store = Store(":memory:")
    result = go(store, profile, tmp_path, use_llm=True, refiner=Refiner(profile, api_key="k", models=["m"]))
    online = [store.get(u).item for u in result.digest.included if store.get(u).item.is_online]
    profile.notable_organizers = ["amazon"]
    assert online and all(notable(i, profile) for i in online)  # e.g. Amazon's hackathon at 7 passes
    assert result.digest.item_count > len(online)  # in-person items at 7 pass min_ai_score 6


@respx.mock
def test_profile_change_triggers_rejudging(profile, tmp_path):
    """R-REL-4: verdicts are reused until the AI instructions or the profile change."""
    mock_sites()
    route = respx.post(GEMINI.format("m")).mock(side_effect=lambda r: gemini_answer(r, score=3))
    store = Store(":memory:")

    def again(hours):
        return go(store, profile, tmp_path, dry_run=True, use_llm=True, now=NOW + timedelta(hours=hours),
                  refiner=Refiner(profile, api_key="k", models=["m"]))

    again(0)
    calls = route.call_count
    again(1)
    assert route.call_count == calls  # same profile → cached verdicts
    profile.about = "Now I care about fintech too."
    again(2)
    assert route.call_count > calls  # changed profile → re-judged


@respx.mock
def test_exploration_adds_one_near_miss(profile, tmp_path):
    """R-BOT-7: one item just under the threshold is shown as 'something different'."""
    mock_sites()
    profile.explore = 1
    respx.post(GEMINI.format("m")).mock(side_effect=lambda r: gemini_answer(r, score=5))  # min_ai_score 6
    store = Store(":memory:")
    result = go(store, profile, tmp_path, use_llm=True, refiner=Refiner(profile, api_key="k", models=["m"]))
    text = "\n".join(result.digest.messages)
    assert result.digest.item_count == 1 and "🎲 Something different" in text


def test_sources_the_owner_keeps_rejecting_lose_a_point():
    """R-BOT-7: ≥5 marks with ≥70 % rejections on a source → its items score one point lower."""
    from eventparser.digest import Entry
    from eventparser.pipeline import apply_source_penalty
    from tests.conftest import item
    skip = Entry("a", item(source="meetup"), 7.0)
    keep = Entry("b", item(source="gdg", url="https://x.y/b"), 7.0)
    few = Entry("c", item(source="new", url="https://x.y/c"), 7.0)
    apply_source_penalty([skip, keep, few], {"meetup": (4, 5), "gdg": (1, 6), "new": (3, 3)})
    assert (skip.score, keep.score, few.score) == (6.0, 7.0, 7.0)
    assert "you often skip meetup" in skip.reasons
