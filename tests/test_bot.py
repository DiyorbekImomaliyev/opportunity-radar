"""The interactive bot (SPEC §8b): review cards, marks, reminders, finds, chat and commands."""

import json
from datetime import timedelta

import pytest
import respx

from eventparser import config
from eventparser.bot import (
    Bot,
    ChatAction,
    apply_change,
    ics_event,
    item_keyboard,
    next_reminder,
    run_bot,
)
from eventparser.models import Kind
from eventparser.store import Store
from tests.conftest import NOW, item, make_fetcher, run
from tests.test_llm_config import GEMINI

OWNER = "1000000001"


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.answers, self.files = [], [], [], []
        self._mid = 100

    async def send(self, chat_id, text, markup=None, silent=False):
        self._mid += 1
        self.sent.append({"chat": chat_id, "text": text, "markup": markup, "id": self._mid, "silent": silent})
        return self._mid

    async def edit(self, chat_id, message_id, text, markup=None):
        self.edits.append({"id": message_id, "text": text, "markup": markup})

    async def answer(self, callback_id, text=""):
        self.answers.append(text)

    async def send_file(self, chat_id, filename, content, caption=""):
        self.files.append((filename, content))


def buttons(markup) -> list[str]:
    return [b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row]


def tap(bot, data, message_id, chat=OWNER):
    return run(bot.handle({"callback_query": {"id": "cb", "data": data,
                                              "message": {"message_id": message_id, "chat": {"id": int(chat)}}}}))


def say(bot, text, chat=OWNER, **extra):
    return run(bot.handle({"message": {"chat": {"id": int(chat)}, "text": text, **extra}}))


@pytest.fixture
def setup(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "overrides.yaml")
    store, tg = Store(":memory:"), FakeTelegram()
    bot = Bot(store, profile, tg, OWNER, make_fetcher(), now=lambda: NOW, api_key="")
    soon = NOW + timedelta(days=10)
    items = [item(url=f"https://ex.com/{i}", title=f"AI Night {i}", starts_at=soon + timedelta(days=i),
                  location="Istanbul") for i in range(3)]
    for it in items:
        store.upsert(it, NOW)
    store.commit()
    return bot, store, tg, [it.uid for it in items]


# ------------------------------------------------------------------ security


def test_only_the_owner_is_served(setup):
    """R-BOT-1"""
    bot, store, tg, uids = setup
    say(bot, "/saved", chat="999")
    tap(bot, f"m:r:{uids[0]}", 1, chat="999")
    assert tg.sent == [] and store.get_mark(uids[0]) is None


# ------------------------------------------------------------------ item cards


def labels(markup) -> list[str]:
    return [b["text"] for row in (markup or {}).get("inline_keyboard", []) for b in row]


def test_item_keyboard_fits_telegram_and_names_the_action():
    """R-BOT-2: ✅ reads Registered for events, Applied for jobs/programs; the choice is ticked."""
    uid = "f" * 16
    kb = item_keyboard(uid=uid, kind=Kind.EVENT)
    assert buttons(kb) == [f"m:r:{uid}", f"m:s:{uid}", f"m:d:{uid}", f"m:i:{uid}"]
    assert labels(kb)[0] == "✅ Registered"
    assert labels(item_keyboard(uid=uid, kind=Kind.JOB))[0] == "✅ Applied"
    assert labels(item_keyboard(uid=uid, kind=Kind.JOB, chosen="saved"))[1] == "• ⭐ Remind me •"


def test_tapping_a_card_marks_it_and_updates_the_message(setup):
    """R-BOT-2, R-BOT-3: ✅ marks, sends a calendar file for a dated event and edits the card in
    place (status line + ticked button); a second tap changes the mark; marked items never return."""
    bot, store, tg, uids = setup
    tap(bot, f"m:r:{uids[0]}", 501)
    mark = store.get_mark(uids[0])
    assert mark.status == "registered" and mark.next_remind_at == mark.deadline - timedelta(days=1)
    assert tg.files and tg.files[0][0] == "event.ics" and b"SUMMARY:AI Night 0" in tg.files[0][1]
    edit = tg.edits[-1]
    assert edit["id"] == 501 and "AI Night 0" in edit["text"] and "✅ Registered" in edit["text"]
    assert "• ✅ Registered •" in labels(edit["markup"]) and tg.answers[-1].startswith("✅ Registered")
    assert uids[0] not in {s.uid for s in store.unsent()}

    tap(bot, f"m:d:{uids[0]}", 501)  # changed their mind
    assert store.get_mark(uids[0]).status == "dismissed" and "• ❌ Not for me •" in labels(tg.edits[-1]["markup"])


