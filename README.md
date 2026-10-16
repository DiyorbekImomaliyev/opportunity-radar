# Opportunity Radar

Get the events, hackathons, internships, jobs and LinkedIn posts that matter to you on Telegram,
once a day, from the websites you choose.

## What it does

- Reads your websites every day: Meetup, Eventbrite, community and university calendars, job boards, RSS feeds and LinkedIn.
- Drops past, far-away, duplicate and off-topic items.
- Rates each item with free Gemini AI (optional) and explains it in one line.
- Checks job and program pages for visa sponsorship and who may apply.
- Sends each pick as its own Telegram message, with buttons to mark it.
- Reminds you of what you saved until you act on it.
- Never sends the same item twice, and reports websites that stop working.

## Set it up

1. **Install** with Python 3.11 or newer:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```
2. **Copy** the example files and edit them:
   ```bash
   cp config/sources.example.yaml config/sources.yaml
   cp config/profile.example.yaml config/profile.yaml
   cp .env.example .env
   ```
3. **Create** a Telegram bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and put the token in `.env`.
4. **Send** your bot a message, run `.venv/bin/radar telegram-setup`, and put the chat id in `.env`.
5. **Add** a free Gemini key from [Google AI Studio](https://aistudio.google.com) to `.env` (optional).
6. **Test** it without sending anything:
   ```bash
   .venv/bin/radar run --dry-run
   ```
7. **Schedule** it on macOS. The digest goes out the first time your Mac is on after 07:00:
   ```bash
   deploy/install_launchd.sh 07:00
   ```

Your `config/*.yaml` files and `.env` stay on your computer. Git ignores them.

## Add your websites

Add a block to `config/sources.yaml`, then check it with `.venv/bin/radar test-source <name>`.

| type | use it for | example |
|---|---|---|
| `jsonld` | pages with event data (Meetup, Eventbrite, dev.events, many venues) | `urls: [https://www.meetup.com/find/?location=…&keywords=AI]` |
| `bevy` | Google Developer Groups, Startup Grind | `chapter_id: 995` (search "chapter_id" in the page source) |
| `tribe_events` | sites with the WordPress "Events Calendar" plugin | `base_url: https://www.example.edu` |
| `wordpress` | posts or events of any WordPress site | `base_url: …`, `post_type: events` |
| `rss` | any RSS or Atom feed | `url: https://www.example.org/feed/` |
| `devpost` | online hackathons | none needed |
| `linkedin_jobs` | LinkedIn job search without login | `keywords: [AI intern]`, `locations: [United Kingdom]` |
| `linkedin_posts` | public posts of companies or people | `pages: [https://www.linkedin.com/company/…/]` |
| `arbeitnow`, `remote_jobs` | job boards in Europe and remote jobs | `provider: remotive`, `search: data` |

- **Set** `default_location` to your city for local sources.
- **List** your best sources first. They win when two sites show the same event.
- **Use** `max_queries` and `details` on `linkedin_jobs` to cover many countries and check visa lines without hitting LinkedIn's limit.

## Tune the digest

Edit `config/profile.yaml`:

- **Write** `about` in plain words: who you are, what you want, what you can apply to. The AI follows it.
- **List** your topics under `interests` and your city under `locations`.
- **Name** trusted organisers under `notable_organizers`. Their online events rank first.
- **Set** `relocation_ok` and `needs_visa_sponsorship` if you want jobs abroad.
- **Change** `max_items`, `quotas` or `min_online_score` for more or fewer items.

## Use the bot

The schedule script also starts the bot. It runs while your computer is on.

- **Mark** each pick with its buttons: Registered (or Applied), Remind me, Not for me, Can't apply. Send `/review` for the ones you skipped.
- **Send** any link or forwarded post. The bot asks whether to keep it and reminds you every 3 days until you mark it done.
- **Write** in plain words: "no more real estate events", "follow linkedin.com/company/…", "what is saved?" The bot asks before it changes anything.
- **Get** a calendar file and a reminder the day before each event you register for.

Your marks teach the ranking. The AI sees what you chose, and sites you keep rejecting rank lower.

## Commands

```bash
radar run [--dry-run] [--source NAME] [--no-llm]   # fetch, rank and send
radar test-source NAME                              # show what one website gives and why
radar health                                        # show which websites work
radar telegram-setup                                # find your chat id
radar bot                                           # run the bot (the schedule script starts it)
radar self-update                                   # install the latest version if its tests pass
```

Add `RADAR_AUTO_UPDATE=1` to `.env` to run `self-update` once a day.

## Develop

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check src tests --select F,E9,B --ignore B008
```

Read `SPEC.md` first. Each requirement has an ID, and the tests cite it.

## Limits

- LinkedIn shows only recent public posts without login, and limits how many pages you can open.
- Sites that block bots or need JavaScript to show content can't be read.
- The schedule script is for macOS. On Linux, run `radar run --scheduled` from cron and `radar bot` as a service.
- The bot answers only while your computer is on. Telegram drops messages older than 24 hours.
