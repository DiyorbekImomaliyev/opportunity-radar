"""One end-to-end run: fetch → store → dedup → score → digest → deliver."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from .config import DATA_DIR, Profile, SourceConfig, redact
from .digest import Digest, Entry, SourceStatus, build_digest, prioritize, write_preview
from .http import Fetcher, FetchError
from .llm import Refiner, ai_version
from .models import Item
from .relevance import assess, find_duplicates, notable
from .sources import REGISTRY
from .store import Store
from .telegram import DeliveryError, send_telegram, telegram_configured
from .textutil import set_local_tz, truncate

log = logging.getLogger(__name__)

MAX_CONCURRENT_SOURCES = 6
SOURCE_TIMEOUT = 420.0  # LinkedIn sources share one 4 s-per-request queue


@dataclass
class SourceResult:
    name: str
    ok: bool
    items: list[Item]
    error: str | None
    duration_ms: int
    skipped: int = 0


@dataclass
class RunResult:
    run_id: int
    sources: list[SourceResult]
    new_items: int
    digest: Digest
    delivered: bool
    delivery_error: str | None = None
    llm_failures: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        all_failed = bool(self.sources) and not any(s.ok for s in self.sources)
        return 2 if all_failed or self.delivery_error else 0


async def fetch_source(cfg: SourceConfig, fx: Fetcher, now: datetime,
                       known: Callable[[str], bool] | None = None) -> SourceResult:
    src = REGISTRY[cfg.type](cfg.name, cfg.params, now=now)
    if known:
        src.known = known
    t0 = time.perf_counter()
    try:
        items = await asyncio.wait_for(src.fetch(fx), timeout=SOURCE_TIMEOUT)
        ok, error = True, (redact("; ".join(src.warnings))[:500] or None)
    except Exception as e:  # noqa: BLE001 - one source must never abort the run (R-NET-4)
        plain = isinstance(e, (RuntimeError, ValueError, FetchError)) and str(e)
        items, ok, error = [], False, redact(str(e) if plain else f"{type(e).__name__}: {e}".rstrip(": "))[:500]
    ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "source %s: %s, %d items in %d ms%s",
        cfg.name, "ok" if ok else "FAILED", len(items), ms, f" — {error}" if error else "",
        extra={"source": cfg.name, "items": len(items), "duration_ms": ms, "ok": ok, "error": error},
    )
    return SourceResult(cfg.name, ok, items, error, ms, src.skipped)


async def fetch_all(cfgs: list[SourceConfig], fx: Fetcher, now: datetime,
                    known: Callable[[str], bool] | None = None) -> list[SourceResult]:
    sem = asyncio.Semaphore(MAX_CONCURRENT_SOURCES)

    async def guarded(cfg: SourceConfig) -> SourceResult:
        async with sem:
            return await fetch_source(cfg, fx, now, known)

    return list(await asyncio.gather(*(guarded(c) for c in cfgs)))


def _deadline_note(deadline: str | None, item: Item) -> str | None:
    if not deadline or item.deadline:
        return None
    try:
        d = date.fromisoformat(deadline)
    except ValueError:
        return None
    return f"Deadline {d.day} {d:%b}."


def apply_source_penalty(entries: list[Entry], rejection: dict[str, tuple[int, int]]) -> None:
    """Sources the owner keeps rejecting (≥5 marks, ≥70 %) lose a point (R-BOT-7).

    In memory only: stored AI scores are reused on later runs, so persisting the penalty would
    subtract it again every day."""
    for e in entries:
        rejected, total = rejection.get(e.item.source, (0, 0))
        if total >= 5 and rejected / total >= 0.7 and e.score > 0:
            e.score -= 1
            e.reasons.append(f"you often skip {e.item.source}")


def owner_examples(store: Store) -> dict[str, list[str]]:
    """The owner's latest choices, for the AI prompt (R-BOT-7)."""
    def titles(*statuses: str) -> list[str]:
        return [m.title for m in store.marks_with(*statuses)[:8]]

    return {"liked": titles("registered", "saved", "done"), "disliked": titles("dismissed"),
            "ineligible": titles("ineligible")}