def test_applied_job_toast(setup, profile):
    bot, store, tg, uids = setup
    job = item(url="https://ex.com/job", title="Data Analyst Intern", kind=Kind.JOB, location="Dubai")
    store.upsert(job, NOW)
    tap(bot, f"m:r:{job.uid}", 7)
    assert tg.answers[-1] == "✅ Applied — good luck!" and not tg.files


def test_review_resends_unmarked_items_of_the_last_digest(setup):
    """R-BOT-2: /review sends only the cards not marked yet."""
    bot, store, tg, uids = setup
    say(bot, "/review")
    assert "Nothing left" in tg.sent[-1]["text"]
    store.kv_set("last_digest", uids)
    run(bot.mark_item(uids[1], "dismissed"))
    say(bot, "/review")
    cards = tg.sent[-2:]
    assert "2 not marked" in tg.sent[-3]["text"]
    assert ["AI Night 0" in cards[0]["text"], "AI Night 2" in cards[1]["text"]] == [True, True]
    assert buttons(cards[0]["markup"])[0] == f"m:r:{uids[0]}"


# ------------------------------------------------------------------ reminders


def test_next_reminder_rules():
    """R-BOT-4"""
    every = 3
    assert next_reminder("saved", NOW, every, None) == NOW + timedelta(days=3)
    assert next_reminder("saved", NOW, every, NOW + timedelta(days=2)) == NOW + timedelta(days=1)  # day before
    assert next_reminder("saved", NOW, every, NOW + timedelta(hours=12)) == NOW + timedelta(hours=12)
    assert next_reminder("saved", NOW, every, NOW - timedelta(days=1)) is None  # deadline passed
    assert next_reminder("registered", NOW, every, None, NOW + timedelta(days=5)) == NOW + timedelta(days=4)
    assert next_reminder("registered", NOW, every, None, NOW + timedelta(hours=10)) is None
    assert next_reminder("dismissed", NOW, every, None) is None


def test_saved_items_are_reminded_every_period_until_removed(setup):
    """R-BOT-4: ⭐ → reminder every 3 days; ⏰ snoozes; ✅ Done and 🔕 Remove stop it."""
    bot, store, tg, uids = setup
    tap(bot, f"m:s:{uids[1]}", 501)  # deadline 11 days away
    assert store.get_mark(uids[1]).next_remind_at == NOW + timedelta(days=3)

    later = NOW + timedelta(days=3, minutes=1)
    bot.now = lambda: later
    assert run(bot.send_due_reminders()) == 1
    reminder = tg.sent[-1]
    assert "⏰ Reminder" in reminder["text"] and "AI Night 1" in reminder["text"]
    assert {f"r:done:{uids[1]}", f"r:snooze:{uids[1]}", f"r:remove:{uids[1]}"} == set(buttons(reminder["markup"]))
    assert store.get_mark(uids[1]).next_remind_at == later + timedelta(days=3)
    assert run(bot.send_due_reminders()) == 0  # not due again yet

    tap(bot, f"r:snooze:{uids[1]}", reminder["id"])
    assert "Snoozed" in tg.edits[-1]["text"]
    tap(bot, f"r:remove:{uids[1]}", reminder["id"])
    assert store.get_mark(uids[1]).status == "removed" and store.get_mark(uids[1]).next_remind_at is None
    bot.now = lambda: later + timedelta(days=30)
    assert run(bot.send_due_reminders()) == 0


def test_registered_event_gets_one_reminder_the_day_before(setup):
    bot, store, tg, uids = setup
    run(bot.mark_item(uids[0], "registered"))
    bot.now = lambda: NOW + timedelta(days=9, hours=1)
    assert run(bot.send_due_reminders()) == 1 and "📅 Tomorrow" in tg.sent[-1]["text"]
    assert store.get_mark(uids[0]).next_remind_at is None


