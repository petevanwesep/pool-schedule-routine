#!/usr/bin/env python3
"""Piedmont Community Pool schedule routine.

Fetches the City of Piedmont pool page, parses the published swim schedule,
renders a clean one-page HTML email, and sends it via the Resend API.

Secrets: the Resend API key is read from the RESEND_API_KEY environment
variable. It is NEVER hardcoded, logged, or written to disk. This repo is
public; do not commit any key.

Usage:
    RESEND_API_KEY=... python3 pool_schedule.py            # normal cadence run
    RESEND_API_KEY=... python3 pool_schedule.py --manual   # force re-send now
    python3 pool_schedule.py --dry-run                     # parse + render, no send
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zoneinfo

PAGE_URL = ("https://piedmont.ca.gov/cms/One.aspx"
            "?portalId=13659823&pageId=16935826")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

MAIL_TO = os.environ.get("MAIL_TO", "petervanwesep@gmail.com")
# Resend's shared onboarding domain works with zero DNS setup and can send to
# the account owner's address. Switch to "Piedmont Pool Schedule <pool@pjvw.io>"
# once pjvw.io is verified in Resend.
MAIL_FROM = os.environ.get("MAIL_FROM", "Piedmont Pool Schedule <onboarding@resend.dev>")

SEND_DAYS = {1, 2, 3, 5, 8, 13, 21}
WARN_AFTER_DAYS = 30

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], start=1)}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def today_la():
    return datetime.datetime.now(TZ).date()


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #
def fetch_html():
    req = urllib.request.Request(PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _clean(fragment):
    # The page embeds literal backslash-n escapes and wraps cell content in
    # <ul><li> lists with <br>/<em>; turn list items and breaks into newlines.
    text = fragment.replace("\\n", " ")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(li|p|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&ndash;", "-").replace("&mdash;", "-"))
    text = re.sub(r"&#\d+;", "", text)
    # Collapse runs of spaces/tabs within each line.
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def _cell_lines(raw):
    """Normalize a messy <td> into display lines.

    Fragments that start with '(' are continuations of the previous line
    (e.g. '(8 lanes)'); everything else starts a new line.
    """
    frags = [s.strip() for s in _clean(raw).split("\n") if s.strip()]
    lines = []
    for fr in frags:
        if fr.startswith("(") and lines:
            lines[-1] += " " + fr
        else:
            lines.append(fr)
    return lines or [""]


def parse_period(html):
    m = re.search(
        r"(Summer|Spring|Fall|Autumn|Winter)\s+(\d{4})\s*:\s*"
        r"([A-Za-z]+)\s+(\d+)\s*[-–]\s*([A-Za-z]+)\s+(\d+)", html)
    if not m:
        return None, None
    season, year, _sm, _sd, em, ed = m.groups()
    label = re.sub(r"\s+", " ", m.group(0)).strip()
    try:
        end = datetime.date(int(year), MONTHS[em], int(ed))
    except (KeyError, ValueError):
        end = None
    return label, end


def parse_schedule(html):
    """Return (period_label, schedule_end_date, sections).

    sections is an ordered list of {"title", "level", "rows"} where rows is a
    list of cell-string lists (first row is the header).
    """
    start = html.find("Lap Swim &amp; Open Swim Schedules")
    if start == -1:
        start = html.find("Lap Swim & Open Swim Schedules")
    section_html = html[start:start + 60000] if start != -1 else html

    label, end = parse_period(html)

    sections = []
    token_re = re.compile(
        r"<h(2|3|4)[^>]*>(.*?)</h\1>|<table[^>]*>(.*?)</table>",
        re.DOTALL | re.IGNORECASE)
    pool = None      # current pool heading, h3 (e.g. "Settlemier Competition Pool")
    sub = None       # current sub-area, h4 (e.g. "Lap Side")
    for tok in token_re.finditer(section_html):
        if tok.group(3) is None:  # heading
            level = int(tok.group(1))
            title = _clean(tok.group(2))
            if not title or (label and title == label):
                continue  # ignore the period heading
            if level <= 3:
                pool, sub = title, None
            else:
                sub = title
        else:  # table
            rows = []
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tok.group(3),
                                  re.DOTALL | re.IGNORECASE):
                cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row,
                                   re.DOTALL | re.IGNORECASE)
                cells = [_cell_lines(c) for c in cells]
                if any(any(x) for x in cells):
                    rows.append(cells)
            if not rows:
                continue
            title = pool or "Schedule"
            if sub:
                title += " — " + sub
            sections.append({"title": title, "rows": rows})
            sub = None  # consumed; next table needs its own sub-heading
    return label, end, sections


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_html(label, sections):
    css = """
