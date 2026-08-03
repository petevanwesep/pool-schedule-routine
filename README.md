# Piedmont Competition Pool Schedule Routine

Tracks the **Competition Pool** schedule at the Piedmont Community Pool and
keeps it available two ways:

1. **A subscribable calendar feed** — `competition_pool.ics`. Every lap-swim,
   rec/open-swim and team-practice session becomes a weekly recurring calendar
   event, and every schedule exception (water-polo game closures, special
   events, holiday hours) becomes a dated event. Subscribe once and your
   calendar app keeps it current.
2. **An email digest** (via [Resend](https://resend.com)) — a clean one-page
   HTML summary sent only when the competition-pool schedule changes, with the
   `.ics` attached. Delivery is verified, not just accepted.

Only the competition pool is tracked; the activity pool is ignored.

## How it parses the page

The city page ([Community Pool schedule][page]) was redesigned in 2026. The
routine now:

- keys off each table's caption (e.g. *“Competition Pool schedule, August 3,
  2026 to August 9, 2026”*) rather than a section heading, so it survives
  heading tweaks;
- decodes the **future date-range schedules that the page hides inside a
  JavaScript tab widget** (`scrollingTabs`), which a plain HTML read misses —
  so upcoming weeks are captured, not just the current one;
- reads the **Schedule Exceptions** block (skipping items commented out in the
  page source) for competition-pool closures, special events and holidays.

[page]: https://piedmont.ca.gov/cms/One.aspx?portalId=13659823&pageId=16935826

## Subscribe to the calendar feed

Once `competition_pool.ics` is committed to `main`, subscribe to its URL:

```
https://raw.githubusercontent.com/petevanwesep/pool-schedule-routine/main/competition_pool.ics
```

- **Google Calendar:** Other calendars ▸ **+** ▸ *From URL* ▸ paste ▸ *Add*.
- **Apple Calendar:** File ▸ *New Calendar Subscription…* ▸ paste.

Calendars refresh on their own schedule (Google, hours; Apple, configurable).

**Cleaner URL (optional):** enable **GitHub Pages** (Settings ▸ Pages ▸ deploy
from `main`, root) and subscribe to
`https://petevanwesep.github.io/pool-schedule-routine/competition_pool.ics`
instead — it's served as `text/calendar`, which a few calendar apps prefer.
Then set the `ICS_FEED_URL` repo variable to that URL so the email links to it.

## Staying current (GitHub Actions)

`.github/workflows/update-schedule.yml` runs daily (and on demand), regenerates
the feed, commits it when the schedule changed, and emails the digest.

- **Email:** add a repository secret `RESEND_API_KEY` (Settings ▸ Secrets and
  variables ▸ Actions). Without it, the workflow still refreshes the `.ics`,
  just no email.
- **Optional variables:** `MAIL_TO`, `MAIL_FROM`, `ICS_FEED_URL`.

## Run it yourself

```bash
python3 pool_schedule.py                     # refresh .ics; email if changed
RESEND_API_KEY=re_... python3 pool_schedule.py
RESEND_API_KEY=re_... python3 pool_schedule.py --manual   # force re-send now
python3 pool_schedule.py --dry-run           # refresh .ics + preview.html, no send
python3 pool_schedule.py --no-email          # refresh .ics only, never email
```

`--dry-run` writes `preview.html` (gitignored) and prints the plain-text body.
The script uses only the Python standard library plus `curl` (for Resend).

## Configuration (env vars)

| Var | Required | Default |
|---|---|---|
| `RESEND_API_KEY` | to email | — |
| `MAIL_TO` | no | `petervanwesep@gmail.com` |
| `MAIL_FROM` | no | `Piedmont Competition Pool <onboarding@resend.dev>` |
| `ICS_FEED_URL` | no | raw `main` URL of `competition_pool.ics` |

`onboarding@resend.dev` sends with zero DNS setup but only to the Resend
account owner's address. To send from your own domain, verify it in Resend and
set `MAIL_FROM` accordingly.

## Network egress (Claude Code on the web)

Outbound calls go to these hosts — they must be on the environment's egress
allowlist:

- `piedmont.ca.gov` — fetch the schedule page
- `api.resend.com` — send the email + verify delivery

> Egress policy is applied when the container starts. After changing the
> allowlist, start a **new session** so the change takes effect.

## Change detection & state

`state.json` stores a `content_hash` of the parsed competition-pool schedule
(sessions + exceptions, no timestamps) plus the current `schedule_ranges`. The
`.ics` is rewritten every run; the email is sent only when the hash changes
(or with `--manual`). Calendar event UIDs are stable, so subscribers get
in-place updates instead of duplicates.

## Secrets

`RESEND_API_KEY` is read from the environment only. Never hardcode, log, or
commit it — this repository is public.