# ------------------------------------------------------------------ finds


@respx.mock
def test_a_link_becomes_a_find(setup):
    """R-BOT-5: a link → 'Keep this?' with the page title → ⭐ saves it with reminders."""
    bot, store, tg, _ = setup
    respx.get("https://hack.example/ai-2026").respond(
        200, text='<html><head><meta property="og:title" content="Global AI Hackathon 2026"></head></html>')
    say(bot, "look at this one https://hack.example/ai-2026 deadline 30 October 2026")
    ask = tg.sent[-1]
    assert "Keep this?" in ask["text"] and "Global AI Hackathon 2026" in ask["text"]
    save = next(b for b in buttons(ask["markup"]) if b.startswith("f:s:"))
    tap(bot, save, ask["id"])
    mark = store.get_mark(save[4:])
    assert mark.status == "saved" and mark.source == "you" and mark.url == "https://hack.example/ai-2026"
    assert mark.deadline.date().isoformat() == "2026-10-29"  # 30 Oct 00:00 Istanbul in UTC
    assert "Saved" in tg.edits[-1]["text"]


def test_forwarded_post_link_entity_and_no(setup):
    bot, store, tg, _ = setup
    say(bot, "Applications open — apply here", entities=[{"type": "text_link", "url": "https://www.linkedin.com/posts/x"}])
    ask = tg.sent[-1]
    assert "Keep this?" in ask["text"]  # LinkedIn titles aren't fetched (rate limits); text is used
    no = next(b for b in buttons(ask["markup"]) if b.startswith("f:x:"))
    tap(bot, no, ask["id"])
    assert store.get_mark(no[4:]) is None and "Not kept" in tg.edits[-1]["text"]


def test_plain_note_without_ai_is_offered_for_keeping(setup):
    bot, store, tg, _ = setup
    say(bot, "Ask Prof. Ali about the research internship on 25 October 2026")
    ask = tg.sent[-1]
    assert "Keep this?" in ask["text"]
    save = next(b for b in buttons(ask["markup"]) if b.startswith("f:s:"))
    tap(bot, save, ask["id"])
    assert store.get_mark(save[4:]).status == "saved"


# ------------------------------------------------------------------ chat, commands, overrides


@respx.mock
def test_plain_language_change_is_proposed_then_applied(setup, monkeypatch):
    """R-BOT-6: Gemini maps the message to an action; nothing changes until ✅ Apply."""
    bot, store, tg, _ = setup
    bot.api_key = "k"
    respx.post(GEMINI.format("gemini-2.5-flash")).respond(200, json={"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": json.dumps({"action": "exclude_word", "value": "real estate", "number": None, "reply": "ok"})}]}}]})
    say(bot, "no more real estate events please")
    ask = tg.sent[-1]
    assert "Change settings?" in ask["text"] and "real estate" in ask["text"]
    assert not config.OVERRIDES_PATH.exists()
    apply = next(b for b in buttons(ask["markup"]) if b.startswith("c:ok:"))
    tap(bot, apply, ask["id"])
    assert "Applied" in tg.edits[-1]["text"]
    assert config.load_overrides()["exclude"] == ["real estate"]


def test_cancel_changes_nothing(setup):
    bot, store, tg, _ = setup
    say(bot, "/exclude crypto")
    cancel = next(b for b in buttons(tg.sent[-1]["markup"]) if b.startswith("c:no:"))
    tap(bot, cancel, tg.sent[-1]["id"])
    assert not config.OVERRIDES_PATH.exists() and "Cancelled" in tg.edits[-1]["text"]


def test_invalid_changes_are_refused(setup):
    bot, store, tg, _ = setup
    say(bot, "/follow https://example.com/not-linkedin")
    assert "couldn't turn that into a valid change" in tg.sent[-1]["text"]
    say(bot, "/quota abroad 3")
    assert "couldn't turn that into a valid change" in tg.sent[-1]["text"]


