"""Adapter contract (SPEC §4)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, ValidationError

from ..http import Fetcher
from ..models import Item
from ..textutil import local_tz

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    REGISTRY[cls.type] = cls
    return cls


def summarize_errors(errors: list[tuple[str, str]], noun: str) -> str:
    """[(what, reason)] → readable text; identical reasons are grouped ("blocked … (3 pages)")."""
    groups: dict[str, list[str]] = {}
    for label, reason in errors:
        groups.setdefault(reason, []).append(label)
    parts = [
        f"{reason} ({len(labels)} {noun}s)" if len(labels) > 1 else f"{labels[0]}: {reason}"
        for reason, labels in groups.items()
    ]
    return "; ".join(parts)


class SourceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Extra tags for every item of this source, e.g. [international] for relocation-friendly boards.
    tags: list[str] = []


class Source(ABC):
    type: ClassVar[str]
    Params: ClassVar[type[SourceParams]] = SourceParams

    def __init__(self, name: str, params: dict[str, Any] | None = None, *, now: datetime | None = None):
        self.name = name
        self.p = self.Params.model_validate(params or {})
        self.now = now or datetime.now(UTC)
        self.skipped = 0
        self.warnings: list[str] = []
        # Set by the pipeline: True for items already in the store, so sources can skip costly
        # per-item requests for things they've already read (R-SRC-5 details).
        self.known: Callable[[str], bool] = lambda uid: False

    @abstractmethod
    async def fetch(self, fx: Fetcher) -> list[Item]: ...

    @property
    def tz(self) -> ZoneInfo:
        """The source's `timezone` param if set, else the owner's (profile `timezone`)."""
        name = getattr(self.p, "timezone", None)
        return ZoneInfo(name) if name else local_tz()

    def make(self, **fields: Any) -> Item | None:
        """Build an Item; a malformed record is skipped and counted, never fatal (R-SRC-9)."""
        if self.p.tags:
            fields["tags"] = [*fields.get("tags", []), *self.p.tags]
        try:
            return Item(source=self.name, **fields)
        except (ValidationError, TypeError, ValueError) as e:
            self.skipped += 1
            log.debug("%s: skipped malformed record: %s", self.name, e)
            return None

    def collect(self, records: list[Item | None]) -> list[Item]:
        return [r for r in records if r is not None]