async def send_with_buttons(fx: Fetcher, digest: Digest) -> None:
    """Telegram delivery: every card gets the bot's mark buttons (R-BOT-2)."""
    from .bot import item_keyboard

    await send_telegram(fx, digest, markup_for=item_keyboard)


async def after_delivery(store: Store, profile: Profile, fx: Fetcher, uids: list[str]) -> None:
    """Remember the digest for /review and send due reminders. Never fails the run."""
    from .bot import Bot
    from .telegram import Telegram, owner_chat_id

    store.kv_set("last_digest", uids)
    try:
        await Bot(store, profile, Telegram(fx), owner_chat_id(), fx).send_due_reminders()
    except Exception as e:  # noqa: BLE001
        log.warning("reminders not sent: %s", redact(str(e)))


async def run(
    *,
    sources: list[SourceConfig],
    profile: Profile,
    store: Store,
    now: datetime | None = None,
    dry_run: bool = False,
    only: list[str] | None = None,
    use_llm: bool = True,
    fetcher: Fetcher | None = None,
    refiner: Refiner | None = None,
    data_dir: Path = DATA_DIR,
    deliver: Callable[[Fetcher, Digest], Awaitable[None]] | None = None,
    scheduled: bool = False,
) -> RunResult:
    """`deliver` defaults to Telegram when configured; without it the run is a dry run."""
    set_local_tz(profile.timezone)
    now = now or datetime.now(UTC)
    priority = {c.name: i for i, c in enumerate(sources)}
    selected = [c for c in sources if c.enabled and (not only or c.name in only)]
    if only and (unknown := set(only) - {c.name for c in sources}):
        raise ValueError(f"unknown source(s): {', '.join(sorted(unknown))}")

    fx = fetcher or Fetcher()
    run_id = store.start_run(now)
    try:
        # 1. fetch
        results = await fetch_all(selected, fx, now, known=lambda uid: store.get(uid) is not None)
        new_uids: set[str] = set()
        for r in results:
            store.record_source(run_id, r.name, r.ok, len(r.items), r.error, r.duration_ms)
            for item in r.items:
                if store.upsert(item, now):
                    new_uids.add(item.uid)
        store.commit()

        pruned = store.prune(now)
        if pruned:
            log.info("pruned %d stale unsent items", pruned)

        # 2. dedup across sources
        # Check every unsent item, not only new ones, so improved rules also clean up the backlog.
        unsent_uids = {s.uid for s in store.unsent()}
        for drop, keep in find_duplicates(unsent_uids, store.active_items(), priority):
            store.mark_duplicate(drop, keep)
        store.commit()

        # 3. relevance: hard filters + rule score on everything not yet sent
        final: dict[str, Entry] = {}
        llm_queue: dict[str, Item] = {}
        ref = refiner or Refiner(profile, examples=owner_examples(store))
        version = ai_version(profile)
        ai_on = use_llm and ref.enabled
        for s in store.unsent():
            a = assess(s.item, profile, now)
            if not a.candidate:
                store.set_score(s.uid, 0.0, a.reasons)
                continue
            # Reuse an earlier AI verdict unless the AI instructions or the profile changed since.
            if s.about is not None and (not ai_on or s.ai_version == version):
                final[s.uid] = Entry(s.uid, s.item, s.score or 0.0, s.about, s.reasons)
                continue
            store.set_score(s.uid, a.score, a.reasons)
            final[s.uid] = Entry(s.uid, s.item, a.score, None, a.reasons)
            if a.score > 0:
                llm_queue[s.uid] = s.item

        # 4. optional LLM refinement
        if ai_on and llm_queue:
            verdicts = await ref.refine(llm_queue, now, fx)
            for uid, v in verdicts.items():
                entry = final[uid]
                score = v.score if v.relevant else 0.0
                about = v.about.strip()
                if note := _deadline_note(v.deadline, entry.item):
                    about = f"{about} {note}"
                reasons = [*entry.reasons, f"ai:{'relevant' if v.relevant else 'not relevant'}"]
                store.set_score(uid, score, reasons, about, version)
                final[uid] = Entry(uid, entry.item, score, about, reasons)
        store.commit()

        def passes(e: Entry) -> bool:
            if e.item.is_online:  # notable organisers (Google, NVIDIA…) get one point of slack
                return e.score >= profile.min_online_score - (1 if notable(e.item, profile) else 0)
            return e.score >= (profile.min_ai_score if e.about is not None else profile.min_score)

        apply_source_penalty(list(final.values()), store.source_rejection())

        entries, deferred = prioritize([e for e in final.values() if passes(e)], profile)
        if profile.explore:  # one near-miss outside the usual pattern, so the digest doesn't narrow
            chosen = {e.uid for e in entries}
            near = sorted((e for e in final.values() if e.uid not in chosen and not passes(e)
                           and e.score >= (profile.min_ai_score if e.about else profile.min_score) - 2
                           and e.score > 0), key=lambda e: -e.score)
            for e in near[: profile.explore]:
                e.explore = True
                entries.append(e)

        # 5. digest + health footer
        statuses: list[SourceStatus] = []
        for r in results:
            h = store.source_health(r.name)
            statuses.append(SourceStatus(
                name=r.name, ok=r.ok, error=r.error,
                consecutive_failures=h.consecutive_failures if h else (0 if r.ok else 1),
                went_silent=h.went_silent if h else False,
            ))
        ai_note = None
        if ai_on and llm_queue:
            if ref.failures and not ref.models_used:
                ai_note = f"🤖 AI scoring unavailable today ({truncate(ref.failures[0], 120)}); showing rule-based picks"
            elif ref.label:
                ai_note = f"🤖 AI picks: {ref.label}" + (" (some batches failed)" if ref.failures else "")
        notes = [n for n in (ai_note,) if n]
        if deferred:
            notes.append(f"📌 Top {len(entries)} shown; {len(deferred)} lower-priority matches held back (they can come another day)")
        digest = build_digest(entries, profile, now, statuses, notes=notes)
        write_preview(digest, data_dir / "last_digest.html")

        # 6. deliver
        if deliver is None and telegram_configured():
            deliver = send_with_buttons
        delivered, delivery_error = False, None
        if dry_run or deliver is None:
            print("\n\n".join(digest.messages))
            why = "dry run" if dry_run else "Telegram not configured (see .env.example)"
            print(f"\n[{why}] not sent; preview: {data_dir / 'last_digest.html'}")
        else:
            try:
                await deliver(fx, digest)
                store.mark_sent(digest.included, now)
                delivered = True
                if deliver is send_with_buttons:
                    await after_delivery(store, profile, fx, digest.included)
            except DeliveryError as e:
                # Cards that did arrive count as sent, so a retry doesn't repeat them.
                if e.delivered:
                    store.mark_sent(e.delivered, now)
                delivery_error = str(e)
                log.error("delivery failed: %s", e)

        result = RunResult(
            run_id=run_id, sources=results, new_items=len(new_uids),
            digest=digest, delivered=delivered, delivery_error=delivery_error,
            llm_failures=ref.failures,
        )
        store.finish_run(run_id, datetime.now(UTC), "ok" if result.exit_code == 0 else "error", {
            "sources_ok": sum(r.ok for r in results), "sources": len(results),
            "new_items": len(new_uids), "digest_items": digest.item_count, "delivered": delivered,
            "scheduled": scheduled,
            "llm": {"models": ref.models_used, "calls": ref.calls, "failures": ref.failures},
        })
        return result
    finally:
        if fetcher is None:
            await fx.aclose()
