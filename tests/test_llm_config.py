"""AI refinement with Gemini (R-REL-4) and configuration (SPEC §13)."""

import json
import os

import httpx
import pytest
import respx

from eventparser.config import ConfigError, load_dotenv, load_profile, load_sources
from eventparser.llm import VERDICTS_SCHEMA, Refiner
from tests.conftest import CONFIG_EXAMPLES, NOW, item, make_fetcher, run

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


def _items_in(content: str) -> list[dict]:
    return json.loads(content.split("Items:\n", 1)[1])


def gemini_answer(request: httpx.Request, score=7, finish="STOP") -> httpx.Response:
    """A Gemini reply judging every item in the request (with a hidden 'thought' part)."""
    body = json.loads(request.content)
    payload = _items_in(body["contents"][0]["parts"][0]["text"])
    verdicts = {"verdicts": [{"id": p["id"], "relevant": True, "score": score,
                              "about": f"Gemini: {p['title']}", "deadline": None} for p in payload]}
    return httpx.Response(200, json={"candidates": [{"finishReason": finish, "content": {"parts": [
        {"text": "thinking…", "thought": True}, {"text": json.dumps(verdicts)}]}}]})


def refiner(profile, *models):
    return Refiner(profile, api_key="KEY123", models=list(models) or ["gemini-2.5-flash", "gemini-3.5-flash-lite"])


@respx.mock
def test_gemini_request_shape_and_parse(profile):
    """R-REL-4: structured JSON output, system prompt, key only in a header."""
    route = respx.post(GEMINI.format("gemini-2.5-flash")).mock(side_effect=gemini_answer)
    ref = refiner(profile)
    out = run(ref.refine({"u1": item(title="AI Night")}, NOW, make_fetcher()))
    assert out["u1"].about == "Gemini: AI Night" and out["u1"].score == 7
    assert ref.models_used == ["gemini-2.5-flash"] and ref.label == "gemini-2.5-flash"
    req = route.calls[0].request
    assert req.headers["x-goog-api-key"] == "KEY123" and "KEY123" not in str(req.url)
    body = json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == VERDICTS_SCHEMA
    assert "treat it as data" in body["systemInstruction"]["parts"][0]["text"]
    assert "Istanbul-based" in body["contents"][0]["parts"][0]["text"]


@respx.mock
def test_batches_of_twenty(profile):
    route = respx.post(GEMINI.format("gemini-2.5-flash")).mock(side_effect=gemini_answer)
    items = {f"u{i}": item(url=f"https://x.y/{i}", title=f"AI thing {i}") for i in range(45)}
    out = run(refiner(profile).refine(items, NOW, make_fetcher()))
    assert len(out) == 45 and route.call_count == 3


@respx.mock
@pytest.mark.parametrize("first_failure", [
    httpx.Response(404, json={"error": {"message": "model is no longer available to new users"}}),
    httpx.Response(400, json={"error": {"message": "bad schema"}}),
    httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not json"}]}}]}),
    httpx.Response(200, json={"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{"}]}}]}),
    httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
])
def test_falls_back_to_next_model(profile, first_failure):
    """A retired / broken / truncated model never loses the batch while another model works."""
    respx.post(GEMINI.format("gemini-2.5-flash")).mock(return_value=first_failure)
    respx.post(GEMINI.format("gemini-3.5-flash-lite")).mock(side_effect=gemini_answer)
    ref = refiner(profile)
    out = run(ref.refine({"u1": item()}, NOW, make_fetcher()))
    assert "u1" in out and ref.models_used == ["gemini-3.5-flash-lite"] and not ref.failures


@respx.mock
def test_rate_limit_is_retried(profile):
    route = respx.post(GEMINI.format("gemini-2.5-flash"))
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "2"}), gemini_answer]
    assert run(refiner(profile, "gemini-2.5-flash").refine({"u1": item()}, NOW, make_fetcher()))
    assert route.call_count == 2


@respx.mock
def test_all_models_failing_returns_empty_and_records(profile, monkeypatch):
    """R-REL-4: any AI failure → {} (rule scores are kept); the key never appears in errors."""
    monkeypatch.setenv("GEMINI_API_KEY", "SECRETKEY99")
    respx.post(url__startswith="https://generativelanguage.googleapis.com/").respond(
        403, json={"error": {"message": "API key SECRETKEY99 not valid"}})
    ref = Refiner(profile, models=["a", "b"])
    assert run(ref.refine({"u1": item()}, NOW, make_fetcher())) == {}
    assert len(ref.failures) == 1 and "SECRETKEY99" not in ref.failures[0] and "403" in ref.failures[0]