body{font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;max-width:760px;margin:0 auto;padding:12px}
h1{font-size:20px;margin:0 0 2px}
h2{font-size:15px;margin:18px 0 6px;color:#0b5394;border-bottom:2px solid #0b5394;padding-bottom:3px}
.period{color:#555;font-size:13px;margin-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:6px}
th,td{border:1px solid #ccc;padding:5px 7px;text-align:left;vertical-align:top}
th{background:#0b5394;color:#fff;font-weight:600}
td:first-child{font-weight:600;background:#f3f6fb;white-space:nowrap}
.foot{font-size:11px;color:#888;margin-top:16px}
""".strip()
    parts = ['<!DOCTYPE html><html><head><meta charset="utf-8"><style>',
             css, "</style></head><body>",
             "<h1>Piedmont Community Pool</h1>",
             f'<div class="period">{esc(label or "Swim Schedule")}</div>']
    for sec in sections:
        parts.append(f"<h2>{esc(sec['title'])}</h2><table>")
        for i, row in enumerate(sec["rows"]):
            tag = "th" if i == 0 else "td"
            cells = "".join(
                f"<{tag}>{'<br>'.join(esc(l) for l in cell)}</{tag}>"
                for cell in row)
            parts.append(f"<tr>{cells}</tr>")
        parts.append("</table>")
    parts.append('<div class="foot">Source: City of Piedmont &middot; '
                 'piedmont.ca.gov</div></body></html>')
    return "".join(parts)


def render_text(label, sections):
    out = [f"Piedmont Community Pool - {label or 'Swim Schedule'}", ""]
    for sec in sections:
        out.append(sec["title"].upper())
        for i, row in enumerate(sec["rows"]):
            joined = "  |  ".join(" ".join(cell) for cell in row)
            out.append(("  " if i else "") + joined)
        out.append("")
    out.append("Source: City of Piedmont - piedmont.ca.gov")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Send (Resend) + delivery verification
# --------------------------------------------------------------------------- #
def _resend_request(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "https://api.resend.com" + path, data=data, method=method,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:500]}


def send_email(key, subject, html, text):
    status, resp = _resend_request("POST", "/emails", key, {
        "from": MAIL_FROM, "to": [MAIL_TO],
        "subject": subject, "html": html, "text": text})
    if status not in (200, 201):
        raise RuntimeError(f"send failed: HTTP {status} {resp.get('error', resp)}")
    email_id = resp.get("id")
    print(f"send accepted: HTTP {status} id={email_id}")
    return email_id


def verify_delivery(key, email_id, attempts=6, delay=5):
    """A 2xx only means accepted. Confirm the message actually delivered."""
    if not email_id:
        return None
    last = None
    for _ in range(attempts):
        status, resp = _resend_request("GET", f"/emails/{email_id}", key)
        last = resp.get("last_event") if status == 200 else None
        print(f"  delivery status: {last}")
        if last in ("delivered", "bounced", "complained", "failed", "canceled"):
            break
        time.sleep(delay)
    if last == "delivered":
        print("DELIVERED ✔")
    else:
        print(f"NOT confirmed delivered (last_event={last}). A 2xx is not proof "
              f"of delivery — investigate before reporting success.")
    return last


def send_warning(key, schedule_end, days):
    subject = "Piedmont pool schedule overdue"
    body = (f"The last posted Piedmont Community Pool schedule ended "
            f"{schedule_end} and is now {days} days overdue with no new posting "
            f"on the city website.")
    eid = send_email(key, subject, f"<p>{esc(body)}</p>", body)
    verify_delivery(key, eid)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", action="store_true",
                    help="force re-send of the current schedule")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + render, print HTML, do not send")
    args = ap.parse_args()

    state = load_state()
    today = today_la()
    schedule_end = datetime.date.fromisoformat(state["schedule_end"])

    if not args.manual and not args.dry_run and today <= schedule_end:
        print(f"today {today} <= schedule_end {schedule_end}; nothing to do.")
        return

    days_over = (today - schedule_end).days
    if not args.manual and not args.dry_run:
        if days_over > WARN_AFTER_DAYS:
            key = require_key()
            send_warning(key, state["schedule_end"], days_over)
            return
        if days_over not in SEND_DAYS:
            state["attempts"] = state.get("attempts", 0) + 1
            save_state(state)
            print(f"D={days_over} not a send day; attempts -> {state['attempts']}.")
            return

    html_page = fetch_html()
    label, end, sections = parse_schedule(html_page)
    if not sections:
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        print("no schedule found on page; attempts incremented.")
        return

    print(f"period: {label}  end: {end}  sections: {len(sections)}")

    is_new = end is not None and end.isoformat() != state["schedule_end"]
    if not is_new and not args.manual and not args.dry_run:
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        print("schedule unchanged; attempts incremented, exiting quietly.")
        return

    html = render_html(label, sections)
    text = render_text(label, sections)

    if args.dry_run:
        out = os.path.join(os.path.dirname(STATE_PATH), "preview.html")
        with open(out, "w") as f:
            f.write(html)
        print(f"--- dry run --- wrote {out}\n")
        print(text)
        return

    if is_new:
        state["schedule_end"] = end.isoformat()
        state["schedule_label"] = label
        state["attempts"] = 0
        save_state(state)
        print(f"state updated: schedule_end -> {end.isoformat()}")

    key = require_key()
    subject = f"Piedmont Pool Schedule — {label}"
    eid = send_email(key, subject, html, text)
    verify_delivery(key, eid)


def require_key():
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        sys.exit("ERROR: RESEND_API_KEY is not set. Export it before running "
                 "(do not hardcode or commit it).")
    return key


if __name__ == "__main__":
    main()
