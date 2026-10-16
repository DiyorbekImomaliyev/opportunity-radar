# Test fixtures

Real responses captured from public sites, so parsers are tested against real markup. Names,
places and domains were replaced with neutral stand-ins (Istanbul, `*.example`); the structure is
unchanged. The Meetup and Eventbrite pages are trimmed to their JSON-LD blocks.

| file | source type |
|---|---|
| tstp_events.json, tstp_jobs.json, tstp_listing.html | `wordpress` (REST API + listing page) |
| bevy_gdg_istanbul.json, bevy_startupgrind_istanbul.json | `bevy` |
| meetup_istanbul_ai.html, eventbrite_istanbul_ai.html, devevents_tr.html | `jsonld` |
| devpost_p1.json | `devpost` |
| tribe_exu.json | `tribe_events` |
| tf_events.xml | `rss` (event dates in pubDate) |

LinkedIn: `li_company_real.html` (a real logged-out company page, trimmed to its JSON-LD and
feed cards) and `li_jobs_real.html` (a real guest job-search response) were captured on
2026-10-16. The other `li_*.html` files are small hand-built cases (profile activity, auth wall,
edge cases) based on the same markup.
