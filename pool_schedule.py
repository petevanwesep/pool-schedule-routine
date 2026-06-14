#!/usr/bin/env python3
"""Piedmont Community Pool schedule routine.

Fetches the City of Piedmont pool page, parses the published swim schedule,
and produces two print-ready one-page HTML files (lap-swim.html, rec-swim.html)
which it sends as attachments via SendGrid.

Usage:
    SENDGRID_API_KEY=... python3 pool_schedule.py              # normal cadence run
    SENDGRID_API_KEY=... python3 pool_schedule.py MANUAL_RESEND  # skip freshness checks

Security: SENDGRID_API_KEY is read from the environment only. Never hardcoded,
logged, or written to disk.
"""

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

PAGE_URL = ("https://piedmont.ca.gov/cms/One.aspx"
            "?portalId=13659823&pageId=16935826")
HERE        = os.path.dirname(os.path.abspath(__file__))
STATE_PATH  = os.path.join(HERE, "state.json")
TMPL_PATH   = os.path.join(HERE, "pool-schedule-template.html")
LAP_PATH    = os.path.join(HERE, "lap-swim.html")
REC_PATH    = os.path.join(HERE, "rec-swim.html")
TZ          = zoneinfo.ZoneInfo("America/Los_Angeles")
SEND_DAYS   = {1, 2, 3, 5, 8, 13, 21}
WARN_DAYS   = 30
MAIL_TO     = "petervanwesep@gmail.com"
MAIL_FROM   = {"email": "pool@pjvw.io", "name": "Piedmont Pool Schedule"}

MONTHS = {m: i for i, m in enumerate(
    ["January","February","March","April","May","June",
     "July","August","September","October","November","December"], start=1)}

DAY_ABBREV = {
    "monday-thursday": "Mon–Thu",
    "monday–thursday": "Mon–Thu",
    "friday": "Fri",
    "saturday": "Sat",
    "sunday": "Sun",
    "monday": "Mon",
    "tuesday": "Tue",
    "wednesday": "Wed",
    "thursday": "Thu",
}


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
# Git
# --------------------------------------------------------------------------- #
def git_commit(files, message):
    try:
        subprocess.run(["git", "add", "--"] + files, cwd=HERE, check=True,
                       capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=HERE, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"git commit: {message}")
        elif "nothing to commit" in result.stdout + result.stderr:
            print("git: nothing to commit")
        else:
            print(f"git commit failed: {result.stderr.strip()}")
    except Exception as e:
        print(f"git commit error (non-fatal): {e}")


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_html():
    req = urllib.request.Request(PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Parse period
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Parse tabs
# --------------------------------------------------------------------------- #
def _extract_tabs_json(html):
    """Walk the JS tabs:[...] array bracket-by-bracket to handle nested strings."""
    start = html.find("tabs: [")
    if start == -1:
        start = html.find("tabs:[")
    if start == -1:
        return []
    idx = html.index("[", start)
    depth = 0
    in_str = False
    escaped = False
    for i in range(idx, len(html)):
        c = html[i]
        if escaped:
            escaped = False
            continue
        if c == "\\" and in_str:
            escaped = True
            continue
        if c == '"' and not escaped:
            in_str = not in_str
            continue
        if not in_str:
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[idx:i + 1])
                    except json.JSONDecodeError:
                        return []
    return []


def _decode(html_fragment):
    """Strip tags and decode common HTML entities."""
    text = html_fragment.replace("\\n", " ")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(li|p|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&ndash;", "–").replace("&mdash;", "—")
                .replace("&lt;", "<").replace("&gt;", ">"))


def _cell_lines(raw):
    """Normalize a <td> fragment to a list of display lines."""
    frags = [s.strip() for s in _decode(raw).split("\n") if s.strip()]
    lines = []
    for fr in frags:
        if fr.startswith("(") and lines:
            lines[-1] += " " + fr
        else:
            lines.append(fr)
    return lines or [""]


