#!/usr/bin/env python3
"""Piedmont Community Pool schedule routine — two-document edition.

Produces lap-swim.html (Doc A: Lap Swim, Pete & Stacy) and
rec-swim.html (Doc B: Rec Swim, Niko) by populating
pool-schedule-template.html from the live Piedmont city page,
then sends both as email attachments via SendGrid v3.

Secrets: SENDGRID_API_KEY is read from the environment only.
         NEVER hardcode, log, commit, or print it.

Usage:
    SENDGRID_API_KEY=SG.xxx python3 pool_schedule.py          # normal cadence run
    SENDGRID_API_KEY=SG.xxx python3 pool_schedule.py --manual # force re-send now
    python3 pool_schedule.py MANUAL_RESEND                    # same, positional form
"""

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zoneinfo

PAGE_URL = (
    "https://piedmont.ca.gov/cms/One.aspx"
    "?portalId=13659823&pageId=16935826"
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "pool-schedule-template.html")
TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

MAIL_TO = os.environ.get("MAIL_TO", "petervanwesep@gmail.com")
MAIL_FROM_EMAIL = "pool@pjvw.io"
MAIL_FROM_NAME = "Piedmont Pool Schedule"

SEND_DAYS = {1, 2, 3, 5, 8, 13, 21}
WARN_AFTER_DAYS = 30

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"],
    start=1)}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def today_la():
    return datetime.datetime.now(TZ).date()


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def git_commit_and_push(message, paths):
    """Stage the given paths, commit, and push to the current branch."""
    try:
        subprocess.run(["git", "add"] + [os.path.relpath(p, SCRIPT_DIR) for p in paths],
                       check=True, cwd=SCRIPT_DIR, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=SCRIPT_DIR)
        if result.returncode == 0:
            print("git: nothing to commit (already up to date).")
            return
        subprocess.run(["git", "commit", "-m", message],
                       check=True, cwd=SCRIPT_DIR, capture_output=True)
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=SCRIPT_DIR).stdout.strip()
        for delay in (0, 2, 4, 8, 16):
            if delay:
                import time; time.sleep(delay)
            r = subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=SCRIPT_DIR, capture_output=True)
            if r.returncode == 0:
                print(f"git: pushed to origin/{branch}")
                return
            print(f"git push failed (attempt); retrying in {delay*2 or 2}s…")
        print("git: push failed after retries.")
    except subprocess.CalledProcessError as e:
        print(f"git error: {e.stderr.decode() if e.stderr else e}")


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def fetch_html():
    req = urllib.request.Request(
        PAGE_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; pool-schedule-routine/2)"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _clean(fragment):
    text = fragment.replace("\\n", " ")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(li|p|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&ndash;", "–").replace("&mdash;", "—"))
    text = re.sub(r"&#\d+;", "", text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def _cell_lines(raw):
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
    """Return (period_label, end_date, sections).

    Each section: {'title': str, 'rows': list[list[list[str]]]}
    rows[0] is the header row; subsequent rows are data.
    Each cell is a list of text lines.
    """
    label, end = parse_period(html)

    sections = []
    token_re = re.compile(
        r"<h(2|3|4)[^>]*>(.*?)</h\1>|<table[^>]*>(.*?)</table>",
        re.DOTALL | re.IGNORECASE)
    pool = None
    sub = None
    for tok in token_re.finditer(html):
        if tok.group(3) is None:
            level = int(tok.group(1))
            title = _clean(tok.group(2))
            if not title or (label and title == label):
                continue
            if level <= 3:
                pool, sub = title, None
            else:
                sub = title
        else:
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
            sub = None
    return label, end, sections


# ---------------------------------------------------------------------------
# Schedule extraction helpers
# ---------------------------------------------------------------------------

def _title_has(section, *keywords):
    t = section["title"].lower()
    return all(k in t for k in keywords)


def settlemier_lap_sections(sections):
    secs = [s for s in sections if _title_has(s, "settlemier", "lap")]
    if not secs:
        # Fallback: Settlemier sections not tagged as open/rec/tube
        secs = [s for s in sections
                if "settlemier" in s["title"].lower()
                and not any(k in s["title"].lower()
                            for k in ("open", "rec", "tube", "lesson"))]
    return secs


def settlemier_rec_sections(sections):
    return [s for s in sections
            if "settlemier" in s["title"].lower()
            and any(k in s["title"].lower() for k in ("open", "rec", "tube"))]


def activity_pool_sections(sections):
    return [s for s in sections if "activity" in s["title"].lower()]


def _row_is_lesson(row):
    text = " ".join(" ".join(cell) for cell in row).lower()
    return any(k in text for k in ("lesson", "instruction", "swim class"))


def _section_plain_lines(sections):
    lines = []
    for sec in sections:
        for i, row in enumerate(sec["rows"]):
            if i == 0:
                continue
            cells = [" ".join(c).strip() for c in row if any(x.strip() for x in c)]
            if cells:
                lines.append("  " + "  |  ".join(cells))
    return lines


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_table(rows, mark_lessons=False):
    parts = ["<table>"]
    for i, row in enumerate(rows):
        is_hdr = (i == 0)
        is_lesson = (not is_hdr) and mark_lessons and _row_is_lesson(row)
        tag = "th" if is_hdr else "td"
        cls = ' class="lesson-row"' if is_lesson else ""
        cells = "".join(
            f"<{tag}>{'<br>'.join(esc(l) for l in cell)}</{tag}>"
            for cell in row)
        parts.append(f"<tr{cls}>{cells}</tr>")
    parts.append("</table>")
    return "\n".join(parts)


def _render_pool_block(display_name, sections, mark_lessons=False):
    parts = [f'<div class="pool">', f"<h2>{esc(display_name)}</h2>"]
    multi = len(sections) > 1
    for sec in sections:
        if not sec["rows"]:
            continue
        if multi and "—" in sec["title"]:
            sub = sec["title"].split("—", 1)[-1].strip()
            parts.append(f'<h3 class="sub">{esc(sub)}</h3>')
        parts.append(_render_table(sec["rows"], mark_lessons=mark_lessons))
    parts.append("</div>")
    return "\n".join(parts)


def _fill_template(template, title, period, audience, schedule_html, genstamp):
    html = template
    html = re.sub(r"<title[^>]*>.*?</title>",
                  f"<title>{esc(title)}</title>", html, flags=re.DOTALL)
    html = re.sub(r'(<[^>]+id="doc-title"[^>]*>)[^<]*(</[^>]+>)',
                  rf'\g<1>{esc(title)}\2', html)
    html = re.sub(r'(<[^>]+id="doc-period"[^>]*>)[^<]*(</[^>]+>)',
                  rf'\g<1>{esc(period)}\2', html)
    html = re.sub(r'(<[^>]+id="doc-audience"[^>]*>)[^<]*(</[^>]+>)',
                  rf'\g<1>{esc(audience)}\2', html)
    html = re.sub(
        r"<!-- BEGIN SCHEDULE -->.*?<!-- END SCHEDULE -->",
        f"<!-- BEGIN SCHEDULE -->\n{schedule_html}\n  <!-- END SCHEDULE -->",
        html, flags=re.DOTALL)
    footnote_inner = f'<span id="genstamp">{esc(genstamp)}</span>'
    html = re.sub(
        r'(<div[^>]+id="footnotes"[^>]*>).*?(</div>)',
        rf'\g<1>{footnote_inner}\2', html, flags=re.DOTALL)
    return html


def build_lap_swim_html(template, label, today, sections):
    """DOC A — Lap Swim: Settlemier lap windows only. Audience: Pete & Stacy."""
    lap_secs = settlemier_lap_sections(sections)
    if not lap_secs:
        return None
    genstamp = f"Generated {today} from piedmont.ca.gov/pool"
    block = _render_pool_block("Settlemier Competition Pool", lap_secs)
    return _fill_template(template, "Lap Swim", label or "", "Pete & Stacy",
                          block, genstamp)


def build_rec_swim_html(template, label, today, sections):
    """DOC B — Rec Swim: open/rec swim across both pools. Audience: Niko."""
    genstamp = f"Generated {today} from piedmont.ca.gov/pool"
    parts = []
    has_lessons = False

    settle_secs = settlemier_rec_sections(sections)
    if settle_secs:
        parts.append(_render_pool_block("Settlemier Competition Pool", settle_secs))

    act_secs = activity_pool_sections(sections)
    if act_secs:
        parts.append(_render_pool_block("Activity Pool", act_secs, mark_lessons=True))
        if any(_row_is_lesson(r) for s in act_secs for r in s["rows"][1:]):
            has_lessons = True

    if not parts:
        return None

    if has_lessons:
        parts.append(
            '<p class="lesson-note">'
            '<em>Shaded/italicized rows: swim lessons in progress — '
            'open swim not available during those windows.</em></p>')

    return _fill_template(template, "Rec Swim", label or "", "Niko",
                          "\n".join(parts), genstamp)


def _text_lap_summary(sections):
    lines = _section_plain_lines(settlemier_lap_sections(sections))
    return "\n".join(lines) if lines else "  (no lap swim data found)"


def _text_rec_summary(sections):
    lines = _section_plain_lines(
        settlemier_rec_sections(sections) + activity_pool_sections(sections))
    return "\n".join(lines) if lines else "  (no rec swim data found)"


# ---------------------------------------------------------------------------
# SendGrid email
# ---------------------------------------------------------------------------

def _sendgrid_send(payload):
    """Send via SendGrid v3. Returns HTTP status or 0 on network error."""
    key = os.environ.get("SENDGRID_API_KEY")
    if not key:
        sys.exit("ERROR: SENDGRID_API_KEY is not set.")
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        })
    try:
        status = urllib.request.urlopen(req).status
        print(f"email sent (HTTP {status})")
        return status
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:500]
        print(f"email send failed: HTTP {e.code} — {err}")
        return e.code
    except Exception as ex:
        print(f"email send error: {ex}")
        return 0


