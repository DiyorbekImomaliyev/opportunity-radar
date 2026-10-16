"""Hard filters, rule scoring and daily prioritisation (SPEC §6)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from eventparser.digest import Entry, Tier, prioritize, tier_for
from eventparser.models import Kind
from eventparser.relevance import assess, has_word
from tests.conftest import NOW, item

SOON = NOW + timedelta(days=5)


def test_word_matching_is_word_bounded_and_plural_aware():
    assert has_word("New AI hackathons in Istanbul", "hackathon")
    assert has_word("AI-powered workshop", "ai")
    assert not has_word("Said the chair", "ai")
    assert has_word("Machine   Learning day", "machine learning")
    assert not has_word("", "ai")


@pytest.mark.parametrize("kw, expected", [
    (dict(starts_at=NOW - timedelta(days=2), ends_at=NOW - timedelta(days=1)), "already over"),
    (dict(starts_at=NOW + timedelta(days=200)), "days ahead"),
    (dict(title="AI Workshop (Sold Out)", starts_at=SOON), "excluded"),
    (dict(starts_at=SOON, location="San Francisco, CA", is_online=False), "outside home region"),
])
def test_hard_filters_drop_events(profile, kw, expected):
    """R-REL-2"""
    a = assess(item(**kw), profile, NOW)
    assert not a.candidate and expected in a.dropped


def test_ongoing_multi_day_event_is_kept(profile):
    a = assess(item(starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=1), location="Istanbul"), profile, NOW)
    assert a.candidate


def test_online_event_outside_region_is_kept(profile):
    a = assess(item(title="AI Hackathon", kind=Kind.HACKATHON, ends_at=SOON, location="Online", is_online=True), profile, NOW)
    assert a.candidate and "online" in a.reasons and "hackathon" in a.reasons


def test_old_posts_and_jobs_dropped(profile):
    """R-REL-2"""
    old = NOW - timedelta(days=10)
    assert "published" in assess(item(kind=Kind.POST, title="AI event", published_at=old), profile, NOW).dropped
    assert "published" in assess(item(kind=Kind.JOB, title="AI Intern", published_at=old, location="Istanbul"), profile, NOW).dropped


def test_job_title_filter(profile):
    """R-REL-2: only entry-level / internship titles."""
    senior = assess(item(kind=Kind.JOB, title="Senior AI Manager", location="Istanbul, Turkey"), profile, NOW)
    assert not senior.candidate and senior.dropped == "senior role (senior)"
    intern = assess(item(kind=Kind.JOB, title="AI Research Intern", location="Istanbul, Turkey"), profile, NOW)
    assert intern.candidate and intern.reasons[0] == "ai" and intern.score > 0


def test_internship_needs_a_real_topic(profile):
    """R-REL-3: 'Internship – Food & Beverage' matched 'intern' and scored 10 (seen live).
    For jobs, the job-type words are a filter, not a topic."""
    fnb = assess(item(kind=Kind.JOB, title="Internship – Food & Beverage", location="Istanbul, Turkey"), profile, NOW)
    assert fnb.candidate and fnb.score == 0


def test_job_outside_region_dropped_unless_remote(profile):
    assert not assess(item(kind=Kind.JOB, title="AI Intern", location="Berlin, Germany"), profile, NOW).candidate
    assert assess(item(kind=Kind.JOB, title="AI Intern", location="Berlin", is_online=True), profile, NOW).candidate


def test_scoring_title_counts_double_and_region_bonus(profile):
    """R-REL-3"""
    a = assess(item(title="AI Workshop", summary="hands-on", starts_at=SOON, location="TSTP, Istanbul"), profile, NOW)
    # topic ai(3)*2 + format workshop(2)*1 + home 3 = 11 → clamped to 10
    assert a.score == 10 and a.reasons == ["ai", "workshop", "local"]
    profile.online_events = True
    b = assess(item(title="Evening session", summary="We talk AI", starts_at=SOON, location="Online", is_online=True), profile, NOW)
    assert b.score == 3 + 1


def test_online_events_can_be_switched_off(profile):
    """R-REL-2: online events are candidates by default; `online_events: false` drops them
    (online hackathons are always kept)."""
    web = item(title="AI Workshop (Zoom)", starts_at=SOON, location="Online", is_online=True)
    hack = item(kind=Kind.HACKATHON, title="AI Hackathon", ends_at=SOON, is_online=True)
    assert assess(web, profile, NOW).candidate
    profile.online_events = False
    assert "online event" in assess(web, profile, NOW).dropped
    assert assess(hack, profile, NOW).candidate


def test_notable_organiser_boost_and_reason(profile):
    """R-REL-3: events by notable organisers (NVIDIA, Google…) get +2 and say so first."""
    plain = assess(item(title="AI Hackathon", kind=Kind.HACKATHON, ends_at=SOON, is_online=True,
                        organizer="Small Club"), profile, NOW)
    nv = assess(item(title="AI Hackathon", kind=Kind.HACKATHON, ends_at=SOON, is_online=True,
                     organizer="Nebius x NVIDIA", url="https://x.y/nv"), profile, NOW)
    assert nv.reasons[0] == "by NVIDIA" and nv.score == min(plain.score + 2, 10)
    assert not has_word("envidia", "nvidia")


def test_format_words_alone_do_not_qualify(profile):
    """R-REL-3: 'Discovering animated cinema … creative workshop' is not an AI opportunity."""
    kids = item(title="Discovering animated cinema for little ones", summary="followed by a creative workshop",
                starts_at=SOON, location="Institut français du Turkey, Istanbul, QA")
    assert assess(kids, profile, NOW).score == 0
    conf = item(title="Marine & Offshore Expo & Conference, Turkey", starts_at=SOON, location="Istanbul")
    assert assess(conf, profile, NOW).score == 0


def test_region_alone_is_not_relevance(profile):
    """R-REL-3: 'Primavera P6 Training Course in Turkey' must not be sent."""
    a = assess(item(title="Primavera P6 Training Course in Turkey", starts_at=SOON, location="Istanbul, Turkey"), profile, NOW)
    assert a.candidate and a.score == 0


def test_posts_need_opportunity_or_interest(profile):
    """R-REL-3"""
    hack = assess(item(kind=Kind.POST, title="Microsoft: Register for our hackathon", published_at=NOW), profile, NOW)
    assert hack.score >= profile.min_score
    generic = assess(item(kind=Kind.POST, title="X: Happy Wednesday team!", published_at=NOW), profile, NOW)
    assert generic.score == 0
    opp_only = assess(item(kind=Kind.POST, title="X: Applications are open — apply now", published_at=NOW), profile, NOW)
    assert opp_only.score == profile.min_score and "followed page" in opp_only.reasons


# ------------------------------------------------------------------ test prioritize

SOON = NOW + timedelta(days=7)


def e(uid, score, **kw):
    kw.setdefault("url", f"https://x.y/{uid}")
    return Entry(uid, item(**kw), score)


def test_tiers(profile):
    assert tier_for(e("a", 5, starts_at=SOON, location="TSTP, Istanbul", is_online=False), profile) == Tier.IN_PERSON_HOME
    assert tier_for(e("b", 5, kind=Kind.HACKATHON, ends_at=SOON, location="Kadikoy", is_online=False), profile) == Tier.IN_PERSON_HOME
    assert tier_for(e("c", 5, kind=Kind.JOB, title="AI Intern"), profile) == Tier.INTERNSHIP
    assert tier_for(e("d", 5, kind=Kind.POST, title="X: join"), profile) == Tier.POST
    assert tier_for(e("f", 5, kind=Kind.HACKATHON, ends_at=SOON, is_online=True), profile) == Tier.ONLINE
    assert tier_for(e("g", 5, starts_at=SOON, is_online=True), profile) == Tier.ONLINE


def test_in_person_turkey_beats_higher_scored_online(profile):
    """A 6/10 networking event in Istanbul outranks a 10/10 online hackathon."""
    istanbul = e("istanbul", 6, starts_at=SOON, location="Istanbul", is_online=False)
    online = e("hack", 10, kind=Kind.HACKATHON, ends_at=SOON, is_online=True)
    chosen, _ = prioritize([online, istanbul], profile)
    assert [x.uid for x in chosen] == ["istanbul", "hack"]


def test_notable_organisers_lead_the_online_tier(profile):
    """R-REL-6: among online items, notable organisers come first even with a lower score."""
    small = e("small", 10, kind=Kind.HACKATHON, ends_at=SOON, is_online=True, organizer="Tiny Club")
    google = e("google", 8, starts_at=SOON, is_online=True, title="Google AI Agents Live Workshop")
    profile.max_online = 1
    chosen, deferred = prioritize([small, google], profile)
    assert [x.uid for x in chosen] == ["google"] and [x.uid for x in deferred] == ["small"]


def test_daily_cap_and_online_cap(profile):
    profile.max_items, profile.max_online = 5, 2
    hacks = [e(f"h{i}", 10 - i, kind=Kind.HACKATHON, ends_at=SOON, is_online=True) for i in range(6)]
    istanbul = [e(f"d{i}", 5, starts_at=SOON, location="Istanbul", is_online=False) for i in range(2)]
    chosen, deferred = prioritize(hacks + istanbul, profile)
    assert [x.uid for x in chosen] == ["d0", "d1", "h0", "h1"]  # only 2 online items, best first
    assert len(deferred) == 4 and {x.uid for x in deferred} == {"h2", "h3", "h4", "h5"}
    profile.max_items = 3
    chosen, deferred = prioritize(hacks + istanbul, profile)
    assert [x.uid for x in chosen] == ["d0", "d1", "h0"] and len(deferred) == 5


def test_within_tier_score_then_soonest(profile):
    a = e("a", 7, starts_at=NOW + timedelta(days=20), location="Istanbul", is_online=False)
    b = e("b", 7, starts_at=NOW + timedelta(days=2), location="Istanbul", is_online=False)
    c = e("c", 9, starts_at=NOW + timedelta(days=30), location="Istanbul", is_online=False)
    assert [x.uid for x in prioritize([a, b, c], profile)[0]] == ["c", "b", "a"]


def test_ai_threshold_is_stricter_than_rule_threshold(profile, tmp_path):
    """R-REL-5: an item the AI rates 5/10 is not sent (min_ai_score 6), even though rules said yes."""
    import respx

    from eventparser.llm import Refiner
    from eventparser.store import Store
    from tests.test_llm_config import GEMINI, gemini_answer
    from tests.test_pipeline import Outbox, go, mock_sites

    with respx.mock:
        mock_sites()
        respx.post(GEMINI.format("m")).mock(side_effect=lambda r: gemini_answer(r, score=5))
        result = go(Store(":memory:"), profile, tmp_path, deliver=Outbox(), use_llm=True,
                    refiner=Refiner(profile, api_key="k", models=["m"]))
    assert result.digest.item_count == 0


def test_company_named_community_groups_are_not_the_company(profile):
    """R-REL-3: 'GOOGLE DEVELOPER GROUP AT FIU' is a student club, not Google (seen live)."""
    from eventparser.relevance import notable
    club = item(title="305 Veterans Hackathon", organizer="GOOGLE DEVELOPER GROUP AT FIU")
    real = item(title="Google AI Agents Workshop", organizer="Google")
    profile.notable_organizers, profile.notable_exclude = ["google"], ["google developer group"]
    assert notable(club, profile) is None and notable(real, profile) == "Google"


def test_entry_level_tag_lets_plain_titles_through(profile):
    """R-REL-2: 'Data Analyst' from an entry-level LinkedIn search is a junior job; without the
    tag the title must say intern/junior/graduate."""
    profile.interests["data analyst"] = 3
    plain = item(kind=Kind.JOB, title="Data Analyst", location="Istanbul, Turkey")
    tagged = plain.model_copy(update={"tags": ["level:entry"]})
    assert not assess(plain, profile, NOW).candidate
    assert assess(tagged, profile, NOW).candidate and assess(tagged, profile, NOW).score > 0


def test_quotas_keep_a_mix_and_leftovers_are_filled(profile):
    """R-REL-6: 20 strong Turkey events can't push out jobs and online picks (seen live: all 15
    slots went to one kind); when a kind has fewer items, its slots go to the next best."""
    profile.max_items, profile.quotas, profile.max_online = 10, {"in_person": 5, "jobs": 3, "online": 2}, 4
    local = [e(f"d{i}", 9, starts_at=SOON, location="Istanbul", is_online=False) for i in range(20)]
    jobs = [e(f"j{i}", 7, kind=Kind.JOB, title="AI Intern") for i in range(2)]
    online = [e(f"o{i}", 8, kind=Kind.HACKATHON, ends_at=SOON, is_online=True) for i in range(5)]
    chosen, deferred = prioritize(local + jobs + online, profile)
    ids = [x.uid for x in chosen]
    assert len(chosen) == 10 and len(deferred) == 17
    assert {"j0", "j1"} <= set(ids)                      # both jobs (quota 3, only 2 exist)
    assert sum(i.startswith("o") for i in ids) == 2      # online quota
    assert sum(i.startswith("d") for i in ids) == 6      # 5 by quota + 1 leftover job slot


def test_notable_priority_is_only_for_online(profile):
    """R-REL-6: in person, a 7/10 'Microsoft' user group must not outrank 9/10 events (seen live)."""
    profile.max_items, profile.quotas = 1, {"in_person": 1}
    mention = e("ms", 7, starts_at=SOON, location="Istanbul", is_online=False, title="Microsoft Power BI User Group")
    strong = e("wif", 9, starts_at=SOON, location="Istanbul", is_online=False, title="World Investment Forum")
    assert [x.uid for x in prioritize([mention, strong], profile)[0]] == ["wif"]


def test_international_jobs_need_relocation_ok_and_get_their_own_tier(profile):
    """R-REL-2, R-REL-6: a Berlin job from an `international` source is dropped unless the owner
    accepts relocation; then it ranks in the International tier, with its own quota."""
    berlin = item(kind=Kind.JOB, title="AI Intern", location="Berlin, Germany", tags=["international"])
    assert "outside home region" in assess(berlin, profile, NOW).dropped
    profile.relocation_ok = True
    assert assess(berlin, profile, NOW).candidate
    remote = item(kind=Kind.JOB, title="Junior Data Analyst", url="https://x.y/r", is_online=True, location="Remote")
    assert tier_for(Entry("b", berlin, 8), profile) == Tier.INTERNATIONAL
    assert tier_for(Entry("r", remote, 8), profile) == Tier.INTERNATIONAL
    local = item(kind=Kind.JOB, title="AI Intern", url="https://x.y/l", location="Istanbul, Turkey")
    assert tier_for(Entry("l", local, 8), profile) == Tier.INTERNSHIP


@pytest.mark.parametrize("title, dropped", [
    ("ML Engineer (Production Model Deployment) | Hybrid (Shibuya) / Max ¥10M / Business Japanese", "needs Japanese"),
    ("Junior Data Analyst – German-speaking (m/w/d)", "needs German"),
    ("Data Scientist Intern, fluent French required", "needs French"),
    ("Arabic NLP Engineer Intern", None),                 # a language as the topic, not a requirement
    ("Senior Data Scientist", "senior role (senior)"),
    ("Lead ML Engineer – Graduate Program", "senior role (lead)"),
    ("Junior AI Engineer", None),
])
def test_language_and_seniority_guards(profile, title, dropped):
    """R-REL-2: titles demanding a language the owner doesn't speak, or a senior level, are dropped
    even when a board labels them entry-level (seen live: 'Business Japanese', '¥25M' senior roles)."""
    profile.interests.update({"ml engineer": 3, "data scientist": 3, "data analyst": 3, "ai engineer": 3, "nlp": 3})
    a = assess(item(kind=Kind.JOB, title=title, tags=["level:entry"], is_online=True), profile, NOW)
    assert a.dropped == dropped


def test_ai_checked_items_rank_before_unchecked_ones(profile):
    """R-REL-6: an unchecked rule score of 10 must not beat an AI-checked 8 in the same tier."""
    profile.max_items, profile.quotas = 1, {"international": 1}
    unchecked = Entry("rule", item(kind=Kind.JOB, title="Data Scientist", url="https://x.y/1", is_online=True), 10.0)
    checked = Entry("ai", item(kind=Kind.JOB, title="AI Engineer", url="https://x.y/2", is_online=True), 8.0, "Real role.")
    assert [x.uid for x in prioritize([unchecked, checked], profile)[0]] == ["ai"]


def test_visa_restricted_jobs_dropped_and_sponsorship_boosted(profile):
    """R-REL-2, R-REL-3: with needs_visa_sponsorship, 'nationals only / no sponsorship' jobs are
    dropped and jobs offering sponsorship get +2."""
    from eventparser.relevance import assess
    p = profile.model_copy(update={"needs_visa_sponsorship": True, "relocation_ok": True})
    base = dict(kind=Kind.JOB, title="AI Research Intern", location="Dubai", published_at=NOW)
    plain = assess(item(url="https://x/1", tags=["international"], **base), p, NOW)
    offer = assess(item(url="https://x/2", tags=["international", "visa:sponsored"], **base), p, NOW)
    closed = assess(item(url="https://x/3", tags=["international", "visa:restricted"], **base), p, NOW)
    assert closed.dropped == "no visa sponsorship / nationals only"
    tamheer = dict(base, title="Customer Relations AI Data Analyst (Tamheer)", location="Riyadh")  # seen live
    assert assess(item(url="https://x/4", tags=["international"], **tamheer), p, NOW).dropped
    assert offer.score == min(plain.score + 2, 10) and "visa sponsorship" in offer.reasons
    assert assess(item(url="https://x/3", tags=["international", "visa:restricted"], **base), profile.model_copy(
        update={"relocation_ok": True}), NOW).dropped is None  # only when the owner needs a visa
