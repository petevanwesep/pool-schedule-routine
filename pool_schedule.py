#!/usr/bin/env python3
"""Piedmont Community Pool schedule routine.

Fetches the City of Piedmont pool page, parses the published swim schedule,
renders two print-ready one-page HTML attachments (lap-swim.html, rec-swim.html),
and emails them via the SendGrid v3 API.

Secrets: the SendGrid API key is read from the SENDGRID_API_KEY environment
variable. It is NEVER hardcoded, logged, or written to disk.

Usage:
    SENDGRID_API_KEY=... python3 pool_schedule.py             # normal cadence run
    SENDGRID_API_KEY=... python3 pool_schedule.py MANUAL_RESEND  # force re-send
    python3 pool_schedule.py --dry-run                           # parse + write, no send
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
REPO_DIR   = os.path.dirname(os.path.abspath(__file__))
TEMPLATE   = os.path.join(REPO_DIR, "pool-schedule-template.html")
STATE_PATH = os.path.join(REPO_DIR, "state.json")
LAP_HTML   = os.path.join(REPO_DIR, "lap-swim.html")
REC_HTML   = os.path.join(REPO_DIR, "rec-swim.html")
TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

MAIL_TO   = "petervanwesep@gmail.com"
MAIL_FROM = {"email": "pool@pjvw.io", "name": "Piedmont Pool Schedule"}

SEND_DAYS   = {1, 2, 3, 5, 8, 13, 21}
WARN_AFTER  = 30

MONTHS = {m: i for i, m in enumerate(
    "January February March April May June "
    "July August September October November December".split(), start=1)}

DAY_ABBR = {
    "Monday":    "Mon–Thu",
    "Tuesday":   "Tue",
    "Wednesday": "Wed",
    "Thursday":  "Thu",
    "Friday":    "Fri",
    "Saturday":  "Sat",
    "Sunday":    "Sun",
}


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def today_la():
    return datetime.datetime.now(TZ).date()


# ── Fetch & parse ─────────────────────────────────────────────────────────────

def fetch_html():
    req = urllib.request.Request(PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
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
    """Normalise a messy <td> into a list of display lines."""
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
    _season, year, _sm, _sd, em, ed = m.groups()
    label = re.sub(r"\s+", " ", m.group(0)).strip()
    try:
        end = datetime.date(int(year), MONTHS[em], int(ed))
    except (KeyError, ValueError):
        end = None
    return label, end


def parse_schedule(html):
    """Return (label, end_date, sections).

    sections: list of {"title": str, "rows": list[list[list[str]]]}
    Each row is a list of cells; each cell is a list of display lines.
    Row 0 is the header row.
    """
    anchor = html.find("Lap Swim &amp; Open Swim Schedules")
    if anchor == -1:
        anchor = html.find("Lap Swim & Open Swim Schedules")
    chunk = html[anchor:anchor + 60000] if anchor != -1 else html

    label, end = parse_period(html)

    sections = []
    token_re = re.compile(
        r"<h(2|3|4)[^>]*>(.*?)</h\1>|<table[^>]*>(.*?)</table>",
        re.DOTALL | re.IGNORECASE)
    pool = None
    sub  = None
    for tok in token_re.finditer(chunk):
        if tok.group(3) is None:                          # heading
            level = int(tok.group(1))
            title = _clean(tok.group(2))
            if not title or (label and title == label):
                continue
            if level <= 3:
                pool, sub = title, None
            else:
                sub = title
        else:                                             # table
            rows = []
            for row_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", tok.group(3),
                                     re.DOTALL | re.IGNORECASE):
                cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>",
                                   row_m.group(1), re.DOTALL | re.IGNORECASE)
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


def _find_section(sections, keyword):
    kw = keyword.lower()
    for s in sections:
        if kw in s["title"].lower():
            return s
    return None


# ── Template fill ─────────────────────────────────────────────────────────────

def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _set_id(html, elem_id, content):
    """Replace inner content of the first element matching id=elem_id."""
    def repl(m):
        return m.group(1) + content + m.group(3)
    return re.sub(
        r'(<[^>]+\bid=["\']' + re.escape(elem_id) + r'["\'][^>]*>)(.*?)(</\w+>)',
        repl, html, count=1, flags=re.DOTALL)


def fill_template(tmpl, *, title, period, audience, schedule_html,
                  footnotes, genstamp):
    html = tmpl
    # <title> in <head>
    html = re.sub(r'(<title[^>]*>)[^<]*(</title>)',
                  lambda m: m.group(1) + _esc(title) + m.group(2),
                  html, count=1, flags=re.IGNORECASE)
    html = _set_id(html, "doc-title",    _esc(title))
    html = _set_id(html, "doc-period",   _esc(period))
    html = _set_id(html, "doc-audience", audience)   # may contain &amp; already
    html = re.sub(
        r'<!--\s*BEGIN SCHEDULE\s*-->.*?<!--\s*END SCHEDULE\s*-->',
        lambda _: '<!-- BEGIN SCHEDULE -->\n' + schedule_html + '\n<!-- END SCHEDULE -->',
        html, count=1, flags=re.DOTALL)
    html = _set_id(html, "footnotes", footnotes)
    html = _set_id(html, "genstamp",  _esc(genstamp))
    return html


# ── Schedule renderers ────────────────────────────────────────────────────────

def _short_day(cell_text):
    """'Monday-Thursday 6am-1pm; 2pm-7pm' → 'Mon–Thu'."""
    for day, abbr in DAY_ABBR.items():
        if cell_text.lower().startswith(day.lower()):
            return abbr
    return cell_text.split()[0] if cell_text else cell_text


def _th(text):
    return f"<th>{_esc(text)}</th>"


def _td(lines, cls=""):
    inner = "<br>".join(_esc(l) for l in lines)
    open_tag = f'<td class="{cls}">' if cls else "<td>"
    return f"{open_tag}{inner}</td>"


def _row_html(cells, is_lesson=False):
    tr_cls = ' class="lesson-row"' if is_lesson else ""
    tds = "".join(
        "<td>" + "<br>".join(_esc(l) for l in cell) + "</td>"
        for cell in cells
    )
    return f"<tr{tr_cls}>{tds}</tr>\n"


def _table(header_row, data_rows):
    """Render a <table> with short day-name headers."""
    ths = _th(_esc_cell(header_row[0]))
    for cell in header_row[1:]:
        ths += _th(_short_day(" ".join(cell)))
    lines = [f"<table>\n<tr>{ths}</tr>\n"]
    for row, is_lesson in data_rows:
        lines.append(_row_html(row, is_lesson))
    lines.append("</table>")
    return "".join(lines)


def _esc_cell(cell):
    return " ".join(cell)


def _pool_block(heading, inner_html):
    return f'<div class="pool">\n<h2>{_esc(heading)}</h2>\n{inner_html}\n</div>\n'


def _filter_rows(rows, keep_kws, lesson_kws=()):
    """Filter rows[1:] by keywords; mark lesson rows."""
    result = []
    for row in rows[1:]:
        act = " ".join(row[0]).lower() if row else ""
        if not any(k in act for k in keep_kws):
            continue
        is_lesson = bool(lesson_kws) and any(k in act for k in lesson_kws)
        result.append((row, is_lesson))
    return result


# ── Doc A: Lap Swim ───────────────────────────────────────────────────────────

def render_lap_swim(tmpl, label, sections, today_str):
    comp = _find_section(sections, "Settlemier")
    if not comp:
        return None, "Settlemier Competition Pool section not found"

    rows = comp["rows"]
    lap_rows = _filter_rows(rows, keep_kws=["lap swim"], lesson_kws=[])
    if not lap_rows or not rows:
        return None, "Lap Swim row not found in Competition Pool"

    table_html = _table(rows[0], lap_rows)
    schedule = _pool_block("Settlemier Competition Pool", table_html)

    # Plain-text summary for email body
    hdr = rows[0]
    summary_parts = []
    for row, _ in lap_rows:
        act = _esc_cell(row[0])
        for i, cell in enumerate(row[1:], 1):
            day = _short_day(" ".join(hdr[i])) if i < len(hdr) else f"col{i}"
            summary_parts.append(f"  {day}: {' / '.join(cell)}")
    summary = f"Lap Swim — Settlemier Competition Pool:\n" + "\n".join(summary_parts)

    html = fill_template(
        tmpl,
        title="Piedmont Pool — Lap Swim",
        period=label,
        audience="Pete &amp; Stacy",
        schedule_html=schedule,
        footnotes="",
        genstamp=f"Generated {today_str} from piedmont.ca.gov/pool",
    )
    return html, summary


# ── Doc B: Rec Swim ───────────────────────────────────────────────────────────

def render_rec_swim(tmpl, label, sections, today_str):
    comp     = _find_section(sections, "Settlemier")
    act_lap  = _find_section(sections, "Lap Side")
    act_zero = _find_section(sections, "Zero-Depth")

    if not comp:
        return None, "Settlemier Competition Pool section not found"

    # ── Competition Pool: Open Swim + Rec Swim (tube time) ──
    comp_rows = _filter_rows(comp["rows"],
                             keep_kws=["open swim", "rec swim"],
                             lesson_kws=[])
    comp_table = _table(comp["rows"][0], comp_rows)
    comp_block = _pool_block("Settlemier Competition Pool", comp_table)

    # ── Activity Pool: Lap Side + Zero-Depth in one .pool block ──
    activity_inner = ""
    if act_lap:
        lap_rows = _filter_rows(
            act_lap["rows"],
            keep_kws=["open swim", "lessons", "classes", "camps"],
            lesson_kws=["lessons", "classes", "camps"])
        if lap_rows:
            activity_inner += '<p class="pool-sub">Lap Side</p>\n'
            activity_inner += _table(act_lap["rows"][0], lap_rows)

    if act_zero:
        zero_rows = _filter_rows(
            act_zero["rows"],
            keep_kws=["open swim", "lessons", "classes", "camps"],
            lesson_kws=["lessons", "classes", "camps"])
        if zero_rows:
            activity_inner += '<p class="pool-sub">Zero-Depth Area</p>\n'
            activity_inner += _table(act_zero["rows"][0], zero_rows)

    activity_block = _pool_block("Activity Pool (Warm Water)", activity_inner) if activity_inner else ""

    schedule = comp_block + activity_block

    footnote = ("&#9733; Shaded rows = swim lessons / camps in session; "
                "open swim unavailable during those windows.")

    # Plain-text summary
    lines = ["Rec Swim — Settlemier Competition Pool:"]
    for row, _ in comp_rows:
        act = _esc_cell(row[0])
        times = " | ".join(" ".join(c) for c in row[1:])
        lines.append(f"  {act}: {times}")
    if act_lap:
        lines.append("Activity Pool (Lap Side):")
        for row, is_lesson in (_filter_rows(act_lap["rows"],
                                            keep_kws=["open swim", "lessons", "classes", "camps"],
                                            lesson_kws=["lessons", "classes", "camps"])):
            act = _esc_cell(row[0])
            times = " | ".join(" ".join(c) for c in row[1:])
            tag = " [LESSONS]" if is_lesson else ""
            lines.append(f"  {act}{tag}: {times}")
    if act_zero:
        lines.append("Activity Pool (Zero-Depth):")
        for row, is_lesson in (_filter_rows(act_zero["rows"],
                                            keep_kws=["open swim", "lessons", "classes", "camps"],
                                            lesson_kws=["lessons", "classes", "camps"])):
            act = _esc_cell(row[0])
            times = " | ".join(" ".join(c) for c in row[1:])
            tag = " [LESSONS]" if is_lesson else ""
            lines.append(f"  {act}{tag}: {times}")
    summary = "\n".join(lines)

    html = fill_template(
        tmpl,
        title="Piedmont Pool — Rec Swim",
        period=label,
        audience="Niko",
        schedule_html=schedule,
        footnotes=footnote,
        genstamp=f"Generated {today_str} from piedmont.ca.gov/pool",
    )
    return html, summary


# ── SendGrid ──────────────────────────────────────────────────────────────────

def _sendgrid(key, subject, body, attachments=None):
    payload = {
        "personalizations": [{"to": [{"email": MAIL_TO}]}],
        "from": MAIL_FROM,
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    if attachments:
        payload["attachments"] = attachments

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    try:
        status = urllib.request.urlopen(req).status
        print(f"sent {status}")
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:500]
        print(f"send failed {e.code} {err}")
        sys.exit(1)


def send_schedule(key, label, lap_summary, rec_summary):
    subject = f"Piedmont Pool Schedule — {label}"
    body = (
        f"Piedmont Community Pool · {label}\n\n"
        f"{lap_summary}\n\n"
        f"{rec_summary}\n\n"
        "Open each attachment and print; it auto-fits to one page."
    )
    atts = []
    for path, name in [(LAP_HTML, "piedmont-lap-swim.html"),
                       (REC_HTML, "piedmont-rec-swim.html")]:
        b = base64.b64encode(open(path, "rb").read()).decode()
        atts.append({"content": b, "type": "text/html",
                     "filename": name, "disposition": "attachment"})
    _sendgrid(key, subject, body, atts)


def send_warning(key, schedule_end, days_over=None,
                 raw_html=None, extraction_failed=False):
    subject = "Piedmont pool schedule overdue"
    if extraction_failed:
        body = (
            f"The Piedmont pool page was fetched but the schedule could not be "
            f"extracted — the page structure may have changed.  "
            f"Last known schedule ended {schedule_end}.  "
            "Raw HTML attached for inspection."
        )
    else:
        body = (
            f"The last posted Piedmont Community Pool schedule ended {schedule_end} "
            f"and is now {days_over} days overdue with no new posting on the city website."
        )
    atts = None
    if raw_html:
        b = base64.b64encode(raw_html.encode()).decode()
        atts = [{"content": b, "type": "text/html",
                 "filename": "piedmont-pool-page.html", "disposition": "attachment"}]
    _sendgrid(key, subject, body, atts)


# ── Git helper ────────────────────────────────────────────────────────────────

def git_commit_schedule(label):
    subprocess.run(
        ["git", "add", "lap-swim.html", "rec-swim.html", "state.json"],
        check=True, cwd=REPO_DIR)
    subprocess.run(
        ["git", "commit", "-m", f"Update pool schedule: {label}"],
        check=True, cwd=REPO_DIR)
    print("Committed updated schedule files.")


# ── Main ──────────────────────────────────────────────────────────────────────

def require_key():
    key = os.environ.get("SENDGRID_API_KEY")
    if not key:
        sys.exit("ERROR: SENDGRID_API_KEY is not set.")
    return key


def main():
    manual_resend = "MANUAL_RESEND" in sys.argv
    dry_run       = "--dry-run" in sys.argv

    state = load_state()
    today = today_la()
    schedule_end = datetime.date.fromisoformat(state["schedule_end"])

    # ── MANUAL_RESEND: skip freshness checks ──────────────────────────────────
    if manual_resend:
        # If previously generated files exist, just resend them as-is.
        if os.path.exists(LAP_HTML) and os.path.exists(REC_HTML):
            print("MANUAL_RESEND: resending existing schedule files.")
            key = require_key()
            # Rebuild summaries from existing HTML would be complex; use simple body.
            label = state.get("schedule_label", state["schedule_end"])
            send_schedule(key, label, "(see attachment)", "(see attachment)")
            return
        # Files don't exist yet — fall through to fetch + generate.
        print("MANUAL_RESEND: no existing files; fetching and generating.")

    # ── Step 1: freshness check ───────────────────────────────────────────────
    if not manual_resend and not dry_run and today <= schedule_end:
        print(f"today {today} <= schedule_end {schedule_end}; nothing to do.")
        return

    # ── Step 2: D-day cadence check ───────────────────────────────────────────
    days_over = (today - schedule_end).days
    if not manual_resend and not dry_run and days_over > 0:
        if days_over > WARN_AFTER:
            key = require_key()
            send_warning(key, state["schedule_end"], days_over)
            return
        if days_over not in SEND_DAYS:
            state["attempts"] = state.get("attempts", 0) + 1
            save_state(state)
            print(f"D={days_over} not in send cadence; attempts → {state['attempts']}.")
            return

    # ── Step 3: fetch ─────────────────────────────────────────────────────────
    print("Fetching pool schedule page…")
    raw_html = fetch_html()

    # ── Step 4: parse period ──────────────────────────────────────────────────
    label, end, sections = parse_schedule(raw_html)
    print(f"period: {label}  end: {end}  sections: {len(sections)}")

    # Same schedule as state — exit quietly (unless manual/dry-run)
    if (end is not None and end.isoformat() == state["schedule_end"]
            and not manual_resend and not dry_run):
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        print(f"Schedule period unchanged; attempts → {state['attempts']}.")
        return

    # ── Step 5: extract & render ──────────────────────────────────────────────
    if not sections:
        print("WARNING: extraction failed — no sections found on page.")
        if not dry_run:
            key = require_key()
            send_warning(key, state["schedule_end"],
                         extraction_failed=True, raw_html=raw_html)
        return

    tmpl = open(TEMPLATE).read()
    today_str = today.isoformat()
    use_label = label or state.get("schedule_label", state["schedule_end"])

    lap_html, lap_summary = render_lap_swim(tmpl, use_label, sections, today_str)
    rec_html, rec_summary = render_rec_swim(tmpl, use_label, sections, today_str)

    if lap_html is None or rec_html is None:
        print(f"WARNING: extraction failed — lap={lap_summary}  rec={rec_summary}")
        if not dry_run:
            key = require_key()
            send_warning(key, state["schedule_end"],
                         extraction_failed=True, raw_html=raw_html)
        return

    with open(LAP_HTML, "w") as f:
        f.write(lap_html)
    with open(REC_HTML, "w") as f:
        f.write(rec_html)
    print(f"Wrote {LAP_HTML}")
    print(f"Wrote {REC_HTML}")

    if dry_run:
        print("\n--- Lap Swim summary ---")
        print(lap_summary)
        print("\n--- Rec Swim summary ---")
        print(rec_summary)
        return

    is_new = end is not None and end.isoformat() != state["schedule_end"]
    if is_new:
        state["schedule_end"]   = end.isoformat()
        state["schedule_label"] = label
        state["attempts"]       = 0
        save_state(state)
        print(f"State updated: schedule_end → {end.isoformat()}")
        git_commit_schedule(label)

    # ── Step 6: send ──────────────────────────────────────────────────────────
    key = require_key()
    send_schedule(key, use_label, lap_summary, rec_summary)


if __name__ == "__main__":
    main()