def _parse_table(table_html):
    """Return (col_headers, data_rows).

    col_headers: list of (abbreviated_day, hours_sub) tuples; first entry is ("", "")
    data_rows: list of lists-of-cell-lines  (row[0] = activity lines)
    """
    col_headers = []
    data_rows = []

    thead = re.search(r"<thead[^>]*>(.*?)</thead>", table_html,
                      re.DOTALL | re.IGNORECASE)
    if thead:
        for th in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1),
                             re.DOTALL | re.IGNORECASE):
            lines = [l.strip()
                     for l in _decode(th).split("\n") if l.strip()]
            if not lines:
                col_headers.append(("", ""))
                continue
            day_key = lines[0].lower()
            day = DAY_ABBREV.get(day_key, lines[0])
            hours = lines[1] if len(lines) > 1 else ""
            col_headers.append((day, hours))

    tbody = re.search(r"<tbody[^>]*>(.*?)</tbody>", table_html,
                      re.DOTALL | re.IGNORECASE)
    if tbody:
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody.group(1),
                             re.DOTALL | re.IGNORECASE):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr,
                               re.DOTALL | re.IGNORECASE)
            row = [_cell_lines(c) for c in cells]
            if any(any(l for l in cell) for cell in row):
                data_rows.append(row)

    return col_headers, data_rows


def extract_schedule(html):
    """Return {"settlemier": ..., "activity_pool": {"Lap Side": ..., "Zero-Depth Area": ...}}
    or None if the page structure has changed and extraction cannot proceed.
    """
    tabs = _extract_tabs_json(html)
    if not tabs:
        return None

    settlemier_content = None
    activity_content = None
    for tab in tabs:
        title = tab.get("title", "")
        content = tab.get("content", "")
        if "settlemier" in title.lower() or "competition" in title.lower():
            settlemier_content = content
        elif "activity" in title.lower():
            activity_content = content

    if settlemier_content is None and activity_content is None:
        return None

    result = {}

    if settlemier_content:
        tables = re.findall(r"<table[^>]*>(.*?)</table>",
                            settlemier_content, re.DOTALL | re.IGNORECASE)
        if tables:
            headers, rows = _parse_table("<table>" + tables[0] + "</table>")
            result["settlemier"] = {"headers": headers, "rows": rows}

    if activity_content:
        sub = {}
        for m in re.finditer(
                r"<h4[^>]*>(.*?)</h4>(.*?)<table[^>]*>(.*?)</table>",
                activity_content, re.DOTALL | re.IGNORECASE):
            label = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            tbl = "<table>" + m.group(3) + "</table>"
            headers, rows = _parse_table(tbl)
            sub[label] = {"headers": headers, "rows": rows}

        if not sub:
            tables = re.findall(r"<table[^>]*>(.*?)</table>",
                                activity_content, re.DOTALL | re.IGNORECASE)
            if tables:
                headers, rows = _parse_table("<table>" + tables[0] + "</table>")
                sub["Lap Side"] = {"headers": headers, "rows": rows}

        result["activity_pool"] = sub

    return result or None


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))


def _render_table(headers, rows, lesson_keywords=None):
    """Build an HTML <table> string from parsed schedule data."""
    if lesson_keywords is None:
        lesson_keywords = []

    parts = ["<table>\n<tr>"]
    for day, hours in headers:
        parts.append("<th>" + esc(day))
        if hours:
            parts.append("<small>" + esc(hours) + "</small>")
        parts.append("</th>")
    parts.append("</tr>\n")

    for row in rows:
        activity = row[0][0] if row and row[0] else ""
        is_lesson = any(kw.lower() in activity.lower() for kw in lesson_keywords)
        cls = ' class="lesson"' if is_lesson else ""
        parts.append(f"<tr{cls}>")
        for cell in row:
            content = "<br>".join(esc(l) for l in cell)
            parts.append(f"<td>{content}</td>")
        parts.append("</tr>\n")

    parts.append("</table>\n")
    return "".join(parts)


def _filter_rows(rows, include_keywords):
    """Keep rows whose first cell (activity name) matches any include_keyword."""
    out = []
    for row in rows:
        activity = row[0][0].lower() if row and row[0] else ""
        if any(kw.lower() in activity for kw in include_keywords):
            out.append(row)
    return out


