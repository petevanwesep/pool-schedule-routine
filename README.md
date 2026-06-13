# Piedmont Pool Schedule Routine

Maintains a printable one-page schedule for the Piedmont Community Pool and
emails it. Fetches the city pool page, parses the published swim schedule,
renders a clean one-page **HTML email** (no PDF), sends via **Resend**, and
verifies actual delivery.

## Run

```bash
RESEND_API_KEY=re_... python3 pool_schedule.py            # normal cadence run
RESEND_API_KEY=re_... python3 pool_schedule.py --manual   # force re-send now
python3 pool_schedule.py --dry-run                        # parse + render only, no send
```

`--dry-run` writes `preview.html` (gitignored) and prints the plain-text body.

## Configuration (env vars)

| Var | Required | Default |
|---|---|---|
| `RESEND_API_KEY` | yes (to send) | — |
| `MAIL_TO` | no | `petervanwesep@gmail.com` |
| `MAIL_FROM` | no | `Piedmont Pool Schedule <onboarding@resend.dev>` |

`onboarding@resend.dev` sends with zero DNS setup but only to the Resend
account owner's address. To send from your own domain, verify `pjvw.io` in
Resend and set `MAIL_FROM="Piedmont Pool Schedule <pool@pjvw.io>"`.

## Network egress (Claude Code on the web)

This routine makes outbound calls to these hosts. They must be on the
environment's network egress allowlist:

- `piedmont.ca.gov` — fetch the schedule page
- `api.resend.com` — send the email + verify delivery

> Egress policy is applied when the container starts. After changing the
> allowlist, start a **new session** so the change takes effect.

## Delivery verification

A 2xx from the send endpoint only means *accepted*, not *delivered*. The
routine polls `GET /emails/{id}` until `last_event == delivered` and warns
loudly if it does not — so an accepted-but-undelivered send is never reported
as success.

## Secrets

`RESEND_API_KEY` is read from the environment only. Never hardcode, log, or
commit it — this repository is public.

## State

`state.json` tracks `schedule_end`, `schedule_label`, and `attempts`. On a new
schedule the routine updates `schedule_end`/`schedule_label` and resets
`attempts` to 0.