def test_apply_change_validates_and_merges(tmp_path, monkeypatch):
    """Overrides are merged over the profile/sources and validated before saving."""
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "overrides.yaml")
    monkeypatch.setattr(config, "load_profile", lambda path=None, overrides=None: None)
    monkeypatch.setattr(config, "load_sources", lambda path=None, overrides=None: None)
    apply_change(ChatAction(action="follow_linkedin", value="https://www.linkedin.com/company/quickdrop/"))
    apply_change(ChatAction(action="set_quota", value="international", number=5))
    apply_change(ChatAction(action="unfollow_linkedin", value="https://www.linkedin.com/company/quickdrop/"))
    ov = config.load_overrides()
    assert ov["quotas"] == {"international": 5}
    assert ov["linkedin_unfollow"] == ["https://www.linkedin.com/company/quickdrop/"] and not ov.get("linkedin_follow")


def test_overrides_merge_into_sources_and_profile():
    entries = [{"name": "li", "type": "linkedin_posts", "pages": ["https://www.linkedin.com/company/a/"]}]
    out = config.apply_source_overrides(entries, {"linkedin_follow": ["https://www.linkedin.com/company/b/"],
                                                  "linkedin_unfollow": ["https://www.linkedin.com/company/a/"]})
    assert out[0]["pages"] == ["https://www.linkedin.com/company/b/"] and entries[0]["pages"] == ["https://www.linkedin.com/company/a/"]
    raw = config.apply_profile_overrides({"exclude": ["x"], "quotas": {"jobs": 3}},
                                         {"exclude": ["y", "x"], "quotas": {"online": 2}})
    assert raw["exclude"] == ["x", "y"] and raw["quotas"] == {"jobs": 3, "online": 2}


def test_saved_and_upcoming_lists(setup):
    bot, store, tg, uids = setup
    run(bot.mark_item(uids[0], "saved"))
    say(bot, "/saved")
    assert "AI Night 0" in tg.sent[-1]["text"] and "⭐" in tg.sent[-1]["text"]
    for u in uids:
        store.set_score(u, 8, [], "about")
    store.commit()
    bot.store = store
    say(bot, "/upcoming")
    assert "Nothing strong" in tg.sent[-1]["text"]  # all are 10+ days away; default window is 7
    assert "next 7 days" in tg.sent[-1]["text"]


def test_help_and_unknown_commands(setup):
    bot, store, tg, _ = setup
    say(bot, "/start")
    assert "What I can do" in tg.sent[-1]["text"]
    say(bot, "/nonsense")
    assert "What I can do" in tg.sent[-1]["text"]


def test_ics_event_format():
    raw = ics_event("AI, Night; Istanbul", NOW, None, "https://x.y", "abc").decode()
    assert raw.startswith("BEGIN:VCALENDAR\r\n") and "SUMMARY:AI\\, Night\\; Istanbul" in raw
    assert "DTSTART:20261015T060000Z" in raw and "DTEND:20261015T080000Z" in raw


# ------------------------------------------------------------------ the loop


@respx.mock
def test_run_bot_once_handles_updates_and_remembers_offset(profile, monkeypatch):
    """R-BOT-1: updates are handled once; the offset is stored so nothing is processed twice."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    updates = respx.post("https://api.telegram.org/botT/getUpdates").respond(200, json={"ok": True, "result": [
        {"update_id": 41, "message": {"chat": {"id": int(OWNER)}, "text": "/help"}},
        {"update_id": 42, "message": {"chat": {"id": 1}, "text": "/help"}},
    ]})
    sent = respx.post("https://api.telegram.org/botT/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 1}})
    store = Store(":memory:")
    run(run_bot(store, profile, make_fetcher(), once=True))
    assert sent.call_count == 1  # only the owner was answered
    assert store.kv_get("tg_offset") == 43
    assert json.loads(updates.calls[0].request.content)["allowed_updates"] == ["message", "callback_query"]


def test_marks_feed_the_ai_examples(setup):
    """R-BOT-7"""
    from eventparser.llm import build_user_content
    from eventparser.pipeline import owner_examples
    bot, store, tg, uids = setup
    run(bot.mark_item(uids[0], "registered"))
    run(bot.mark_item(uids[1], "dismissed"))
    ex = owner_examples(store)
    assert ex["liked"] == ["AI Night 0"] and ex["disliked"] == ["AI Night 1"]
    content = build_user_content({"u": item()}, bot.profile, NOW, ex)
    assert "I registered for / saved: AI Night 0" in content and "Not for me: AI Night 1" in content