def _build_doc(template, doc_title, period, audience, schedule_html, genstamp,
               footnotes="All times Pacific. Subject to change — verify at piedmont.ca.gov/pool before visiting."):
    """Clone template and replace the five variable regions."""
    def _replace_span(html, elem_id, content):
        return re.sub(
            r'(<(?:span|title)\s[^>]*\bid="' + re.escape(elem_id) + r'"[^>]*>)[^<]*(</(?:span|title)>)',
            r"\g<1>" + content + r"\g<2>", html)

    html = template
    html = re.sub(r"(<title[^>]*>)[^<]*(</title>)",
                  r"\g<1>" + esc(doc_title) + r"\g<2>", html)
    html = _replace_span(html, "doc-title",    esc(doc_title))
    html = _replace_span(html, "doc-period",   esc(period))
    html = _replace_span(html, "doc-audience", audience)   # may contain &amp; literal
    html = re.sub(
        r"<!-- BEGIN SCHEDULE -->.*?<!-- END SCHEDULE -->",
        "<!-- BEGIN SCHEDULE -->\n" + schedule_html + "<!-- END SCHEDULE -->",
        html, flags=re.DOTALL)
    html = re.sub(r'(<div\s+id="footnotes"[^>]*>)[^<]*(</div>)',
                  r"\g<1>" + footnotes + r"\g<2>", html)
    html = re.sub(r'(<div\s+id="genstamp"[^>]*>)[^<]*(</div>)',
                  r"\g<1>" + esc(genstamp) + r"\g<2>", html)
    return html


# --------------------------------------------------------------------------- #
# Document builders
# --------------------------------------------------------------------------- #
def build_lap_swim_doc(template, period, genstamp, data):
    """DOC A — Lap Swim windows for Settlemier Competition Pool only."""
    sett = data.get("settlemier", {})
    headers = sett.get("headers", [])
    rows    = _filter_rows(sett.get("rows", []), ["lap swim"])

    schedule_html = '<div class="pool">\n<h2>Settlemier Competition Pool</h2>\n'
    if rows:
        schedule_html += _render_table(headers, rows)
    else:
        schedule_html += "<p>No lap swim windows found.</p>\n"
    schedule_html += "</div>\n"

    return _build_doc(template, "Lap Swim", period, "Pete &amp; Stacy",
                      schedule_html, genstamp)


def build_rec_swim_doc(template, period, genstamp, data):
    """DOC B — Open/Rec Swim windows across both pools for Niko."""
    lesson_kw = ["lesson", "class", "camp", "conditioning"]

    # ---- Settlemier block ----
    sett = data.get("settlemier", {})
    sett_headers = sett.get("headers", [])
    sett_rows = _filter_rows(sett.get("rows", []), ["open swim", "rec swim"])

    sched = '<div class="pool">\n<h2>Settlemier Competition Pool</h2>\n'
    if sett_rows:
        sched += _render_table(sett_headers, sett_rows)
    else:
        sched += "<p>No open/rec swim windows found.</p>\n"
    sched += "</div>\n"

    # ---- Activity Pool block ----
    ap = data.get("activity_pool", {})
    sched += '<div class="pool">\n<h2>Activity Pool (Warm Water)</h2>\n'

    for sub_label, sub_data in ap.items():
        ap_headers = sub_data.get("headers", [])
        ap_all_rows = sub_data.get("rows", [])

        # Separate open swim and lesson rows
        open_rows   = _filter_rows(ap_all_rows, ["open swim"])
        lesson_rows = _filter_rows(ap_all_rows, lesson_kw)

        # Skip sections with no relevant rows
        if not open_rows and not lesson_rows:
            continue

        sched += f'<p class="sub-label">{esc(sub_label)}</p>\n'
        display_rows = open_rows + [
            [["⚠ " + r[0][0]] + r[0][1:]] + r[1:]
            for r in lesson_rows
        ]
        sched += _render_table(ap_headers, display_rows, lesson_keywords=lesson_kw)

    sched += "</div>\n"

    footnotes = ("All times Pacific. Subject to change — verify at piedmont.ca.gov/pool. "
                 "Yellow rows: pool unavailable for open swim during lesson/class windows.")
    return _build_doc(template, "Rec Swim", period, "Niko",
                      sched, genstamp, footnotes=footnotes)


