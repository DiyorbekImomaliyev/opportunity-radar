"""Digest rendering (SPEC §9) and delivery (SPEC §10)."""

import json
import logging
from datetime import timedelta

import httpx
import pytest
import respx

from eventparser.cli import setup_logging
from eventparser.digest import (
    MAX_MESSAGE,
    Card,
    Digest,
    Entry,
    Section,
    SourceStatus,
    build_digest,
    section_for,
)
from eventparser.models import Kind
from eventparser.telegram import DeliveryError, send_telegram, telegram_chat_ids
from tests.conftest import NOW, item, make_fetcher, run

SOON = NOW + timedelta(days=10)


def entry(uid="u1", score=7.0, about=None, **kw) -> Entry:
    kw.setdefault("url", f"https://example.com/{uid}")
    return Entry(uid, item(**kw), score, about)


def test_sections(profile):
    """R-DIG-2"""
    cases = [
        (entry(kind=Kind.HACKATHON, ends_at=NOW + timedelta(days=2)), Section.CLOSING),
        (entry(starts_at=NOW + timedelta(days=1), location="Istanbul"), Section.CLOSING),
        (entry(starts_at=SOON, location="TSTP, Istanbul, Turkey", is_online=False), Section.HOME),
        (entry(kind=Kind.HACKATHON, ends_at=SOON, is_online=True), Section.HACKATHONS),
        (entry(kind=Kind.JOB, title="AI Intern"), Section.JOBS),
        (entry(kind=Kind.PROGRAM, title="Accelerator"), Section.JOBS),
        (entry(starts_at=SOON, is_online=True), Section.ONLINE),
        (entry(kind=Kind.POST, title="X: hackathon"), Section.POSTS),
    ]
    for e, expected in cases:
        assert section_for(e, profile, NOW) == expected, e.item


def test_digest_order_escaping_and_content(profile):
    """R-DIG-2, R-DIG-3, R-DIG-4"""
    entries = [
        entry("post", kind=Kind.POST, title="Microsoft: AI cost < $5 & you", published_at=NOW - timedelta(hours=5)),
        entry("qa-low", 4, starts_at=SOON, location="Istanbul", title="Low"),
        entry("qa-high", 9, starts_at=SOON, location="Istanbul", title="High", about="2-day AI bootcamp for students."),
        entry("hack", kind=Kind.HACKATHON, ends_at=SOON, is_online=True, title="Global AI Hack"),
    ]
    d = build_digest(entries, profile, NOW, [SourceStatus("tstp", True), SourceStatus("linkedin", False, "auth wall", 3)])
    text = "\n".join(d.messages)
    assert text.index("🏠 Near you") < text.index("🏆 Hackathons") < text.index("💬 LinkedIn posts")
    assert text.index(">High<") < text.index(">Low<")  # score desc
    assert "AI cost &lt; $5 &amp; you" in text  # escaped for Telegram HTML
    assert "2-day AI bootcamp for students." in text
    assert "4 new opportunities" in text and "Thu 15 Oct 2026" in text
    assert "Sources: 1/2 OK" in text and "linkedin: auth wall ⚠️ failing 3 runs" in text
    assert "Posted 5h ago" in text and "⏳ Deadline" in text
    assert "AI rated 9/10" in text  # AI-judged entries show the rating
    assert set(d.included) == {"post", "qa-low", "qa-high", "hack"}


def test_rule_scored_entries_show_reasons_not_numbers(profile):
    e = Entry("u", item(starts_at=SOON, is_online=True), 10.0, None, ["ai", "hackathon", "online", "ai:relevant"])
    text = build_digest([e], profile, NOW, []).cards[0].text
    assert "test · ai · hackathon · online" in text and "/10" not in text


def test_empty_digest_still_reports(profile):
    """R-DIG-6: silence never means broken."""
    d = build_digest([], profile, NOW, [SourceStatus("tstp", True)])
    assert len(d.messages) == 1 and "No new opportunities today" in d.messages[0] and "Sources: 1/1 OK" in d.messages[0]
    assert d.included == []


