"""Telegram Bot API: digest delivery, buttons, edits, files and update polling (SPEC §8, §8b)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from .config import redact, secret
from .digest import Card, Digest
from .http import Fetcher, FetchError

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
CARD_PAUSE = 0.4  # seconds between cards: Telegram allows about one message per second per chat


class DeliveryError(Exception):
    def __init__(self, message: str, delivered: list[str] | None = None):
        super().__init__(message)
        self.delivered = delivered or []  # cards that did reach the chat before the failure


def telegram_configured() -> bool:
    return bool(secret("TELEGRAM_BOT_TOKEN") and secret("TELEGRAM_CHAT_ID"))


def owner_chat_id() -> str | None:
    return secret("TELEGRAM_CHAT_ID")


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Inline keyboard from rows of (label, callback_data); callback data must stay ≤ 64 bytes."""
    for row in rows:
        for _, data in row:
            if len(data.encode()) > 64:
                raise ValueError(f"callback data too long: {data!r}")
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


class Telegram:
    """Thin client over the Bot API. Errors never contain the token."""

    def __init__(self, fx: Fetcher, token: str | None = None):
        self.fx = fx
        self.token = token or secret("TELEGRAM_BOT_TOKEN")
        if not self.token:
            raise DeliveryError("TELEGRAM_BOT_TOKEN is not set")

    async def call(self, method: str, payload: dict | None = None, **kwargs: Any) -> Any:
        url = TELEGRAM_API.format(token=self.token, method=method)
        try:
            data = await self.fx.post_json(url, payload or {}, **kwargs)
        except FetchError as e:  # the URL contains the token
            raise DeliveryError(redact(f"Telegram {method} failed: {e}")) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else data
            raise DeliveryError(f"Telegram {method} rejected: {desc}")
        return data.get("result")

    async def send(self, chat_id: str, text: str, markup: dict | None = None, silent: bool = False) -> int:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        if silent:
            payload["disable_notification"] = True
        return int((await self.call("sendMessage", payload))["message_id"])

    async def edit(self, chat_id: str, message_id: int, text: str, markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "reply_markup": markup or {"inline_keyboard": []}}
        try:
            await self.call("editMessageText", payload)
        except DeliveryError as e:
            if "message is not modified" not in str(e):
                raise

    async def answer(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def send_file(self, chat_id: str, filename: str, content: bytes, caption: str = "") -> None:
        url = TELEGRAM_API.format(token=self.token, method="sendDocument")
        try:
            resp = await self.fx.request(
                "POST", url, data={"chat_id": chat_id, "caption": caption},
                files={"document": (filename, content, "text/calendar")},
            )
            data = resp.json()
        except (FetchError, ValueError) as e:
            raise DeliveryError(redact(f"Telegram sendDocument failed: {e}")) from None
        if not data.get("ok"):
            raise DeliveryError(f"Telegram sendDocument rejected: {data.get('description')}")

    async def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self.call("getUpdates", payload, timeout=httpx.Timeout(timeout + 15))
        return result if isinstance(result, list) else []


async def send_telegram(fx: Fetcher, digest: Digest,
                        markup_for: Callable[[Card], dict | None] | None = None) -> None:
    """Header (with a notification), then each card silently with its buttons, then the footer."""
    chat_id = owner_chat_id()
    if not chat_id or not secret("TELEGRAM_BOT_TOKEN"):
        raise DeliveryError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    tg = Telegram(fx)
    delivered: list[str] = []
    parts = digest.parts()
    for i, (text, card) in enumerate(parts, 1):
        try:
            if card:
                await fx.pause(CARD_PAUSE)
            await tg.send(chat_id, text, markup_for(card) if card and markup_for else None, silent=i > 1)
        except DeliveryError as e:
            raise DeliveryError(f"message {i}/{len(parts)}: {e}", delivered) from None
        if card:
            delivered.append(card.uid)


async def telegram_chat_ids(fx: Fetcher) -> list[tuple[str, str]]:
    """(chat_id, name) of chats that recently messaged the bot — first-time setup helper."""
    if not secret("TELEGRAM_BOT_TOKEN"):
        raise DeliveryError("set TELEGRAM_BOT_TOKEN first (from @BotFather)")
    result = await Telegram(fx).call("getUpdates")
    seen: dict[str, str] = {}
    for upd in result or []:
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            seen[str(chat["id"])] = name or chat.get("username") or "?"
    return list(seen.items())
