"""Command-line interface, logging and the once-a-day schedule guard (SPEC §10–12)."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import logging.handlers
import os
import socket
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, time
from pathlib import Path

from .bot import Bot, run_bot
from .config import (
    DATA_DIR,
    ConfigError,
    load_dotenv,
    load_profile,
    load_sources,
    redact,
)
from .digest import fmt_day
from .http import Fetcher
from .pipeline import fetch_source, run
from .relevance import assess
from .store import Store
from .telegram import (
    DeliveryError,
    Telegram,
    owner_chat_id,
    telegram_chat_ids,
    telegram_configured,
)
from .textutil import local_tz, set_local_tz
from .updates import self_update


def _cmd_run(args: argparse.Namespace) -> int:
    sources, profile = load_sources(), load_profile()
    set_local_tz(profile.timezone)
    if args.scheduled:
        with single_instance(DATA_DIR / "run.lock") as got_lock:
            if not got_lock:
                logging.getLogger(__name__).info("scheduled run skipped: another run is in progress")
                return 0
            store = Store(DATA_DIR / "radar.db")
            try:
                due, why = should_run(store, datetime.now(UTC), args.not_before)
                if why != "offline — will try again at the next check":
                    asyncio.run(_scheduled_extras(store, profile))
            finally:
                store.close()
            if not due:
                logging.getLogger(__name__).info("scheduled run skipped: %s", why)
                return 0
            return _run_now(args, sources, profile)
    return _run_now(args, sources, profile)


async def _scheduled_extras(store: Store, profile) -> None:
    """Every scheduled check: send due reminders (in case the bot isn't running) and, once a day
    when RADAR_AUTO_UPDATE=1, try a safe self-update (R-BOT-4, R-UPD-1)."""
    log = logging.getLogger(__name__)
    if not telegram_configured():
        return
    async with Fetcher() as fx:
        tg = Telegram(fx)
        try:
            await Bot(store, profile, tg, owner_chat_id(), fx).send_due_reminders()
        except Exception as e:  # noqa: BLE001
            log.warning("reminders not sent: %s", e)
        today = datetime.now(UTC).astimezone(local_tz()).date().isoformat()
        if os.environ.get("RADAR_AUTO_UPDATE") == "1" and store.kv_get("update_checked") != today:
            store.kv_set("update_checked", today)
            updated, message = await asyncio.to_thread(self_update)
            log.info("self-update: %s", message)
            if updated or "NOT installed" in message:
                await tg.send(owner_chat_id(), f"🔄 Opportunity Radar: {message}")


def _cmd_bot(_args: argparse.Namespace) -> int:
    profile = load_profile()
    set_local_tz(profile.timezone)
    with single_instance(DATA_DIR / "bot.lock") as got_lock:
        if not got_lock:
            print("the bot is already running", file=sys.stderr)
            return 0
        store = Store(DATA_DIR / "radar.db")

        async def go():
            async with Fetcher() as fx:
                await run_bot(store, profile, fx)

        try:
            asyncio.run(go())
        except KeyboardInterrupt:
            pass
        finally:
            store.close()
    return 0


def _cmd_remind(_args: argparse.Namespace) -> int:
    profile = load_profile()
    set_local_tz(profile.timezone)
    store = Store(DATA_DIR / "radar.db")

    async def go():
        async with Fetcher() as fx:
            return await Bot(store, profile, Telegram(fx), owner_chat_id(), fx).send_due_reminders()

    try:
        print(f"{asyncio.run(go())} reminders sent")
    finally:
        store.close()
    return 0


def _cmd_self_update(_args: argparse.Namespace) -> int:
    updated, message = self_update()
    print(message)
    return 0 if updated or "up to date" in message else 2


def _run_now(args: argparse.Namespace, sources, profile) -> int:
    store = Store(DATA_DIR / "radar.db")
    try:
        result = asyncio.run(run(
            sources=sources, profile=profile, store=store, dry_run=args.dry_run,
            only=args.source or None, use_llm=not args.no_llm, scheduled=args.scheduled,
        ))
    finally:
        store.close()
    ok = sum(s.ok for s in result.sources)
    print(
        f"\nrun #{result.run_id}: sources {ok}/{len(result.sources)} ok · "
        f"{result.new_items} new items · {result.digest.item_count} in digest · "
        + ("delivered to Telegram" if result.delivered else "not delivered")
        + (f" · delivery error: {result.delivery_error}" if result.delivery_error else ""),
        file=sys.stderr,
    )
    return result.exit_code


def _cmd_test_source(args: argparse.Namespace) -> int:
    sources = {c.name: c for c in load_sources()}
    if args.name not in sources:
        print(f"unknown source {args.name!r}. Known: {', '.join(sources)}", file=sys.stderr)
        return 2
    profile = load_profile()
    set_local_tz(profile.timezone)
    now = datetime.now(UTC)

    async def go():
        async with Fetcher() as fx:
            return await fetch_source(sources[args.name], fx, now)

    r = asyncio.run(go())
    for item in r.items:
        a = assess(item, profile, now)
        when = fmt_day(item.deadline or item.published_at) if (item.deadline or item.published_at) else "-"
        verdict = f"DROP ({a.dropped})" if a.dropped else f"score {a.score:.0f} [{', '.join(a.reasons)}]"
        print(f"- {item.title}\n    {when} · {item.location or ('Online' if item.is_online else '-')} · {item.url}\n    {verdict}")
    print(
        f"\n{args.name}: {'ok' if r.ok else 'FAILED'} · {len(r.items)} items · {r.skipped} skipped · "
        f"{r.duration_ms} ms" + (f"\nerror/warnings: {r.error}" if r.error else "")
    )
    return 0 if r.ok else 2


def _cmd_health(_args: argparse.Namespace) -> int:
    store = Store(DATA_DIR / "radar.db")
    try:
        rows = []
        for cfg in load_sources():
            h = store.source_health(cfg.name)
            if not cfg.enabled:
                rows.append((cfg.name, "disabled", "", "", "", ""))
            elif h is None:
                rows.append((cfg.name, "never run", "", "", "", ""))
            else:
                status = "OK" if h.last_ok else "FAIL"
                if h.went_silent:
                    status = "SILENT?"
                last = h.last_success.strftime("%Y-%m-%d %H:%M") if h.last_success else "never"
                rows.append((cfg.name, status, last, str(h.consecutive_failures),
                             f"{h.avg_items_7:.1f}", (h.last_error or "")[:70]))
        delivered = store.last_delivered()
    finally:
        store.close()
    header = ("source", "status", "last success (UTC)", "fails", "avg items", "last error / warning")
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    for r in [header, *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())
    print(f"\nlast delivered: {delivered:%Y-%m-%d %H:%M} UTC" if delivered else "\nno delivery yet")
    return 0


def _cmd_telegram_setup(_args: argparse.Namespace) -> int:
    async def go():
        async with Fetcher() as fx:
            return await telegram_chat_ids(fx)

    try:
        chats = asyncio.run(go())
    except DeliveryError as e:
        print(e, file=sys.stderr)
        return 2
    if not chats:
        print("No messages found. Open your bot in Telegram, send it any message, then run this again.")
        return 1
    for chat_id, name in chats:
        print(f"TELEGRAM_CHAT_ID={chat_id}    # {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description="Daily opportunity digest")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="fetch everything and send the digest")
    p_run.add_argument("--dry-run", action="store_true", help="print the digest; send nothing, mark nothing")
    p_run.add_argument("--source", action="append", help="only run this source (repeatable)")
    p_run.add_argument("--no-llm", action="store_true", help="skip AI scoring")
    p_run.add_argument("--scheduled", action="store_true",
                       help="for launchd: run only if not yet delivered today, after --not-before, and online")
    p_run.add_argument("--not-before", type=lambda s: time.fromisoformat(s), default=time(7, 0),
                       help="earliest local time (profile timezone) for a scheduled run (default 07:00)")
    p_run.set_defaults(func=_cmd_run)

    p_test = sub.add_parser("test-source", help="fetch one source and show how each item is judged")
    p_test.add_argument("name")
    p_test.set_defaults(func=_cmd_test_source)

    sub.add_parser("health", help="per-source status history").set_defaults(func=_cmd_health)
    sub.add_parser("telegram-setup", help="find your Telegram chat id").set_defaults(func=_cmd_telegram_setup)
    sub.add_parser("bot", help="run the interactive Telegram bot (buttons, finds, chat)").set_defaults(func=_cmd_bot)
    sub.add_parser("remind", help="send due reminders now").set_defaults(func=_cmd_remind)
    sub.add_parser("self-update", help="install the latest version if its tests pass").set_defaults(func=_cmd_self_update)

    args = parser.parse_args(argv)
    load_dotenv()
    setup_logging(DATA_DIR / "logs", verbose=args.verbose)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2



# ------------------------------------------------------------------ logging setup

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("source", "items", "duration_ms", "ok", "error", "run_id"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        if "error" in entry and isinstance(entry["error"], str):
            entry["error"] = redact(entry["error"])
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "radar.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(console)

    # These libraries log full request URLs at INFO; Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ schedule

PROBE_HOSTS = (("api.telegram.org", 443), ("www.google.com", 443))


def online(timeout: float = 8.0) -> bool:
    for host, port in PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def should_run(
    store: Store, now: datetime, not_before: time, is_online: Callable[[], bool] = online
) -> tuple[bool, str]:
    local = now.astimezone(local_tz())
    if local.time() < not_before:
        return False, f"before {not_before:%H:%M} local time"
    if store.delivered_on(local.date(), scheduled_only=True):
        return False, f"already delivered today ({local:%Y-%m-%d})"
    if not is_online():
        return False, "offline — will try again at the next check"
    return True, "due"


@contextmanager
def single_instance(lock_path: Path):
    """Yields True if this process holds the lock, False if another run is in progress."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)