# --------------------------------------------------------------------------- #
# Plain-text summary for email body
# --------------------------------------------------------------------------- #
def _summarise(data):
    """Return a short plain-text summary of lap and rec windows."""
    lines = []

    # Lap swim (Settlemier)
    sett = data.get("settlemier", {})
    lap_rows = _filter_rows(sett.get("rows", []), ["lap swim"])
    if lap_rows:
        headers = sett.get("headers", [])
        day_names = [h[0] for h in headers[1:]] if len(headers) > 1 else []
        lines.append("LAP SWIM — Settlemier Competition Pool")
        for row in lap_rows:
            act = row[0][0] if row[0] else ""
            for i, cell in enumerate(row[1:]):
                day = day_names[i] if i < len(day_names) else f"col{i+1}"
                times = ", ".join(cell) if cell else "—"
                lines.append(f"  {day}: {times}")
        lines.append("")

    # Rec swim (Settlemier)
    rec_rows = _filter_rows(sett.get("rows", []), ["open swim", "rec swim"])
    if rec_rows:
        headers = sett.get("headers", [])
        day_names = [h[0] for h in headers[1:]] if len(headers) > 1 else []
        lines.append("REC / OPEN SWIM — Settlemier Competition Pool")
        for row in rec_rows:
            act = row[0][0] if row[0] else ""
            lines.append(f"  [{act}]")
            for i, cell in enumerate(row[1:]):
                day = day_names[i] if i < len(day_names) else f"col{i+1}"
                times = ", ".join(cell) if cell else "—"
                lines.append(f"    {day}: {times}")
        lines.append("")

    # Activity Pool
    ap = data.get("activity_pool", {})
    for sub_label, sub_data in ap.items():
        open_rows = _filter_rows(sub_data.get("rows", []), ["open swim"])
        if open_rows:
            ap_headers = sub_data.get("headers", [])
            day_names = [h[0] for h in ap_headers[1:]] if len(ap_headers) > 1 else []
            lines.append(f"REC / OPEN SWIM — Activity Pool ({sub_label})")
            for row in open_rows:
                for i, cell in enumerate(row[1:]):
                    day = day_names[i] if i < len(day_names) else f"col{i+1}"
                    times = ", ".join(cell) if cell else "—"
                    lines.append(f"  {day}: {times}")
            lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# SendGrid email
# --------------------------------------------------------------------------- #
def _sendgrid_request(key, payload):
    """POST payload to SendGrid v3 mail/send. Returns HTTP status."""
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    try:
        status = urllib.request.urlopen(req).status
        print(f"sendgrid: sent, HTTP {status}")
        return status
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        # Never print the key; log the response body only
        print(f"sendgrid: send failed HTTP {e.code}: {body}")
        return e.code


def send_schedule_email(key, period, lap_html, rec_html):
    lap_b64 = base64.b64encode(lap_html.encode("utf-8")).decode()
    rec_b64 = base64.b64encode(rec_html.encode("utf-8")).decode()

    payload = {
        "personalizations": [{"to": [{"email": MAIL_TO}]}],
        "from": MAIL_FROM,
        "subject": f"Piedmont Pool Schedule — {period}",
        "content": [{"type": "text/plain", "value":
            "Two schedule attachments:\n"
            "  • piedmont-lap-swim.html — Lap Swim (Pete & Stacy)\n"
            "  • piedmont-rec-swim.html — Rec Swim (Niko)\n\n"
            "Open each attachment and print; it auto-fits to one page.\n\n"
            f"Source: piedmont.ca.gov/pool | Period: {period}"}],
        "attachments": [
            {"content": lap_b64, "type": "text/html",
             "filename": "piedmont-lap-swim.html", "disposition": "attachment"},
            {"content": rec_b64, "type": "text/html",
             "filename": "piedmont-rec-swim.html", "disposition": "attachment"},
        ],
    }
    return _sendgrid_request(key, payload)