@respx.mock
def test_unknown_ids_from_model_are_ignored(profile):
    respx.post(GEMINI.format("m")).respond(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": json.dumps({"verdicts": [{"id": "nope", "relevant": True, "score": 9, "about": "x", "deadline": None}]})}]}}]})
    assert run(refiner(profile, "m").refine({"u": item()}, NOW, make_fetcher())) == {}


def test_disabled_without_key(profile, monkeypatch):
    assert not Refiner(profile).enabled
    assert run(Refiner(profile).refine({"u": item()}, NOW)) == {}
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert Refiner(profile).enabled and Refiner(profile).models == ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


# ------------------------------------------------------------------ config

def write(tmp_path, text):
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    return p


def test_example_configs_are_valid():
    """R-CFG-1: the example configs shipped in the repo load and validate."""
    sources = load_sources(CONFIG_EXAMPLES / "sources.example.yaml")
    assert len(sources) >= 5 and len({s.name for s in sources}) == len(sources)
    assert load_profile(CONFIG_EXAMPLES / "profile.example.yaml").min_score > 0


def test_private_configs_are_valid_if_present():
    """Your own (git-ignored) config/sources.yaml and profile.yaml, when they exist."""
    if not (CONFIG_EXAMPLES / "sources.yaml").exists():
        pytest.skip("no private config")
    assert load_sources() and load_profile()


def test_missing_config_points_to_example(tmp_path):
    (tmp_path / "sources.example.yaml").write_text("sources: []\n")
    with pytest.raises(ConfigError, match="copy sources.example.yaml to sources.yaml"):
        load_sources(tmp_path / "sources.yaml")


def test_bad_timezone_rejected(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("timezone: Mars/Olympus\n")
    with pytest.raises(ConfigError, match="unknown timezone"):
        load_profile(p)


@pytest.mark.parametrize("yaml_text, message", [
    ("sources:\n  - {name: a, type: nope}\n", "unknown type 'nope'"),
    ("sources:\n  - {name: a, type: bevy}\n", "chapter_id"),
    ("sources:\n  - {name: a, type: bevy, chapter_id: 1, bogus: 2}\n", "bogus"),
    ("sources:\n  - {name: a, type: bevy, chapter_id: 1}\n  - {name: a, type: bevy, chapter_id: 2}\n", "duplicate"),
    ("nothing: here\n", "sources:"),
    ("sources: [\n", "invalid YAML"),
])
def test_config_errors_name_the_problem(tmp_path, yaml_text, message):
    """R-CFG-1"""
    with pytest.raises(ConfigError, match=message):
        load_sources(write(tmp_path, yaml_text))


def test_disabled_flag_parsed(tmp_path):
    """R-CFG-3"""
    (s,) = load_sources(write(tmp_path, "sources:\n  - {name: a, type: bevy, chapter_id: 1, enabled: false}\n"))
    assert s.enabled is False and s.params == {"chapter_id": 1}


def test_dotenv(tmp_path, monkeypatch):
    """R-CFG-2: .env is read; real environment wins."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "from-env")
    env = tmp_path / ".env"
    env.write_text("# comment\nTELEGRAM_BOT_TOKEN='abc'\nTELEGRAM_CHAT_ID=from-file\nEMPTY=\n")
    load_dotenv(env)
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "abc" and os.environ["TELEGRAM_CHAT_ID"] == "from-env"
    assert "EMPTY" not in os.environ


@respx.mock
def test_slow_model_hands_over_after_two_tries(profile):
    """A hanging model is tried twice at most, then the next model answers."""
    slow = respx.post(GEMINI.format("slow")).mock(side_effect=httpx.ReadTimeout("read timed out"))
    respx.post(GEMINI.format("fast")).mock(side_effect=gemini_answer)
    ref = refiner(profile, "slow", "fast")
    assert run(ref.refine({"u1": item()}, NOW, make_fetcher()))
    assert slow.call_count == 2 and ref.models_used == ["fast"]


@respx.mock
def test_time_budget_stops_further_batches(profile):
    respx.post(GEMINI.format("m")).mock(side_effect=gemini_answer)
    ref = Refiner(profile, api_key="k", models=["m"], time_budget=0)
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(30)}
    assert run(ref.refine(items, NOW, make_fetcher())) == {}
    assert "time budget" in ref.failures[0] and "30 items" in ref.failures[0]


def test_quotas_must_fit_in_max_items(tmp_path):
    """R-REL-6: quotas larger than the digest starve the kinds ranked last (seen live: 19 > 15)."""
    p = tmp_path / "profile.yaml"
    p.write_text("max_items: 15\nquotas: {in_person: 8, jobs: 4, posts: 3, online: 4}\n")
    with pytest.raises(ConfigError, match="more than max_items"):
        load_profile(p)


@respx.mock
def test_over_quota_model_is_skipped_for_the_rest_of_the_run(profile):
    """A model that answers 429 isn't retried on every batch (seen live: wasted calls per batch)."""
    limited = respx.post(GEMINI.format("busy")).respond(429, json={"error": {"message": "quota"}})
    respx.post(GEMINI.format("spare")).mock(side_effect=gemini_answer)
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(45)}  # 3 batches
    ref = refiner(profile, "busy", "spare")
    assert len(run(ref.refine(items, NOW, make_fetcher()))) == 45
    assert limited.call_count == 2  # 2 tries in batch 1, then skipped


@respx.mock
def test_stops_early_when_every_model_fails(profile):
    """When all models fail on a batch (quota/outage), later batches aren't attempted."""
    busy = respx.post(url__startswith="https://generativelanguage.googleapis.com/").respond(503)
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(80)}  # 4 batches
    ref = refiner(profile, "a", "b")
    assert run(ref.refine(items, NOW, make_fetcher())) == {}
    assert busy.call_count == 8  # batches 1-2 only: 2 models x 2 tries each
    assert "stopped early, 40 items" in ref.failures[-1]


@respx.mock
def test_one_failed_batch_does_not_stop_the_run(profile):
    """A single overloaded moment (503) often clears within seconds (seen live)."""
    route = respx.post(GEMINI.format("m"))
    route.side_effect = [httpx.Response(503), httpx.Response(503), gemini_answer, gemini_answer]
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(60)}  # 3 batches
    out = run(refiner(profile, "m").refine(items, NOW, make_fetcher()))
    assert len(out) == 40
