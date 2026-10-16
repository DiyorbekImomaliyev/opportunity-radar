"""LinkedIn without login: public job search and public company/profile posts (SPEC R-SRC-5, R-SRC-6)."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import unquote, urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from ..http import BlockedError, Fetcher
from ..models import Item, Kind
from ..textutil import clean_text, parse_iso, parse_relative_age, truncate
from .base import Source, SourceParams, register, summarize_errors
from .events import iter_typed, ld_blocks
from .jobs import eligibility_sentences, visa_tags

GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_POSTING_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
ENTRY_LEVEL_TAG = "level:entry"
_JOB_ID = re.compile(r"jobPosting:(\d+)|/jobs/view/(?:[^/?]*-)?(\d{6,})")


class LinkedInJobsParams(SourceParams):
    keywords: list[str]
    locations: list[str] = ["Worldwide"]
    posted_within_hours: int = 72
    max_pages: int = 2
    page_size: int = 10
    filters: dict[str, str] = {}  # extra query params, e.g. {"f_E": "1,2"} internship+entry, {"f_WT": "2"} remote
    # Run at most this many (keyword, location) queries per run, rotating daily through all of
    # them, so many countries fit under LinkedIn's rate limit. Keep posted_within_hours ≥ the cycle.
    max_queries: int | None = None
    # Open up to this many *new* job pages per run to read visa/citizenship lines (R-SRC-5).
    details: int = 0


def job_description(html: str) -> str:
    """Plain text of a guest job-posting page's description ('' if missing)."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".show-more-less-html__markup, .description__text")
    return clean_text(el.get_text(" ")) if el else ""


def parse_job_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.base-card, div.base-search-card, li > div.job-search-card")
    if not cards:
        cards = [li for li in soup.find_all("li") if li.select_one(".base-search-card__title")]
    out: list[dict] = []
    seen: set[str] = set()
    for card in cards:
        rec = _parse_card(card)
        if rec and rec["job_id"] not in seen:
            seen.add(rec["job_id"])
            out.append(rec)
    return out


def _text(card: Tag, selector: str) -> str:
    el = card.select_one(selector)
    return clean_text(el.get_text(" ")) if el else ""


def _parse_card(card: Tag) -> dict | None:
    urn = card.get("data-entity-urn") or ""
    link = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
    href = link.get("href", "") if link else ""
    m = _JOB_ID.search(str(urn)) or _JOB_ID.search(href)
    if not m:
        return None
    job_id = m.group(1) or m.group(2)
    title = _text(card, ".base-search-card__title") or (clean_text(link.get_text(" ")) if link else "")
    time_el = card.select_one("time")
    return {
        "job_id": job_id,
        "title": title,
        "company": _text(card, ".base-search-card__subtitle"),
        "location": _text(card, ".job-search-card__location"),
        "posted": time_el.get("datetime") if time_el else None,
        "benefits": _text(card, ".job-search-card__benefits, .result-benefits__text"),
        "url": f"https://www.linkedin.com/jobs/view/{job_id}",
    }