def send_warning_email(key, schedule_end, days_over,
                       extraction_failed=False, raw_html=None):
    if extraction_failed:
        subject = "Piedmont pool schedule — extraction failed"
        body = (f"The pool schedule routine ran today but could not confidently "
                f"parse the schedule from piedmont.ca.gov/pool. "
                f"The page structure may have changed. Raw HTML is attached.")
    else:
        subject = "Piedmont pool schedule overdue"
        body = (f"The last posted Piedmont Community Pool schedule ended "
                f"{schedule_end} and is now {days_over} day(s) overdue "
                f"with no new posting found on the city website.")

    payload = {
        "personalizations": [{"to": [{"email": MAIL_TO}]}],
        "from": MAIL_FROM,
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    if extraction_failed and raw_html:
        raw_b64 = base64.b64encode(raw_html.encode("utf-8", "replace")).decode()
        payload["attachments"] = [
            {"content": raw_b64, "type": "text/html",
             "filename": "pool-page-raw.html", "disposition": "attachment"},
        ]

    return _sendgrid_request(key, payload)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def require_key():
    key = os.environ.get("SENDGRID_API_KEY")
    if not key:
        sys.exit("ERROR: SENDGRID_API_KEY is not set.")
    return key


def main():
    manual = len(sys.argv) > 1 and sys.argv[1] == "MANUAL_RESEND"

    state = load_state()
    today = today_la()
    schedule_end = datetime.date.fromisoformat(state["schedule_end"])

    # Step 1 — freshness check
    if today <= schedule_end and not manual:
        print(f"today {today} <= schedule_end {schedule_end}; nothing to do.")
        return

    # Step 2 — overdue cadence (skip if manual)
    if not manual:
        days_over = (today - schedule_end).days
        if days_over > WARN_DAYS:
            key = require_key()
            send_warning_email(key, state["schedule_end"], days_over)
            return
        if days_over not in SEND_DAYS:
            state["attempts"] = state.get("attempts", 0) + 1
            save_state(state)
            git_commit(["state.json"],
                       f"routine: no-send day D={days_over}, attempts={state['attempts']}")
            print(f"D={days_over} not in send days; attempts -> {state['attempts']}")
            return

    # Step 3 — fetch
    print(f"fetching {PAGE_URL}")
    raw_html = fetch_html()

    # Step 4 — period check
    label, period_end = parse_period(raw_html)
    print(f"period: {label!r}  end: {period_end}")

    if (period_end is not None
            and period_end.isoformat() == state["schedule_end"]
            and not manual):
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        git_commit(["state.json"],
                   f"routine: schedule unchanged ({label}), attempts={state['attempts']}")
        print("schedule period unchanged; attempts incremented.")
        return

    # Step 5 — extract and build
    data = extract_schedule(raw_html)

    if data is None:
        key = require_key()
        send_warning_email(key, state["schedule_end"], 0,
                           extraction_failed=True, raw_html=raw_html)
        print("extraction failed; WARNING email sent; state.json unchanged.")
        return

    if not data.get("settlemier") and not data.get("activity_pool"):
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        git_commit(["state.json"],
                   f"routine: no schedule found, attempts={state['attempts']}")
        print("no schedule found; attempts incremented.")
        return

    effective_label = label or state.get("schedule_label", "Current Schedule")
    genstamp = f"Generated {today.isoformat()} from piedmont.ca.gov/pool"
    template = open(TMPL_PATH, encoding="utf-8").read()

    lap_html = build_lap_swim_doc(template, effective_label, genstamp, data)
    rec_html = build_rec_swim_doc(template, effective_label, genstamp, data)

    with open(LAP_PATH, "w", encoding="utf-8") as f:
        f.write(lap_html)
    with open(REC_PATH, "w", encoding="utf-8") as f:
        f.write(rec_html)

    print(f"wrote {LAP_PATH}")
    print(f"wrote {REC_PATH}")

    is_new = (period_end is not None
              and period_end.isoformat() != state["schedule_end"])
    if is_new:
        state["schedule_end"]   = period_end.isoformat()
        state["schedule_label"] = label or effective_label
        state["attempts"]       = 0
        save_state(state)
        git_commit(["lap-swim.html", "rec-swim.html", "state.json"],
                   f"schedule: {label} (ends {period_end})")
        print(f"state updated: schedule_end -> {period_end}")

    # Step 6 — send email
    key = require_key()
    status = send_schedule_email(key, effective_label, lap_html, rec_html)
    if status != 202:
        print(f"WARNING: SendGrid returned HTTP {status} (expected 202).")


if __name__ == "__main__":
    main()
