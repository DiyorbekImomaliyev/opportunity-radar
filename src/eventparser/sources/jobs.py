"""Job-board adapters: Arbeitnow (Germany/Europe) and remote-job APIs (SPEC R-SRC-11, R-SRC-12)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from ..http import Fetcher
from ..models import Item, Kind
from ..textutil import html_to_text, parse_iso
from .base import Source, SourceParams, register

VISA_TAG = "visa:sponsored"
_VISA = re.compile(
    r"visa[\s-]*sponsor|sponsor(?:ship)?\s+(?:of\s+)?(?:your\s+|a\s+)?(?:work\s+)?visa|relocation\s+(?:support|package|assistance)",
    re.I,
)
_NEGATION = re.compile(r"\b(?:no|not|unable|cannot|can['’]?t|without|don['’]?t|do\s+not|won['’]?t)\b[^.]{0,40}$", re.I)


# Application-form templates (Workday…) list fields as "Relocation VISA Sponsorship Travel
# Requirements …" with the value after the label — or nothing at all (seen live, MSD Gulf).
_FIELD_ANSWER = re.compile(r"\s*:?\s*(yes|no|not applicable|n/a|none)\b", re.I)
_NEXT_LABEL = re.compile(r"\s+([A-Z][a-z]+)")
_PROSE_WORDS = {"available", "provided", "offered", "and", "is", "for", "to", "included", "support"}


def _field_value(text: str, end: int) -> str | None:
    """For a visa phrase ending at `end`: 'yes'/'no' when it is a form field with an answer, ''
    when it is an empty field label (another Title Case label follows), None for ordinary prose."""
    while end < len(text) and text[end].isalnum():  # finish the word ("Sponsor" → "Sponsorship")
        end += 1
    if m := _FIELD_ANSWER.match(text, end):
        return "yes" if m.group(1).lower() == "yes" else "no"
    if (m := _NEXT_LABEL.match(text, end)) and m.group(1).lower() not in _PROSE_WORDS:
        return ""
    return None


def offers_visa(text: str) -> bool:
    """True if the text offers visa sponsorship/relocation and doesn't negate it ("no visa sponsorship")."""
    for m in _VISA.finditer(text or ""):
        field = _field_value(text, m.end())
        if field is not None:
            if field == "yes":
                return True
            continue
        if not _NEGATION.search(text[max(0, m.start() - 60) : m.start()]):
            return True
    return False


def _field_says_no(text: str) -> bool:
    return any(_field_value(text, m.end()) == "no" for m in _VISA.finditer(text or ""))


RESTRICTED_TAG = "visa:restricted"
_NATIONS = r"(?:EU|EEA|UK|US|U\.S\.|UAE|GCC|Saudi|Emirati|Qatari|Kuwaiti|Omani|Bahraini|local|national)"
_RESTRICTED = re.compile(
    r"\b(?:no|not|unable to|cannot|can['’]?t|do(?:es)? not|won['’]?t|will not)\b[^.]{0,40}\bsponsor"
    r"|sponsorship\s+(?:is\s+)?(?:not|unavailable)"
    r"|\b(?:must|need to|should|required to)\s+(?:already\s+)?(?:have|hold|possess)\s+[^.]{0,30}?"
    r"(?:right to work|work permit|work authori[sz]ation|residence permit|residency)"
    r"|\b(?:right to work|work permit|work authori[sz]ation)\b[^.]{0,20}\b(?:is\s+)?(?:required|mandatory|essential)"
    rf"|\b(?:only\s+(?:open\s+to|for|available\s+to)?\s*){_NATIONS}\s+(?:citizens|nationals|residents|passport)"
    rf"|\b{_NATIONS}\s+(?:citizens|nationals)\s+only"
    r"|\b(?:open\s+to|for|reserved\s+for)\s+(?:UAE|Saudi|Qatari|Emirati|GCC)\s+nationals"
    r"|\bEmiratis?\s+only|\b(?:Emiratisation|Emiratization|Saudi[sz]ation|Tawteen|Nafis|Tamheer)\b"
    r"|\b(?:citizenship|security clearance)\s+(?:is\s+)?required",
    re.I,
)
_SENTENCE = re.compile(r"[^.!?\n]*(?:visa|sponsor|citizen|nationals?\b|work permit|right to work|work authori|"
                       r"relocat|Emirati|Saudi[sz]ation|Nafis|Tamheer)[^.!?\n]*[.!?]?", re.I)


def restricts_foreigners(text: str) -> bool:
    """True if the text rules out applicants who need a visa: "no sponsorship", "must have the
    right to work", "UAE nationals only", Emiratisation/Saudization programmes."""
    return bool(_RESTRICTED.search(text or "")) or _field_says_no(text)


def visa_tags(text: str) -> list[str]:
    """[RESTRICTED_TAG], [VISA_TAG] or [] — a restriction wins over an offer in the same text."""
    if restricts_foreigners(text):
        return [RESTRICTED_TAG]
    return [VISA_TAG] if offers_visa(text) else []


def eligibility_sentences(text: str, limit: int = 300) -> str:
    """The sentences of a job description about visas, citizenship or work permits."""
    out: list[str] = []
    for m in _SENTENCE.finditer(text or ""):
        sentence = " ".join(m.group(0).split())
        if sentence and sentence not in out:
            out.append(sentence)
        if sum(map(len, out)) > limit:
            break
    return " ".join(out)[:limit]


# ------------------------------------------------------------------ arbeitnow


class ArbeitnowParams(SourceParams):
    max_pages: int = 3
    visa_only: bool = False  # keep only jobs whose description offers visa sponsorship/relocation


