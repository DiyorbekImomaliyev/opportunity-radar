"""Configuration loading and validation (SPEC §13)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("RADAR_DATA_DIR") or ROOT / "data")  # override to test with a fresh database


class ConfigError(Exception):
    pass


DEFAULT_JOB_TITLE_KEYWORDS = [
    "intern", "internship", "trainee", "graduate", "junior", "entry level",
    "student", "fellowship", "co-op",
    "praktikum", "praktikant", "werkstudent", "absolvent", "berufseinsteiger",  # German
    "stage", "stagiaire", "becario", "tirocinio",  # French, Spanish, Italian
]


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    about: str = ""
    interests: dict[str, float] = Field(default_factory=dict)  # topics: an item must match one
    formats: dict[str, float] = Field(default_factory=dict)  # workshop, conference…: boost only
    timezone: str = "UTC"  # IANA name, e.g. "Europe/London", "America/New_York"
    home_label: str = "📍 Near you"  # section title for in-person events in your area
    locations: list[str] = Field(default_factory=list)  # your city/region keywords
    exclude: list[str] = Field(default_factory=list)
    job_title_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_JOB_TITLE_KEYWORDS))
    min_score: float = 3
    min_ai_score: float = 6  # threshold when the AI judged the item (its scale is stricter)
    online_events: bool = True  # online workshops/meetups (they must still reach min_online_score)
    min_online_score: float = 8  # online items need this score: only strong ones get through
    max_items: int = 15  # daily cap; the rest compete again later
    # Slots reserved per kind so one kind can't fill the whole digest; unused slots go to the next best.
    quotas: dict[str, int] = Field(default_factory=lambda: {"in_person": 6, "jobs": 3, "posts": 2, "online": 4})
    max_online: int = 6  # at most this many online items (hackathons + events) per digest
    # Organisers whose events are worth it even online; matched in organizer, title and text.
    notable_organizers: list[str] = Field(default_factory=lambda: [
        "google", "microsoft", "nvidia", "aws", "amazon", "meta", "openai", "anthropic",
        "hugging face", "ibm", "apple", "github", "gitlab", "databricks", "y combinator", "mlh",
        "intel", "amd", "oracle", "salesforce", "stripe", "cloudflare", "mistral", "cohere", "deepmind",
    ])
    # Community groups named after a company are not the company ("Google Developer Group at FIU").
    notable_exclude: list[str] = Field(default_factory=lambda: [
        "google developer group", "google developer student club", "gdg", "microsoft learn student",
        "aws user group", "aws community", "student ambassador",
    ])
    remote_ok: bool = True
    relocation_ok: bool = False  # jobs/programs abroad from sources tagged `international`
    needs_visa_sponsorship: bool = False  # drop jobs that say no sponsorship / nationals only
    languages: list[str] = Field(default_factory=lambda: ["english"])  # jobs demanding others are dropped
    # Never junior, whatever a job board's level filter says.
    job_exclude_keywords: list[str] = Field(default_factory=lambda: [
        "senior", "sr", "lead", "principal", "staff", "manager", "head of", "director", "architect", "vp",
    ])
    lookahead_days: int = 120
    max_post_age_days: int = 7
    max_program_age_days: int = 30
    remind_every_days: int = 3  # ⭐ saved items are reminded this often until done or removed
    explore: int = 1  # "🎲 Something different" slots per digest (0 = off)
    # AI scoring runs when GEMINI_API_KEY is set. Models are tried in order.
    llm_models: list[str] = Field(default_factory=lambda: ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])

    @field_validator("interests", "formats")
    @classmethod
    def _lower_keys(cls, v: dict[str, float]) -> dict[str, float]:
        return {k.lower().strip(): float(w) for k, w in v.items()}

    @model_validator(mode="after")
    def _quotas_fit(self) -> Profile:
        # Guaranteed slots must fit in the digest, or the kinds ranked last get none (seen live).
        if sum(self.quotas.values()) > self.max_items:
            raise ValueError(f"quotas add up to {sum(self.quotas.values())}, more than max_items ({self.max_items})")
        return self

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r} (use an IANA name like 'Europe/London')") from e
        return v

    @field_validator("locations", "exclude", "job_title_keywords", "notable_organizers", "notable_exclude",
                     "languages", "job_exclude_keywords")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [x.lower().strip() for x in v if x.strip()]


class SourceConfig(BaseModel):
    """One entry in sources.yaml. Adapter-specific params live in `params`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as e:
        example = path.with_name(path.stem + ".example" + path.suffix)
        hint = f" — copy {example.name} to {path.name} and edit it" if example.exists() else ""
        raise ConfigError(f"config file not found: {path}{hint}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e


OVERRIDES_PATH = CONFIG_DIR / "overrides.yaml"
OVERRIDE_KEYS = {"exclude", "interests", "quotas", "notable_organizers", "linkedin_follow", "linkedin_unfollow"}


def load_overrides(path: Path | None = None) -> dict:
    """Changes the owner approved in Telegram (SPEC R-BOT-6). Missing file = no changes."""
    path = path or OVERRIDES_PATH
    if not path.exists():
        return {}
    data = _read_yaml(path)
    return {k: v for k, v in (data or {}).items() if k in OVERRIDE_KEYS}


def save_overrides(data: dict, path: Path | None = None) -> None:
    path = path or OVERRIDES_PATH
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Written by the Telegram bot (approved changes). Delete a line to undo it.\n"
                   + yaml.safe_dump({k: v for k, v in data.items() if v}, sort_keys=True, allow_unicode=True))
    tmp.replace(path)