def _base_payload(subject, body):
    return {
        "personalizations": [{"to": [{"email": MAIL_TO}]}],
        "from": {"email": MAIL_FROM_EMAIL, "name": MAIL_FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }


def send_schedule_email(label, lap_path, rec_path, sections):
    subject = f"Piedmont Pool Schedule — {label}"
    body = (
        f"Piedmont Community Pool — {label}\n\n"
        f"LAP SWIM (Settlemier Competition Pool — Doc A, Pete & Stacy):\n"
        f"{_text_lap_summary(sections)}\n\n"
        f"REC SWIM (Doc B, Niko):\n"
        f"{_text_rec_summary(sections)}\n\n"
        "Open each attachment and print; it auto-fits to one page.\n\n"
        "Source: piedmont.ca.gov/pool"
    )
    payload = _base_payload(subject, body)
    atts = []
    for path, name in [(lap_path, "piedmont-lap-swim.html"),
                       (rec_path, "piedmont-rec-swim.html")]:
        if path and os.path.exists(path):
            b = base64.b64encode(open(path, "rb").read()).decode()
            atts.append({"content": b, "type": "text/html",
                         "filename": name, "disposition": "attachment"})
    if atts:
        payload["attachments"] = atts
    _sendgrid_send(payload)


def send_warning_email(schedule_end, days_over=None,
                       extraction_failed=False, raw_html=None):
    subject = "Piedmont pool schedule overdue"
    today = today_la()
    if extraction_failed:
        body = (
            f"The Piedmont pool page was fetched on {today} but the schedule "
            f"tables could not be confidently extracted. "
            f"The raw HTML is attached for manual inspection.\n\n"
            f"Last known schedule ended: {schedule_end}"
        )
    else:
        body = (
            f"The last Piedmont Community Pool schedule ended {schedule_end} "
            f"and is now {days_over} days overdue with no new posting found "
            f"on the city website (checked {today})."
        )
    payload = _base_payload(subject, body)
    if extraction_failed and raw_html:
        b = base64.b64encode(raw_html.encode("utf-8", "replace")).decode()
        payload["attachments"] = [{
            "content": b, "type": "text/html",
            "filename": "piedmont-pool-raw.html",
            "disposition": "attachment",
        }]
    _sendgrid_send(payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", action="store_true",
                    help="skip freshness checks and re-send (same as MANUAL_RESEND)")
    args, extra = ap.parse_known_args()

    manual = (
        args.manual
        or os.environ.get("MANUAL_RESEND", "").strip()
        or "MANUAL_RESEND" in extra
    )

    state = load_state()
    today = today_la()
    schedule_end = datetime.date.fromisoformat(state["schedule_end"])

    # Step 1
    if today <= schedule_end and not manual:
        print(f"Schedule current through {schedule_end}; nothing to do.")
        return

    # Step 2
    days_over = (today - schedule_end).days
    if not manual:
        if days_over > WARN_AFTER_DAYS:
            print(f"Schedule {days_over}d overdue (>{WARN_AFTER_DAYS}d); sending warning.")
            send_warning_email(state["schedule_end"], days_over=days_over)
            return
        if days_over not in SEND_DAYS:
            state["attempts"] = state.get("attempts", 0) + 1
            save_state(state)
            git_commit_and_push(
                f"chore: cadence check (D={days_over}, attempt {state['attempts']})",
                [STATE_PATH])
            print(f"D={days_over} not a send day; attempts → {state['attempts']}.")
            return

    # Step 3
    print("Fetching pool schedule page…")
    raw_html = fetch_html()

    # Step 4
    label, end, sections = parse_schedule(raw_html)
    print(f"Parsed: label={label!r}  end={end}  sections={len(sections)}")

    if end is not None and end.isoformat() == state["schedule_end"] and not manual:
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        git_commit_and_push(
            f"chore: period unchanged ({label}), attempt {state['attempts']}",
            [STATE_PATH])
        print("Period unchanged; exiting quietly.")
        return

    # Step 5
    if not sections:
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        git_commit_and_push(
            f"chore: no schedule found, attempt {state['attempts']}",
            [STATE_PATH])
        print("No schedule sections found; attempts incremented.")
        return

    # Check we can identify at least one expected pool
    have_settlemier = any("settlemier" in s["title"].lower() for s in sections)
    have_activity = any("activity" in s["title"].lower() for s in sections)
    if not have_settlemier and not have_activity:
        print("Sections found but neither expected pool matched — extraction failed.")
        send_warning_email(state["schedule_end"],
                           extraction_failed=True, raw_html=raw_html)
        return

    template = open(TEMPLATE_PATH).read()
    is_new = end is not None and end.isoformat() != state["schedule_end"]

    lap_path = os.path.join(SCRIPT_DIR, "lap-swim.html")
    rec_path = os.path.join(SCRIPT_DIR, "rec-swim.html")

    lap_html = build_lap_swim_html(template, label, today, sections)
    rec_html = build_rec_swim_html(template, label, today, sections)

    if lap_html:
        with open(lap_path, "w") as f:
            f.write(lap_html)
    if rec_html:
        with open(rec_path, "w") as f:
            f.write(rec_html)

    if is_new:
        state["schedule_end"] = end.isoformat()
        state["schedule_label"] = label
        state["attempts"] = 0
        save_state(state)
        commit_paths = [STATE_PATH]
        if lap_html:
            commit_paths.append(lap_path)
        if rec_html:
            commit_paths.append(rec_path)
        git_commit_and_push(f"feat: new pool schedule — {label}", commit_paths)
        print(f"New schedule committed: {label}")

    # Step 6
    if not os.environ.get("SENDGRID_API_KEY"):
        print("SENDGRID_API_KEY not set; skipping email.")
        return
    print("Sending schedule email…")
    send_schedule_email(label, lap_path, rec_path, sections)


if __name__ == "__main__":
    main()
