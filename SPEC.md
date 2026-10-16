# Opportunity Radar: Specification

This file is the source of truth for the code. Each requirement has an ID (`R-…`), and tests cite
it in their docstrings. List covered IDs with `grep -rho 'R-[A-Z]*-[0-9]*' tests/ | sort -u`.

## 1. Purpose

Send the owner the few most important new opportunities once a day on Telegram: in-person events
near them, internships and jobs, funded programs, posts from LinkedIn pages they follow, and the
strongest online hackathons and events. Keep the digest short, but prefer one extra item to a
missed opportunity.

Out of scope: logged-in scraping, storing passwords for other sites, auto-apply, a web app.

## 2. Domain model

**R-MODEL-1** Produce `Item`s with `source`, `kind` (`event`, `hackathon`, `job`, `post`,
`program`), `title` (plain text, not empty), `url` (absolute http(s)), `summary` (plain text,
≤ 600 chars), `starts_at`, `ends_at` (deadline for hackathons), `published_at`, `location`,
`is_online` (true, false or unknown), `organizer` and `tags`. Store datetimes in UTC; reject
datetimes without a timezone.
**R-MODEL-2** Set `uid` to the first 16 hex chars of the SHA-256 of the canonical URL.
**R-MODEL-3** Build the canonical URL: lowercase scheme and host, no `www.`, no fragment, no
tracking parameters (`utm_*`, `fbclid`, `gclid`, `ref`, `trk`, `trackingId`, `refId`, `position`,
`pageNum`, `aff`…), sorted query, no trailing `/`. Map LinkedIn jobs to
`linkedin.com/jobs/view/<id>`, LinkedIn posts to `linkedin.com/feed/update/urn:li:activity:<id>`,
and Eventbrite pages of any country to `eventbrite.com/e/<id>`.

## 3. Sources

Write each source as an adapter (`async fetch(fetcher) -> list[Item]`), configured in
`config/sources.yaml` by `type` and parameters. Add a site of a known type with config only. Use
the profile timezone when an adapter's `timezone` is not set. Add a source's `tags` (for example
`[international]`) to all its items.