def apply_profile_overrides(raw: dict, ov: dict) -> dict:
    raw = dict(raw)
    for key in ("exclude", "notable_organizers"):
        if ov.get(key):
            raw[key] = list(dict.fromkeys([*(raw.get(key) or []), *ov[key]]))
    for key in ("interests", "quotas"):
        if ov.get(key):
            raw[key] = {**(raw.get(key) or {}), **ov[key]}
    return raw


def load_profile(path: Path | None = None, overrides: dict | None = None) -> Profile:
    """The profile; when loading the default file, approved overrides are merged on top."""
    default = path is None
    path = path or CONFIG_DIR / "profile.yaml"
    raw = _read_yaml(path)
    if overrides is not None or default:
        raw = apply_profile_overrides(raw, load_overrides() if overrides is None else overrides)
    try:
        return Profile.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e


def apply_source_overrides(entries: list, ov: dict) -> list:
    """Follow/unfollow LinkedIn pages on the first `linkedin_posts` source."""
    follow, unfollow = ov.get("linkedin_follow") or [], set(ov.get("linkedin_unfollow") or [])
    if not (follow or unfollow):
        return entries
    out = [dict(e) if isinstance(e, dict) else e for e in entries]
    target = next((e for e in out if isinstance(e, dict) and e.get("type") == "linkedin_posts"), None)
    if target is None:
        target = {"name": "linkedin-posts", "type": "linkedin_posts", "pages": []}
        out.append(target)
    pages = [p for p in [*(target.get("pages") or []), *follow] if p not in unfollow]
    target["pages"] = list(dict.fromkeys(pages))
    return out


def load_sources(path: Path | None = None, overrides: dict | None = None) -> list[SourceConfig]:
    from .sources import REGISTRY  # avoid import cycle

    default = path is None
    path = path or CONFIG_DIR / "sources.yaml"
    raw = _read_yaml(path)
    entries = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: expected a top-level 'sources:' list")
    if overrides is not None or default:
        entries = apply_source_overrides(entries, load_overrides() if overrides is None else overrides)

    out: list[SourceConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: sources[{i}] must be a mapping")
        entry = dict(entry)
        base = {k: entry.pop(k) for k in ("name", "type", "enabled") if k in entry}
        label = base.get("name", f"sources[{i}]")
        try:
            cfg = SourceConfig.model_validate({**base, "params": entry})
        except ValidationError as e:
            raise ConfigError(f"{path}: source {label!r}: {e}") from e
        if cfg.name in seen:
            raise ConfigError(f"{path}: duplicate source name {cfg.name!r}")
        seen.add(cfg.name)
        adapter_cls = REGISTRY.get(cfg.type)
        if adapter_cls is None:
            raise ConfigError(
                f"{path}: source {cfg.name!r} has unknown type {cfg.type!r} "
                f"(known: {', '.join(sorted(REGISTRY))})"
            )
        try:
            adapter_cls.Params.model_validate(cfg.params)
        except ValidationError as e:
            raise ConfigError(f"{path}: source {cfg.name!r} ({cfg.type}): {e}") from e
        out.append(cfg)
    return out


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing environment wins."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def secret(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY")


def redact(text: str) -> str:
    """Replace every configured secret value in text (errors, logs, digest footer)."""
    for name in SECRET_NAMES:
        value = secret(name)
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    return text