@register
class LinkedInJobsSource(Source):
    type = "linkedin_jobs"
    Params = LinkedInJobsParams
    p: LinkedInJobsParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        items: dict[str, Item] = {}
        errors: list[tuple[str, str]] = []
        queries = self.queries()
        for keyword, location in queries:
            try:
                for page in range(self.p.max_pages):
                    params = {
                        "keywords": keyword,
                        "location": location,
                        "f_TPR": f"r{self.p.posted_within_hours * 3600}",
                        "start": str(page * self.p.page_size),
                        **self.p.filters,
                    }
                    html = await fx.get_text(f"{GUEST_API}?{urlencode(params)}")
                    cards = parse_job_cards(html)
                    for rec in cards:
                        item = self._item(rec, keyword)
                        if item and item.uid not in items:
                            items[item.uid] = item
                    if len(cards) < self.p.page_size:
                        break
            except Exception as e:
                errors.append((f"{keyword!r} in {location}", str(e)))
                if "blocked" in str(e).lower():
                    break  # same block applies to every query
        if errors and not items:
            raise RuntimeError(summarize_errors(errors, "query"))
        if errors:
            self.warnings.append(summarize_errors(errors, "query"))
        found = _dedup_reposts(list(items.values()))
        if self.p.details:
            found = await self._add_details(fx, found)
        return found

    def queries(self) -> list[tuple[str, str]]:
        """Every (keyword, location) pair, or today's slice of them when `max_queries` is set."""
        pairs = [(k, loc) for k in self.p.keywords for loc in self.p.locations]
        n = self.p.max_queries
        if not n or n >= len(pairs):
            return pairs
        rounds = -(-len(pairs) // n)
        start = (self.now.date().toordinal() % rounds) * n
        return (pairs + pairs)[start : start + n]

    async def _add_details(self, fx: Fetcher, items: list[Item]) -> list[Item]:
        """Read the description of up to `details` jobs not seen before: visa and citizenship
        lines become tags (visa:sponsored / visa:restricted) and lead the summary."""
        out, opened, failed = [], 0, 0
        for item in items:
            if opened >= self.p.details or self.known(item.uid) or failed >= 2:
                out.append(item)
                continue
            opened += 1
            job_id = item.url.rstrip("/").rsplit("/", 1)[-1]
            try:
                text = job_description(await fx.get_text(JOB_POSTING_API.format(job_id=job_id)))
            except Exception as e:  # best effort: the card is still useful
                failed += 1
                self.warnings.append(f"job details not read: {e}")
                out.append(item)
                continue
            rules = eligibility_sentences(text)
            extra = f"Eligibility: {rules}" if rules else truncate(text, 300)
            out.append(item.model_copy(update={
                "summary": " — ".join(x for x in (extra, item.summary) if x),
                "tags": [*item.tags, *visa_tags(text)],
            }))
        return out

    def _level_tags(self) -> list[str]:
        """LinkedIn's experience filter (f_E: 1 internship, 2 entry level, 3 associate) vouches for
        junior roles whose titles don't say so ("Data Analyst")."""
        levels = set(self.p.filters.get("f_E", "").split(","))
        return [ENTRY_LEVEL_TAG] if levels and levels <= {"1", "2", "3"} else []

    def _item(self, rec: dict, keyword: str) -> Item | None:
        summary = " · ".join(b for b in (rec["company"], rec["location"], rec["benefits"]) if b)
        return self.make(
            kind=Kind.JOB,
            title=rec["title"],
            url=rec["url"],
            summary=summary,
            published_at=parse_iso(rec["posted"]),
            location=rec["location"] or None,
            # f_WT=2 is LinkedIn's "Remote" filter: every result of such a query is remote.
            is_online=("2" in self.p.filters.get("f_WT", "").split(","))
            or "remote" in rec["location"].lower() or None,
            organizer=rec["company"] or None,
            tags=[f"search:{keyword}", *self._level_tags()],
        )


def _dedup_reposts(items: list[Item]) -> list[Item]:
    """One job per (title, company): agencies post the same role under many branch names
    ("Data Analyst Trainee" by "MedTourEasy Dubai", "MedTourEasy Jaipur", … — seen live)."""
    seen: set[tuple[str, str]] = set()
    out = []
    for item in items:
        key = (item.title.lower(), (item.organizer or "").split(" ")[0].lower())
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ------------------------------------------------------------------ linkedin posts

_ACTIVITY = re.compile(r"activity[:-](\d{15,})")
_AUTHWALL_PATHS = ("/authwall", "/login", "/signup", "/uas/login", "/checkpoint")
_POST_TYPES = ("SocialMediaPosting", "DiscussionForumPosting", "BlogPosting", "NewsArticle", "Article")


class LinkedInPostsParams(SourceParams):
    pages: list[str]
    max_posts_per_page: int = 20
    retry_pause: float = 20.0  # seconds before retrying pages LinkedIn refused with 999


def is_auth_wall(final_url: str, html: str) -> bool:
    path = urlsplit(final_url).path.lower()
    if path.startswith(_AUTHWALL_PATHS):
        return True
    title = re.search(r"<title[^>]*>(.*?)</title>", html[:20000], re.S | re.I)
    t = clean_text(title.group(1)).lower() if title else ""
    return t in {"sign up | linkedin", "linkedin login, sign in | linkedin", "sign in | linkedin", "linkedin: log in or sign up"}


def page_name(html: str, url: str) -> str:
    soup = BeautifulSoup(html[:50000], "html.parser")
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        # LinkedIn double-escapes some names ("Science &amp;amp; Technology"): clean_text decodes them.
        name = clean_text(re.split(r"\s+[|\-–]\s+", og["content"])[0])
        if name and name.lower() != "linkedin":
            return name
    slug = [p for p in urlsplit(url).path.split("/") if p]
    return unquote(slug[1]).replace("-", " ").title() if len(slug) > 1 else url


def _title_from_text(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    return truncate(first or text, 110)


def extract_posts(html: str, now: datetime) -> list[dict]:
    """Return [{url, text, published}] merged from every strategy, one entry per post.

    Real company pages carry JSON-LD for only a couple of posts but feed cards for all of them
    (seen live: 2 vs 10), so strategies are combined by activity id, not tried in turn. Exact
    JSON-LD dates win over relative card ages ("2d")."""
    merged: dict[str, dict] = {}
    by_text: dict[str, str] = {}  # a reshare has its own activity id but the same text
    for strategy in (_from_jsonld, _from_cards, _from_links):
        for post in strategy(html, now):
            if not post["text"]:
                continue
            m = _ACTIVITY.search(post["url"])
            key = m.group(1) if m else post["url"]
            key = by_text.setdefault(" ".join(post["text"].lower().split())[:120], key)
            if key not in merged:
                merged[key] = post
            elif not merged[key].get("published") and post.get("published"):
                merged[key]["published"] = post["published"]
    return list(merged.values())


def _from_jsonld(html: str, now: datetime) -> list[dict]:
    out = []
    for block in ld_blocks(html):
        for obj in iter_typed(block, _POST_TYPES):
            url = obj.get("url") or obj.get("mainEntityOfPage") or ""
            if isinstance(url, dict):
                url = url.get("@id", "")
            text = clean_text(obj.get("articleBody") or obj.get("text") or obj.get("headline") or "")
            if url:
                out.append({"url": url, "text": text, "published": parse_iso(obj.get("datePublished"))})
    return out


def _card_text(card: Tag) -> str:
    for sel in (
        ".attributed-text-segment-list__content",
        "[data-test-id*='commentary']",
        ".feed-shared-update-v2__description",
        ".base-main-card__title",
    ):
        el = card.select_one(sel)
        if el and el.get_text(strip=True):
            return clean_text(el.get_text(" "))
    return ""


def _card_age(card: Tag, now: datetime) -> datetime | None:
    for el in card.select("time, span[class*='time'], [class*='card__metadata']"):
        if dt := parse_iso(el.get("datetime")) or parse_relative_age(el.get_text(" "), now):
            return dt
    return None


def _from_cards(html: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    for card in soup.select("[data-activity-urn], [data-urn*='urn:li:activity:'], [data-featured-activity-urn]"):
        urn = card.get("data-activity-urn") or card.get("data-urn") or card.get("data-featured-activity-urn") or ""
        m = _ACTIVITY.search(urn)
        if not m or m.group(1) in out:
            continue
        aid = m.group(1)
        link = card.select_one("a[href*='/posts/'], a[href*='activity']")
        url = link["href"] if link and link.get("href") else f"https://www.linkedin.com/feed/update/urn:li:activity:{aid}"
        out[aid] = {"url": url, "text": _card_text(card), "published": _card_age(card, now)}
    return list(out.values())


def _from_links(html: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        m = _ACTIVITY.search(a["href"])
        if not m or m.group(1) in out:
            continue
        container = a.find_parent(["li", "article"]) or a.parent
        text = _card_text(container) if container else ""
        if not text:
            text = clean_text(a.get_text(" "))
        out[m.group(1)] = {
            "url": a["href"],
            "text": text,
            "published": _card_age(container, now) if container else None,
        }
    return list(out.values())


@register
class LinkedInPostsSource(Source):
    type = "linkedin_posts"
    Params = LinkedInPostsParams
    p: LinkedInPostsParams

    async def fetch(self, fx: Fetcher) -> list[Item]:
        items: list[Item | None] = []
        errors: list[tuple[str, str]] = []
        refused: list[str] = []
        for page in self.p.pages:
            await self._page(fx, page, items, errors, refused)
        if refused:
            # LinkedIn's 999 refusals are often momentary (seen live): one slower second pass.
            await fx.pause(self.p.retry_pause)
            errors = [(p, why) for p, why in errors if p not in refused]
            for page in refused:
                await self._page(fx, page, items, errors, [])

        result = self.collect(items)
        if errors and not result:
            raise RuntimeError(summarize_errors(errors, "page"))
        if errors:
            self.warnings.append(summarize_errors(errors, "page"))
        return result

    async def _page(self, fx, page, items, errors, refused) -> None:
        try:
            resp = await fx.get(page)
            html = resp.text
            if is_auth_wall(str(resp.url), html):
                raise BlockedError("auth wall (LinkedIn requires login for this page)")
            posts = extract_posts(html, self.now)
            if not posts:
                raise BlockedError("no public posts found (page may be auth-walled or layout changed)")
            name = page_name(html, page)
            items.extend(self._item(p, name) for p in posts[: self.p.max_posts_per_page])
        except Exception as e:  # BlockedError (auth wall, 999) or anything else: next page
            errors.append((page, str(e)))
            if "999" in str(e):
                refused.append(page)

    def _item(self, post: dict, organizer: str) -> Item | None:
        text = post["text"]
        return self.make(
            kind=Kind.POST,
            title=f"{organizer}: {_title_from_text(text)}",
            url=post["url"],
            summary=text,
            published_at=post.get("published"),
            organizer=organizer,
            is_online=None,
        )
