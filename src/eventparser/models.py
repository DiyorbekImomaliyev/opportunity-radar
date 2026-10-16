"""Domain model and URL identity (SPEC §2)."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from .textutil import clean_text, truncate

SUMMARY_MAX = 900  # room for a program's eligibility section plus its teaser


class Kind(StrEnum):
    EVENT = "event"
    HACKATHON = "hackathon"
    JOB = "job"
    POST = "post"
    PROGRAM = "program"


class Item(BaseModel):
    source: str
    kind: Kind
    title: str
    url: str
    summary: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    published_at: datetime | None = None
    location: str | None = None
    is_online: bool | None = None
    organizer: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = clean_text(v)
        if not v:
            raise ValueError("title is empty")
        return v

    @field_validator("summary")
    @classmethod
    def _summary(cls, v: str) -> str:
        return truncate(clean_text(v), SUMMARY_MAX)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if v.startswith("//"):
            v = "https:" + v
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"not an absolute http(s) URL: {v!r}")
        return v

    @field_validator("starts_at", "ends_at", "published_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return v.astimezone(UTC)  # one representation everywhere (R-MODEL-1)

    @field_validator("location", "organizer")
    @classmethod
    def _opt_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return clean_text(v) or None

    @property
    def canonical_url(self) -> str:
        return canonical_url(self.url)

    @property
    def uid(self) -> str:
        return uid_for(self.url)

    @property
    def deadline(self) -> datetime | None:
        """The date that matters most for acting on this item."""
        if self.kind == Kind.HACKATHON:
            return self.ends_at or self.starts_at
        return self.starts_at


# ------------------------------------------------------------------ canonical urls

TRACKING_PARAMS = {
    "fbclid", "gclid", "ref", "trk", "trackingid", "refid", "position", "pagenum",
    "aff", "aff_id", "mc_cid", "mc_eid", "igshid",
}

_LI_JOB = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})")
_LI_ACTIVITY = re.compile(r"(?:activity[:-])(\d{15,})")
_EB_EVENT = re.compile(r"/e/(?:[^/?#]*-)?(\d{9,})")


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        if m := _LI_JOB.search(parts.path):
            return f"https://linkedin.com/jobs/view/{m.group(1)}"
        if m := _LI_ACTIVITY.search(parts.path):
            return f"https://linkedin.com/feed/update/urn:li:activity:{m.group(1)}"
        host = "linkedin.com"  # country subdomains (qa.linkedin.com) are the same page

    # eventbrite.com / .sg / .co.uk … list the same event under different domains and slugs
    if re.match(r"^eventbrite\.[a-z.]+$", host) and (m := _EB_EVENT.search(parts.path)):
        return f"https://eventbrite.com/e/{m.group(1)}"

    netloc = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def uid_for(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:16]