@register
class ArbeitnowSource(Source):
    """https://www.arbeitnow.com/api/job-board-api — jobs in Germany and Europe, paginated."""

    type = "arbeitnow"
    Params = ArbeitnowParams
    p: ArbeitnowParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        url: str | None = "https://www.arbeitnow.com/api/job-board-api"
        items: list[Item | None] = []
        for _ in range(self.p.max_pages):
            if not url:
                break
            data = await fx.get_json(url)
            for j in data.get("data", []) if isinstance(data, dict) else []:
                if isinstance(j, dict):
                    items.append(self._item(j))
            url = (data.get("links") or {}).get("next")
        return self.collect(items)

    def _item(self, j: dict) -> Item | None:
        description = html_to_text(j.get("description"))
        visa = offers_visa(description)
        if self.p.visa_only and not visa:
            return None
        remote = bool(j.get("remote"))
        created = j.get("created_at")
        return self.make(
            kind=Kind.JOB,
            title=j.get("title") or "",
            url=j.get("url") or "",
            summary=" · ".join(b for b in (j.get("company_name"), j.get("location"), description) if b),
            published_at=datetime.fromtimestamp(created, UTC) if isinstance(created, (int, float)) else None,
            location=j.get("location"),
            is_online=remote or None,
            organizer=j.get("company_name"),
            tags=[*(j.get("tags") or []), *(j.get("job_types") or []), *([VISA_TAG] if visa else [])],
        )


# ------------------------------------------------------------------ remote-job APIs


class RemoteJobsParams(SourceParams):
    provider: Literal["remotive", "jobicy", "himalayas"]
    search: str  # keyword (Remotive/Himalayas) or tag (Jobicy), e.g. "data analyst"
    # Where you live, as the boards write it: a job is kept only if open to one of these
    # (empty restriction = worldwide). Example for Germany: worldwide, anywhere, emea, europe, germany.
    allowed_regions: list[str] = ["worldwide", "anywhere"]
    limit: int = 50


def _short_region(region: str, allowed: list[str]) -> str:
    """'Afghanistan, Algeria, … Germany, … (77)' → '77 countries incl. Germany'."""
    parts = [p.strip() for p in region.split(",") if p.strip()]
    if len(parts) <= 5:
        return region.strip()
    mine = next((p for p in parts if any(a.lower() in p.lower() for a in allowed)), parts[0])
    return f"{len(parts)} countries incl. {mine}"


def _open_to(restriction: str, allowed: list[str]) -> bool:
    r = restriction.strip().lower()
    return not r or any(a.lower() in r for a in allowed)


@register
class RemoteJobsSource(Source):
    """Remote jobs from Remotive, Jobicy or Himalayas, filtered to roles open where you live."""

    type = "remote_jobs"
    Params = RemoteJobsParams
    p: RemoteJobsParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        p = self.p
        if p.provider == "remotive":
            data = await fx.get_json("https://remotive.com/api/remote-jobs", params={"search": p.search, "limit": p.limit})
            jobs = [self._remotive(j) for j in data.get("jobs", [])]
        elif p.provider == "jobicy":
            data = await fx.get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": p.limit, "tag": p.search})
            jobs = [self._jobicy(j) for j in data.get("jobs", [])]
        else:
            data = await fx.get_json("https://himalayas.app/jobs/api", params={"limit": p.limit, "q": p.search})
            jobs = [self._himalayas(j) for j in data.get("jobs", [])]
        return self.collect(jobs)

    def _job(self, *, title, url, company, region, text, published, level_entry: bool) -> Item | None:
        if not _open_to(region, self.p.allowed_regions):
            return None  # not open to applicants where you live
        region = _short_region(region, self.p.allowed_regions)
        tags = ["remote", f"open to: {region or 'worldwide'}"]
        if level_entry:
            tags.append("level:entry")
        if offers_visa(text):
            tags.append(VISA_TAG)
        return self.make(
            kind=Kind.JOB, title=title or "", url=url or "", summary=" · ".join(b for b in (company, region, text) if b),
            published_at=published, location=f"Remote ({region or 'worldwide'})", is_online=True,
            organizer=company, tags=tags,
        )

    def _remotive(self, j: dict) -> Item | None:
        return self._job(
            title=j.get("title"), url=j.get("url"), company=j.get("company_name"),
            region=j.get("candidate_required_location") or "", text=html_to_text(j.get("description")),
            published=parse_iso(j.get("publication_date")), level_entry=False,
        )

    def _jobicy(self, j: dict) -> Item | None:
        level = (j.get("jobLevel") or "").lower()
        return self._job(
            title=html_to_text(j.get("jobTitle")), url=j.get("url"), company=j.get("companyName"),
            region=(j.get("jobGeo") or "").replace("Anywhere", "worldwide"),
            text=html_to_text(j.get("jobExcerpt") or j.get("jobDescription")),
            published=parse_iso(j.get("pubDate")), level_entry=level in ("junior", "entry", "entry-level", "intern"),
        )

    def _himalayas(self, j: dict) -> Item | None:
        seniority = [s.lower() for s in j.get("seniority") or []]
        restrictions = j.get("locationRestrictions") or []
        published = j.get("pubDate")
        return self._job(
            title=j.get("title"), url=j.get("applicationLink") or j.get("guid") or "", company=j.get("companyName"),
            region=", ".join(r if isinstance(r, str) else str(r.get("name", "")) for r in restrictions),
            text=html_to_text(j.get("excerpt") or j.get("description")),
            published=datetime.fromtimestamp(published, UTC) if isinstance(published, (int, float)) else parse_iso(published),
            level_entry=any("entry" in s or "intern" in s for s in seniority),
        )