def test_every_item_is_its_own_message(profile):
    """R-DIG-5: header, one card per item (section label first), footer; each ≤ the Telegram limit."""
    entries = [entry(f"u{i}", starts_at=SOON, is_online=True, title=f"Workshop {i} " + "x" * 80,
                     summary="y" * 400) for i in range(5)]
    entries.append(entry("huge", starts_at=SOON, location="Istanbul", title="Big " + "z" * 5000))
    d = build_digest(entries, profile, NOW, [SourceStatus("tstp", True)])
    assert len(d.messages) == 1 + 6 + 1 and [c.uid for c in d.cards] == d.included
    assert "6 new opportunities" in d.messages[0] and "🏠 Near you 1" in d.messages[0] and "🌐 Online events 5" in d.messages[0]
    assert d.cards[0].text.startswith("<i>🏠 Near you</i>\n")
    assert all(len(m) <= MAX_MESSAGE for m in d.messages) and "Sources: 1/1 OK" in d.messages[-1]
    assert all(c.text.count("<b><a") == c.text.count("</a></b>") for c in d.cards)


@respx.mock
def test_send_telegram(monkeypatch):
    """R-OUT-1, R-BOT-2: the header notifies, cards arrive silently with their buttons."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    route = respx.post("https://api.telegram.org/bot123:SECRET/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 1}})
    d = Digest("t", "head", [Card("a", Kind.EVENT, "card a"), Card("b", Kind.JOB, "card b")], "foot")
    run(send_telegram(make_fetcher(), d, markup_for=lambda c: {"inline_keyboard": [[{"text": c.uid, "callback_data": c.uid}]]}))
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["text"] for b in bodies] == ["head", "card a", "card b", "foot"]
    assert bodies[0] == {"chat_id": "42", "text": "head", "parse_mode": "HTML", "disable_web_page_preview": True}
    assert all(b["disable_notification"] for b in bodies[1:])
    assert bodies[2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "b" and "reply_markup" not in bodies[3]


@respx.mock
def test_partial_delivery_reports_the_cards_that_arrived(monkeypatch):
    """R-OUT-1: if Telegram fails midway, the cards already delivered are known (and marked sent)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    ok = {"ok": True, "result": {"message_id": 1}}
    respx.post("https://api.telegram.org/bot123:SECRET/sendMessage").mock(side_effect=[
        httpx.Response(200, json=ok), httpx.Response(200, json=ok),
        *[httpx.Response(400, json={"ok": False, "description": "Bad Request"})] * 5])
    d = Digest("t", "head", [Card("a", Kind.EVENT, "card a"), Card("b", Kind.EVENT, "card b")], "")
    with pytest.raises(DeliveryError) as e:
        run(send_telegram(make_fetcher(), d))
    assert e.value.delivered == ["a"] and "message 3/3" in str(e.value)


@respx.mock
def test_telegram_errors_never_leak_token(monkeypatch, tmp_path):
    """R-CFG-2: the bot token must not appear in errors or logs."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    setup_logging(tmp_path, verbose=True)
    respx.post("https://api.telegram.org/bot123:SECRET/sendMessage").respond(500)
    with pytest.raises(DeliveryError) as exc:
        run(send_telegram(make_fetcher(), Digest("t", ["x"], [])))
    assert "SECRET" not in str(exc.value)
    respx.post("https://api.telegram.org/bot123:SECRET/sendMessage").respond(
        400, json={"ok": False, "description": "Bad Request: can't parse entities"})
    with pytest.raises(DeliveryError):
        run(send_telegram(make_fetcher(), Digest("t", ["x"], [])))
    logging.shutdown()
    assert "SECRET" not in (tmp_path / "radar.log").read_text()


@respx.mock
def test_telegram_rejection_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    respx.post("https://api.telegram.org/bott/sendMessage").respond(200, json={"ok": False, "description": "chat not found"})
    with pytest.raises(DeliveryError, match="chat not found"):
        run(send_telegram(make_fetcher(), Digest("t", ["x"], [])))


def test_send_without_config_raises():
    with pytest.raises(DeliveryError):
        run(send_telegram(make_fetcher(), Digest("t", ["x"], [])))


@respx.mock
def test_telegram_setup_lists_chats(monkeypatch):
    """R-OUT-3"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    respx.post("https://api.telegram.org/bott/getUpdates").respond(200, json={"ok": True, "result": [
        {"message": {"chat": {"id": 555, "first_name": "Alex"}}},
        {"message": {"chat": {"id": 555, "first_name": "Alex"}}},
    ]})
    assert run(telegram_chat_ids(make_fetcher())) == [("555", "Alex")]