**R-SRC-1 `wordpress`** Read `/wp-json/wp/v2/<post_type>`. With `listing_url`, read each item's
date from the text nearest its link on that page: the link text first, then up to 3 parent
elements; stop when a parent also links to another item (it is a list, not a card). Turn
WordPress `tag-*` classes into tags. Replace `default_location` when the item says it happens
elsewhere (see Places in section 6).
**R-SRC-2 `bevy`** Read Bevy communities (Google Developer Groups, Startup Grind) from
`{base_url}/api/event_slim/for_chapter/{chapter_id}/?status=Live`. Mark `VIRTUAL` events online.
Use `organizer` instead of the chapter name when set.
**R-SRC-3 `jsonld`** Read schema.org `Event` objects from any page, including nested lists,
`@graph` and `itemListElement`. Skip invalid blocks. Resolve relative URLs against the page.
Treat a bare midnight time as a local date. Remove repeated address parts.
**R-SRC-4 `devpost`** Read open and upcoming hackathons from the Devpost API, page by page. Map
the submission period to `starts_at` and `ends_at`. Put prize, sign-ups and themes in the summary.
Filter by `themes` when set. Skip online hackathons with fewer than `min_registrations` sign-ups.
**R-SRC-5 `linkedin_jobs`** Search LinkedIn's public job listings without login, one query per
keyword and location, page by page. Mark results of the remote filter (`f_WT=2`) online. Tag
results `level:entry` when the experience filter `f_E` holds only 1, 2 or 3 (internship, entry,
associate); that tag passes the job-title check.
Run at most `max_queries` queries per run, and rotate them daily so all are covered within
⌈queries / max_queries⌉ days. Open the pages of up to `details` jobs not stored before: put
sentences about visas, citizenship or work permits first in the summary ("Eligibility: …"), and
tag the job `visa:sponsored` (offers sponsorship) or `visa:restricted` ("no sponsorship", "must
have the right to work", "UAE nationals only", Emiratisation or Saudization programs). Let a
restriction win over an offer. Keep both when the search later refreshes the item. Keep one job
per title and company when an agency posts it under many branch names.
**R-SRC-6 `linkedin_posts`** Read public company and profile pages without login. Merge posts from
JSON-LD, feed cards with an activity ID, and links to posts. Remove duplicates by activity ID and
by text (reshares). Keep exact JSON-LD dates. Retry pages refused with HTTP 999 once, after
`retry_pause`. Fail only the page that shows a login wall, a 999 or no posts.
**R-SRC-7 `rss`** Read RSS and Atom feeds. Set `date_is_event_start: true` for event feeds whose
publish date is the event date. With `fetch_eligibility: true` (programs), open each article and
put the requirement sentences of its eligibility section first in the summary. Open each article
only once; the store keeps that text when the feed refreshes the item.
**R-SRC-9** Send realistic browser headers and time out after 20 s. Skip and count a malformed
record; never fail the source for it.
**R-SRC-10 `tribe_events`** Read the WordPress "Events Calendar" API
(`/wp-json/tribe/events/v1/events`), page by page. Read times in UTC. Span all-day events over
whole local days. Mark `is_virtual` events online. Use the venue as location and categories as
tags.
**R-SRC-11 `arbeitnow`** Read the Arbeitnow job board (Germany and Europe), following
`links.next`. Tag jobs whose description offers visa sponsorship or relocation `visa:sponsored`.
Ignore negated offers ("no visa sponsorship", "cannot sponsor"). Count a "VISA Sponsorship" form
field only by its answer (Yes or No), and ignore an empty field. Keep only tagged jobs when
`visa_only` is set.
**R-SRC-12 `remote_jobs`** Read Remotive, Jobicy or Himalayas (`provider`, `search`). Keep a job
only when its applicant region matches `allowed_regions` (empty means worldwide). Shorten long
country lists to "N countries incl. <yours>". Tag entry-level and junior jobs `level:entry`.

## 4. Fetching

**R-NET-1** Run sources in parallel, at most 6 at once.
**R-NET-2** Retry connection errors, timeouts, 429 and 5xx up to 3 attempts, with exponential
backoff and jitter. Honour `Retry-After` up to 60 s. Don't retry other 4xx; keep their response
text in the error.
**R-NET-3** Wait 1 s between requests to one host by default, 4 s for LinkedIn and 0.05 s for
Telegram.
**R-NET-4** Never stop a run because one source fails.
**R-NET-5** Report a host as blocked on this machine when it resolves to `0.0.0.0`, `::` or
loopback (`/etc/hosts` or a site blocker).

## 5. Storage (SQLite, `data/radar.db`)

**R-DB-1** Store every item with first and last seen, score, reasons, AI summary, duplicate link
and `sent_at`.
**R-DB-2** Never reset `first_seen` or `sent_at` on update.
**R-DB-3** Record every run and every source result (`runs`, `source_runs`).
**R-DB-4** Create or migrate the schema on open (`PRAGMA user_version`) without losing rows:
v2 adds `ai_version`, v3 adds `marks` and `kv`.
Delete unsent items that are over or unseen for 60 days. Keep sent items.

## 6. Duplicates and relevance

**R-DEDUP-1** Treat the same canonical URL as the same item.
**R-DEDUP-2** Merge near duplicates across sources: same kind, same local day, and title token
overlap ≥ 0.85, or ≥ 0.6 when the start time is the same. Keep the item from the source listed
first. Recheck all unsent items every run. Never replace an item already sent.

**R-REL-1** Read `config/profile.yaml`: `timezone`, `home_label`, `about`, `interests` (topics with
weights; an item must match one), `formats` (boost only), `locations` (home keywords), `exclude`,
and the thresholds and limits below. Lowercase all keywords.
**R-REL-2** Drop before scoring:
- events that are over or start more than `lookahead_days` ahead;
- in-person events outside the home region, except programs from `international` sources when
  `relocation_ok: true`;
- online events (not hackathons) when `online_events: false`;
- posts and jobs older than `max_post_age_days`, and undated program pages older than
  `max_program_age_days`;
- jobs outside the home region, unless remote or (`relocation_ok` and tagged `international`);
- jobs with a senior title (`job_exclude_keywords`: senior, lead, principal, manager…), even when
  a board calls them entry-level;
- jobs whose title asks for a language not in `languages` ("Business Japanese",
  "German-speaking"; a language as the topic, such as "Arabic NLP", is fine);
- jobs whose title has none of `job_title_keywords` (English plus common German, French, Spanish
  and Italian words such as Praktikum, Werkstudent, stage) and no `level:entry` tag; for jobs,
  these words filter, they are not topics;
- titles with an `exclude` keyword;
- with `needs_visa_sponsorship: true`, jobs tagged `visa:restricted` or whose title names a
  nationals-only program ("Data Analyst (Tamheer)").

**Places.** Replace a source's `default_location` when the item names another place in its title,
uses one as a location in its text ("in Paris", "to Sarajevo"), or announces travel ("heading to
…") to somewhere outside the default location. Count the words of the default location as home,
so this works in any city.
**R-REL-3** Score items 0–10 by rules: topics ×2 in the title, ×1 in the text; formats ×1 or ×0.5;
+3 in the home region; +1 online when `remote_ok`; +2 for `visa:sponsored` when
`needs_visa_sponsorship`; +2 when a `notable_organizers` name (Google, NVIDIA…) appears in the
organizer, title or text, shown first as "by NVIDIA". Don't count community groups named after a
company and listed in `notable_exclude` ("Google Developer Group at …"). Score 0 without a topic
match. Match keywords of 3 letters or fewer only in capitals; match plurals. Require a topic or an
opportunity word ("register", "apply", "hackathon"…) in LinkedIn posts. Decide "home" by the known
location; use the title only when the location is unknown.
**R-REL-4** Rate items with AI when `GEMINI_API_KEY` is set:
- Send items with a rule score above 0 that were not judged before, in batches of ≤ 20, with the
  profile, asking for structured JSON. Send the key only in a header.
- Try the models in `llm_models` in order for each batch, each at most twice, 60 s per try. Skip a
  model for the rest of the run after a 429. Stop the AI step after 10 minutes, or after two
  batches in a row fail on every model.
- Keep rule scores for unjudged items, rank them after judged ones, and judge them on a later run.
- Replace the rule score with the AI score and the summary with its one-line `about`.
- Rate online items 8+ only when run by a major organisation, large or interactive.
- Read eligibility text first. Rate programs and internships the owner can't apply to
  (nationality, university country, degree level, local work permit) 3 or less. Take the owner's
  citizenship, study year and languages from `about`.
- Store a fingerprint of the prompt and the profile fields it uses with each verdict, and judge
  unsent items again when it changes.
- Keep rule scores when every model fails, and say so in the footer.
**R-REL-5** Send items with at least `min_score` (rules) or `min_ai_score` (AI). Require
`min_online_score` (default 8) for online items, or one point less from a notable organiser.
Store items below these scores without sending them.
**R-REL-6** Send at most `max_items` (default 15). Reserve slots per kind with `quotas` (default
in_person 6, jobs 3, posts 2, online 4; the sum may not exceed `max_items`), and give leftover slots
to the best remaining items, in-person first. Order by tier, then score, then soonest date:
1. in person in the home region;
2. internships and programs;
3. LinkedIn posts;
4. online hackathons and events (at most `max_online`, default 6; notable organisers first);
5. international: jobs and programs tagged `international`, and remote jobs (quota
   `international`);
6. other.

Rate international items 8+ only for real, paid or funded openings that accept international
applicants and sponsor the visa, or that are fully remote from the owner's country. Let held-back
items compete again later, and show how many in the footer.

## 7. Digest

**R-DIG-1** Send only items never sent and not duplicates. Mark them sent after delivery. Mark
nothing in a dry run.
**R-DIG-2** Group items into sections: Closing soon (≤ 3 days), `home_label`, Hackathons,
Internships and programs, International and remote, Online events, LinkedIn posts. Sort each
section by score, then soonest.
**R-DIG-3** Show for each item: its section, the linked bold title (≤ 300 chars), when and where,
a "Visa sponsorship" badge for `visa:sponsored`, one line ≤ 160 chars, and the source with "AI
rated N/10" or the top matched reasons. Escape all dynamic text as HTML.
**R-DIG-4** Start with a header: local date, count, count per section, and what the buttons mean.
End with a footer: AI note, held-back count, and source health (failures, a warning after 3 in a
row, sources that returned nothing).
**R-DIG-5** Send every item as its own message (a card), in section order, between the header
and the footer. Keep every message ≤ 4000 chars.
**R-DIG-6** Send a short "No new opportunities today" with the footer when nothing is new.

## 8. Delivery and setup

**R-OUT-1** Send with Telegram `sendMessage` (HTML, no link previews) using `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID`. Notify only on the header; send cards and footer silently, 0.4 s apart.
When delivery fails midway, mark the cards that arrived as sent and send the rest on the next run.
**R-OUT-2** Print the digest and save it to `data/last_digest.html` without Telegram settings or
with `--dry-run`. Mark nothing sent.
**R-OUT-3** Print the chat ID of whoever messaged the bot with `radar telegram-setup`.

## 8b. Bot (`radar bot`)

**R-BOT-1** Serve only the configured chat (`TELEGRAM_CHAT_ID`). Ignore and log messages and taps
from anyone else. Long-poll `getUpdates`; Telegram keeps updates for 24 h, so handle taps made
while the computer was off when it comes back.
**R-BOT-2** Put four buttons under every card: Registered (Applied for jobs and programs, Done for
posts), Remind me, Not for me, Can't apply. Record a tap as a mark, add the status to the card and
highlight the chosen button. Change the mark when another button is tapped. Resend unmarked cards
of the last digest on `/review`.
**R-BOT-3** Store marks per item (`marks` table). Never show a marked item in a digest again. Send
a calendar file (.ics) and one reminder the day before for a registered event with a date.
**R-BOT-4** Remind saved items and saved finds every `remind_every_days` (default 3) until the
owner taps Done or Remove; snooze one period on Snooze. Add a reminder the day before a known
deadline, and stop once the deadline or event has passed. Send due reminders from the bot loop
and from scheduled runs.
**R-BOT-5** Treat a message with a link (typed or forwarded) as a find: read the page title and
ask "Keep this?" with Save and remind, Already registered, and No.
**R-BOT-6** Turn other text into one action with Gemini, from a fixed list: exclude a word, add a
topic, follow or unfollow a LinkedIn page, change a quota, list saved items, list what is coming
up. Only propose changes: apply them after Apply, validate them like the config, and write them
to the git-ignored `config/overrides.yaml`, which is merged over `profile.yaml` and
`sources.yaml`. Offer the same actions as commands without an AI key (`/exclude`, `/follow`,
`/saved`, `/upcoming`, `/help`).
**R-BOT-7** Learn from marks: add the owner's latest choices to the AI prompt as examples; take one
point from a source whose items the owner rejects ≥ 70 % of the time (≥ 5 marks); fill one
"Something different" slot with the best near-miss (within 2 points of the threshold) so the
digest doesn't narrow.

## 8c. Updates

**R-UPD-1** Fetch the tracked branch with `radar self-update`. When it is ahead, run the full test
suite on the new version in a separate worktree, and fast-forward only if every test passes.
Restart the bot on macOS. Tell the owner the result on Telegram. Do this once a day in scheduled
runs when `RADAR_AUTO_UPDATE=1`. Run lint and tests on GitHub Actions for every push and pull
request.

## 9. Configuration and secrets

**R-CFG-1** Validate config files on load. Report an unknown source type, a bad parameter, a
duplicate name or an unknown timezone clearly. Point a missing file to its `*.example.yaml`.
**R-CFG-2** Read secrets from the environment or a git-ignored `.env`. Redact them from every
error, log line and message.
**R-CFG-3** Skip sources with `enabled: false`.

## 10. Observability

**R-OBS-1** Write JSON-lines logs to `data/logs/radar.log` (5 × 1 MB) and readable logs to stderr.
Silence HTTP client logging, because URLs can contain tokens.
**R-OBS-2** Show per source with `radar health`: last status, last success, failures in a row,
average items, a warning when a source goes silent, and the last delivery.
**R-OBS-3** Exit with code 2 when every source failed or delivery failed; otherwise exit with 0.

## 11. Scheduling (macOS)

**R-SCHED-1** Send at most one digest per local day with `radar run --scheduled [--not-before
HH:MM]`: do nothing before `not-before`, after a successful scheduled delivery that day, or when
offline. Never let manual runs cancel the daily digest. Run it from launchd at login and every
30 minutes, so the digest arrives the first time the computer is on and online each day.
**R-SCHED-2** Prevent overlapping runs with an exclusive lock.

## 12. Known limits

- LinkedIn shows only recent public posts without login and hides some pages.
- Pages built only with JavaScript, and sites that block bots, can't be read. Follow those on
  LinkedIn instead.
