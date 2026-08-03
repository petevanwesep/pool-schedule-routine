#!/usr/bin/env python3
"""Piedmont Community Pool — Competition Pool schedule routine.

Fetches the City of Piedmont pool page, parses the *Competition Pool* schedule
(the current week plus the future date-range schedules that the page hides
inside a JavaScript tab widget) together with the schedule exceptions that
affect the competition pool (water-polo closures, special events, holiday
hours), and produces two things:

  1. `competition_pool.ics` — a subscribable calendar feed. Every lap-swim,
     rec/open-swim and team-practice session becomes a weekly recurring event;
     every exception becomes a dated event. Subscribe to the published feed in
     Google/Apple Calendar and it stays current on its own.
  2. An optional one-page HTML email digest (via Resend), sent only when the
     competition-pool schedule actually changes, with the .ics attached.

Only the competition pool is tracked — the activity pool is ignored.

Secrets: the Resend API key is read from RESEND_API_KEY. It is NEVER hardcoded,
logged, or written to disk. This repo is public; do not commit any key.

Usage:
    python3 pool_schedule.py                       # regen .ics; email if changed
    RESEND_API_KEY=... python3 pool_schedule.py    # (needs the key to email)
    RESEND_API_KEY=... python3 pool_schedule.py --manual   # force re-send now
    python3 pool_schedule.py --dry-run             # regen .ics + preview, no send
    python3 pool_schedule.py --no-email            # regen .ics only, never email
"""

import argparse
import datetime
import hashlib
import html as htmlmod
import json
import os
import re
import sys
import time
import zoneinfo

PAGE_URL = ("https://piedmont.ca.gov/cms/One.aspx"
            "?portalId=13659823&pageId=16935826")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
ICS_PATH = os.path.join(HERE, "competition_pool.ics")
TZ = zoneinfo.ZoneInfo("America/Los_Angeles")
TZID = "America/Los_Angeles"

MAIL_TO = os.environ.get("MAIL_TO", "petervanwesep@gmail.com")
# Resend's shared onboarding domain works with zero DNS setup and can send to
# the account owner's address. Switch to "Piedmont Pool <pool@pjvw.io>" once
# pjvw.io is verified in Resend.
MAIL_FROM = os.environ.get(
    "MAIL_FROM", "Piedmont Competition Pool <onboarding@resend.dev>")

# Public URL where competition_pool.ics is served (for the email's subscribe
# link). Defaults to the raw file on the repo's main branch, which works as
# soon as the committed .ics lands on main. Override with ICS_FEED_URL to point
# at a GitHub Pages URL (cleaner MIME type) once Pages is enabled.
ICS_FEED_URL = os.environ.get(
    "ICS_FEED_URL",
    "https://raw.githubusercontent.com/petevanwesep/pool-schedule-routine/"
    "main/competition_pool.ics")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday"]
WEEKDAY_ICS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

MONTHS = {}
for _i, _names in enumerate([
        ("January", "Jan"), ("February", "Feb"), ("March", "Mar"),
        ("April", "Apr"), ("May",), ("June", "Jun"), ("July", "Jul"),
        ("August", "Aug"), ("September", "Sept", "Sep"), ("October", "Oct"),
        ("November", "Nov"), ("December", "Dec")], start=1):
    for _n in _names:
        MONTHS[_n.lower()] = _i


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def today_la():
    return datetime.datetime.now(TZ).date()


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_html():
    import urllib.request
    req = urllib.request.Request(PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #
def strip_comments(h):
    return re.sub(r"<!--.*?-->", "", h, flags=re.DOTALL)


def strip_scripts(h):
    return re.sub(r"(?is)<script\b.*?</script>", "", h)


def unescape_js(s):
    return (s.replace('\\"', '"').replace("\\/", "/").replace("\\n", "\n")
             .replace("\\t", " ").replace("\\r", "").replace("\\\\", "\\"))


def html_fragments(raw):
    """The real DOM (comments + scripts stripped) plus each JavaScript tab
    'content' payload, decoded.

    The page's future competition-pool schedules live only inside a
    `scrollingTabs({tabs:[{content:"..."}]})` call as escaped HTML strings.
    Stripping <script> from the DOM copy avoids parsing those escaped tables
    twice; decoding each payload recovers the real future schedules.
    """
    frags = [strip_scripts(strip_comments(raw))]
    for m in re.finditer(r'"content":"(.*?)"\}', raw, re.DOTALL):
        frags.append(unescape_js(m.group(1)))
    return frags


def text_of(fragment):
    t = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    t = re.sub(r"(?i)</(li|p|div)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = htmlmod.unescape(t)
    t = t.replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


# --------------------------------------------------------------------------- #
# Parse competition-pool schedule blocks
# --------------------------------------------------------------------------- #
def parse_range_from_caption(cap):
    """Pull the two dates out of a table caption, e.g.
    'Competition Pool schedule, August 3, 2026 to August 9, 2026' or
    'Competition Pool weekly schedule, effective Aug 10, 2026 - Sept 30, 2026'.
    """
    def mk(mo, d, y):
        mi = MONTHS.get(mo.lower())
        if not mi:
            return None
        try:
            return datetime.date(int(y), mi, int(d))
        except ValueError:
            return None
    ds = [mk(*t) for t in re.findall(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", cap)]
    ds = [d for d in ds if d]
    if len(ds) >= 2:
        return ds[0], ds[1]
    return None, None


def parse_comp_blocks(raw):
    """Return an ordered list of competition-pool schedule blocks:
    {start, end, caption, activities: [(name, {weekday_idx: [slot, ...]})]}.
    """
    tables = []
    for frag in html_fragments(raw):
        tables += re.findall(r"<table[^>]*>(.*?)</table>", frag,
                             re.DOTALL | re.IGNORECASE)
    blocks, seen = [], set()
    for body in tables:
        head = body[:body.lower().find("<tr")] if "<tr" in body.lower() else body[:200]
        caption = text_of(head)
        if "competition pool" not in caption.lower():
            continue
        start, end = parse_range_from_caption(caption)
        if not (start and end):
            continue
        if (start, end) in seen:
            continue
        seen.add((start, end))

        rows = []
        for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", body, re.DOTALL | re.IGNORECASE):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rm.group(1),
                               re.DOTALL | re.IGNORECASE)
            rows.append([text_of(c) for c in cells])
        rows = [r for r in rows if any(c.strip() for c in r)]
        if len(rows) < 2:
            continue

        header = rows[0]
        col_day = {}
        for ci, ch in enumerate(header):
            for di, dn in enumerate(DAYS):
                if dn.lower() in ch.lower():
                    col_day[ci] = di
        if not col_day:
            continue

        activities = []
        for r in rows[1:]:
            activity = r[0].strip()
            if not activity:
                continue
            day_slots = {}
            for ci in range(1, len(r)):
                di = col_day.get(ci)
                if di is None:
                    continue
                slots = [ln.strip() for ln in r[ci].split("\n") if ln.strip()]
                slots = [s for s in slots if s not in ("-", "\u2013", "\u2014")]
                if slots:
                    day_slots[di] = slots
            if day_slots:
                activities.append((activity, day_slots))
        if activities:
            blocks.append({"start": start, "end": end,
                           "caption": caption, "activities": activities})
    blocks.sort(key=lambda b: b["start"])
    return blocks


# ---- time / slot parsing --------------------------------------------------- #
def parse_time(tok):
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([ap])$", tok.strip(), re.IGNORECASE)
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if ap == "a" and hh == 12:
        hh = 0
    elif ap == "p" and hh != 12:
        hh += 12
    try:
        return datetime.time(hh, mm)
    except ValueError:
        return None


def parse_slot(slot):
    """'6a-12p · 8 lanes' -> (start_time, end_time, description)."""
    parts = [p.strip() for p in re.split(r"[·|]", slot) if p.strip()]
    if not parts:
        return None, None, slot
    tm = re.split(r"[\u2013\u2014-]", parts[0])
    desc = " · ".join(parts[1:]).strip()
    if len(tm) != 2:
        return None, None, slot
    return parse_time(tm[0]), parse_time(tm[1]), (desc or parts[0])


# --------------------------------------------------------------------------- #
# Parse schedule exceptions (competition-pool relevant)
# --------------------------------------------------------------------------- #
def _h24(hh, ap):
    hh = int(hh)
    if ap == "a" and hh == 12:
        return 0
    if ap == "p" and hh != 12:
        return hh + 12
    return hh


def parse_exceptions(raw, default_year):
    h = strip_scripts(strip_comments(raw))
    start = h.find("Schedule Exceptions")
    if start == -1:
        return []
    nxt = h.find("<h2", start + 5)
    seg = h[start: nxt if nxt != -1 else start + 20000]

    out, cat = [], None
    for m in re.finditer(r"<h4[^>]*>(.*?)</h4>|<li[^>]*>(.*?)</li>", seg,
                         re.DOTALL | re.IGNORECASE):
        if m.group(1) is not None:
            cat = text_of(m.group(1))
            continue
        item = text_of(m.group(2)).replace("\n", " ").strip()
        dm = re.match(r"[A-Za-z]+,?\s+([A-Za-z]+)\.?\s+(\d{1,2})\s*[:,]?\s*(.*)", item)
        if not dm:
            continue
        mo = MONTHS.get(dm.group(1).lower())
        if not mo:
            continue
        # The pool "year" runs summer->winter; months before June belong to the
        # next calendar year relative to a summer-anchored schedule.
        year = default_year if mo >= 6 else default_year + 1
        try:
            d = datetime.date(year, mo, int(dm.group(2)))
        except ValueError:
            continue
        desc = dm.group(3).strip(" :")

        span = None
        tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*([ap])m",
                       item, re.IGNORECASE)
        if tm:
            ap = tm.group(5).lower()
            eh, em = _h24(tm.group(3), ap), int(tm.group(4) or 0)
            sh, sm = _h24(tm.group(1), ap), int(tm.group(2) or 0)
            if sh > eh:  # e.g. "2-8pm": the start is pm too
                sh = int(tm.group(1)) + (12 if int(tm.group(1)) != 12 else 0)
            try:
                span = (datetime.time(sh, sm), datetime.time(eh, em))
            except ValueError:
                span = None
        out.append({"category": cat, "date": d, "text": item,
                    "desc": desc, "span": span})
    return out


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #
def content_signature(blocks, exceptions):
    """Stable hash of the parsed competition-pool picture (no timestamps)."""
    payload = {
        "blocks": [
            {"start": b["start"].isoformat(), "end": b["end"].isoformat(),
             "activities": [[name, {str(k): v for k, v in ds.items()}]
                            for name, ds in b["activities"]]}
            for b in blocks],
        "exceptions": [
            {"date": e["date"].isoformat(), "category": e["category"],
             "text": e["text"]} for e in exceptions],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# ICS generation
# --------------------------------------------------------------------------- #
def _ics_fold(line):
    b = line.encode("utf-8")
    if len(b) <= 73:
        return line
    out = []
    while len(b) > 73:
        cut = 73
        while (b[cut] & 0xC0) == 0x80:  # never split a multibyte char
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def _ics_esc(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\n", "\\n"))


def _uid(*parts):
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]
    return f"{h}@piedmont-competition-pool"


def _local(d, t):
    return f"{d.strftime('%Y%m%d')}T{t.strftime('%H%M%S')}"


def build_ics(blocks, exceptions, now):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0",
         "PRODID:-//piedmont-pool-routine//competition-pool//EN",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         "X-WR-CALNAME:Piedmont Competition Pool",
         _ics_fold("X-WR-CALDESC:Competition Pool lap/rec/open swim, team "
                   "practice and closures — City of Piedmont"),
         "X-WR-TIMEZONE:" + TZID,
         "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
         "X-PUBLISHED-TTL:PT12H",
         "BEGIN:VTIMEZONE", "TZID:" + TZID,
         "BEGIN:DAYLIGHT", "TZOFFSETFROM:-0800", "TZOFFSETTO:-0700",
         "TZNAME:PDT", "DTSTART:19700308T020000",
         "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "END:DAYLIGHT",
         "BEGIN:STANDARD", "TZOFFSETFROM:-0700", "TZOFFSETTO:-0800",
         "TZNAME:PST", "DTSTART:19701101T020000",
         "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", "END:STANDARD",
         "END:VTIMEZONE"]

    def event(uid, summary, dtstart_lines, extra):
        L.append("BEGIN:VEVENT")
        L.append("UID:" + uid)
        L.append("DTSTAMP:" + stamp)
        L.extend(dtstart_lines)
        L.append(_ics_fold("SUMMARY:" + _ics_esc(summary)))
        L.extend(extra)
        L.append("END:VEVENT")

    # Recurring weekly sessions
    for b in blocks:
        until = datetime.datetime.combine(b["end"], datetime.time(23, 59, 59))
        for activity, day_slots in b["activities"]:
            for di, slots in day_slots.items():
                first = b["start"] + datetime.timedelta(
                    days=(di - b["start"].weekday()) % 7)
                for slot in slots:
                    st, en, desc = parse_slot(slot)
                    if not (st and en):
                        continue
                    # A session ending at/after midnight isn't expected here,
                    # but guard against an inverted range.
                    if en <= st:
                        continue
                    summary = f"{activity} · {desc}" if desc else activity
                    lines = [
                        f"DTSTART;TZID={TZID}:{_local(first, st)}",
                        f"DTEND;TZID={TZID}:{_local(first, en)}",
                        "RRULE:FREQ=WEEKLY;BYDAY=%s;UNTIL=%s" % (
                            WEEKDAY_ICS[di], until.strftime('%Y%m%dT%H%M%S')),
                    ]
                    event(_uid("slot", b["start"], b["end"], activity, di, slot),
                          summary, lines,
                          [_ics_fold("DESCRIPTION:" + _ics_esc(
                              f"{activity} — {slot}\nCompetition Pool")),
                           "CATEGORIES:" + _ics_esc(activity)])

    # Dated exceptions
    for e in exceptions:
        cat = e["category"] or "Exception"
        summary = f"[{cat}] {e['desc'] or cat}"
        if e["span"]:
            s, en = e["span"]
            lines = [f"DTSTART;TZID={TZID}:{_local(e['date'], s)}",
                     f"DTEND;TZID={TZID}:{_local(e['date'], en)}"]
        else:
            nd = e["date"] + datetime.timedelta(days=1)
            lines = [f"DTSTART;VALUE=DATE:{e['date'].strftime('%Y%m%d')}",
                     f"DTEND;VALUE=DATE:{nd.strftime('%Y%m%d')}"]
        event(_uid("exc", e["date"], e["text"]), summary, lines,
              [_ics_fold("DESCRIPTION:" + _ics_esc(e["text"])),
               "CATEGORIES:" + _ics_esc(cat)])

    L.append("END:VCALENDAR")
    return "\r\n".join(L) + "\r\n"


# --------------------------------------------------------------------------- #
# Render email digest
# --------------------------------------------------------------------------- #
def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_range(b):
    s, e = b["start"], b["end"]
    same_year = s.year == e.year
    a = s.strftime("%b %-d") if hasattr(s, "strftime") else str(s)
    a = s.strftime("%b %-d, %Y") if not same_year else s.strftime("%b %-d")
    return f"{a} – {e.strftime('%b %-d, %Y')}"


def render_html(blocks, exceptions, today):
    css = """
body{font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;max-width:820px;margin:0 auto;padding:12px}
h1{font-size:20px;margin:0 0 2px}
h2{font-size:15px;margin:20px 0 6px;color:#0b5394;border-bottom:2px solid #0b5394;padding-bottom:3px}
.range{color:#555;font-size:12px;margin:0 0 6px}
table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:6px}
th,td{border:1px solid #ccc;padding:5px 7px;text-align:left;vertical-align:top}
th{background:#0b5394;color:#fff;font-weight:600}
td:first-child,th:first-child{font-weight:600;background:#f3f6fb;white-space:nowrap}
.exc{font-size:12px;margin:2px 0}
.cat{font-weight:600;color:#0b5394}
.foot{font-size:11px;color:#888;margin-top:18px}
""".strip()
    P = ['<!DOCTYPE html><html><head><meta charset="utf-8"><style>', css,
         "</style></head><body>",
         "<h1>Piedmont Competition Pool</h1>"]
    if ICS_FEED_URL:
        P.append('<p class="range">Subscribe to the live calendar feed: '
                 f'<a href="{esc(ICS_FEED_URL)}">{esc(ICS_FEED_URL)}</a></p>')

    for b in blocks:
        if b["end"] < today:
            continue  # skip fully-past blocks in the email
        P.append(f"<h2>{esc(b['start'].strftime('%B %-d'))} – "
                 f"{esc(b['end'].strftime('%B %-d, %Y'))}</h2>")
        P.append("<table><tr><th>Activity</th>"
                 + "".join(f"<th>{d[:3]}</th>" for d in DAYS) + "</tr>")
        for activity, day_slots in b["activities"]:
            P.append(f"<tr><td>{esc(activity)}</td>")
            for di in range(7):
                slots = day_slots.get(di, [])
                P.append("<td>" + "<br>".join(esc(s) for s in slots) + "</td>")
            P.append("</tr>")
        P.append("</table>")

    upcoming = [e for e in exceptions if e["date"] >= today]
    if upcoming:
        P.append("<h2>Schedule exceptions</h2>")
        last_cat = None
        for e in upcoming:
            if e["category"] != last_cat:
                P.append(f'<div class="exc"><span class="cat">'
                         f'{esc(e["category"] or "Other")}</span></div>')
                last_cat = e["category"]
            P.append(f'<div class="exc">• '
                     f'{esc(e["date"].strftime("%a %b %-d"))}: '
                     f'{esc(e["desc"])}</div>')

    P.append('<div class="foot">Source: City of Piedmont · piedmont.ca.gov · '
             'competition pool only. Schedule subject to change — check the '
             'city website before visiting.</div></body></html>')
    return "".join(P)


def render_text(blocks, exceptions, today):
    out = ["Piedmont Competition Pool", ""]
    if ICS_FEED_URL:
        out += [f"Calendar feed: {ICS_FEED_URL}", ""]
    for b in blocks:
        if b["end"] < today:
            continue
        out.append(f"{b['start'].strftime('%B %-d')} - "
                   f"{b['end'].strftime('%B %-d, %Y')}")
        for activity, day_slots in b["activities"]:
            out.append(f"  {activity}")
            for di in range(7):
                slots = day_slots.get(di, [])
                if slots:
                    out.append(f"    {DAYS[di][:3]}: " + "; ".join(slots))
        out.append("")
    upcoming = [e for e in exceptions if e["date"] >= today]
    if upcoming:
        out.append("SCHEDULE EXCEPTIONS")
        for e in upcoming:
            out.append(f"  {e['date'].strftime('%a %b %-d')} "
                       f"[{e['category']}]: {e['desc']}")
        out.append("")
    out.append("Source: City of Piedmont - piedmont.ca.gov (competition pool)")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Send (Resend) + delivery verification
# --------------------------------------------------------------------------- #
def _resend_request(method, path, key, body=None):
    import subprocess
    import tempfile
    url = "https://api.resend.com" + path
    cmd = ["curl", "-s", "-w", "\n__STATUS__:%{http_code}", "-X", method, url,
           "-H", "Authorization: Bearer " + key,
           "-H", "Content-Type: application/json"]
    tmp = None
    if body is not None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(json.dumps(body))
            tmp = f.name
        cmd += ["-d", f"@{tmp}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = result.stdout
        if "\n__STATUS__:" in out:
            body_part, status_part = out.rsplit("\n__STATUS__:", 1)
            status = int(status_part.strip())
        else:
            body_part, status = out, 0
        return status, json.loads(body_part.strip() or "{}")
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def send_email(key, subject, html, text, ics=None):
    import base64
    body = {"from": MAIL_FROM, "to": [MAIL_TO], "subject": subject,
            "html": html, "text": text}
    if ics is not None:
        body["attachments"] = [{
            "filename": "competition_pool.ics",
            "content": base64.b64encode(ics.encode("utf-8")).decode("ascii"),
            "content_type": "text/calendar",
        }]
    status, resp = _resend_request("POST", "/emails", key, body)
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


def require_key():
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        sys.exit("ERROR: RESEND_API_KEY is not set. Export it before running "
                 "(do not hardcode or commit it).")
    return key


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", action="store_true",
                    help="force re-send even if the schedule is unchanged")
    ap.add_argument("--dry-run", action="store_true",
                    help="regenerate .ics + write preview.html, do not send")
    ap.add_argument("--no-email", action="store_true",
                    help="regenerate .ics only; never send email")
    args = ap.parse_args()

    today = today_la()
    html_page = fetch_html()
    blocks = parse_comp_blocks(html_page)
    if not blocks:
        state = load_state()
        state["attempts"] = state.get("attempts", 0) + 1
        save_state(state)
        sys.exit("ERROR: no competition-pool schedule found on the page "
                 "(layout may have changed again); attempts incremented.")

    base_year = blocks[0]["start"].year
    exceptions = parse_exceptions(html_page, base_year)
    sig = content_signature(blocks, exceptions)

    state = load_state()
    changed = sig != state.get("content_hash")

    # DTSTAMP (the "last revised" time) only advances when the content actually
    # changes, so an unchanged schedule regenerates a byte-identical .ics — no
    # spurious daily commits, and calendar clients don't see phantom updates.
    if changed or not state.get("revised_utc"):
        revised = now_utc()
    else:
        try:
            revised = datetime.datetime.fromisoformat(state["revised_utc"])
        except (ValueError, TypeError):
            revised = now_utc()

    ics = build_ics(blocks, exceptions, revised)
    with open(ICS_PATH, "w", newline="") as f:
        f.write(ics)
    n_events = ics.count("BEGIN:VEVENT")
    print(f"blocks={len(blocks)} exceptions={len(exceptions)} "
          f"ics_events={n_events} changed={changed} "
          f"-> wrote {os.path.basename(ICS_PATH)}")

    html = render_html(blocks, exceptions, today)
    text = render_text(blocks, exceptions, today)

    if args.dry_run:
        out = os.path.join(HERE, "preview.html")
        with open(out, "w") as f:
            f.write(html)
        print(f"--- dry run --- wrote {out}\n")
        print(text)
        return

    if args.no_email:
        print("--no-email: .ics regenerated; skipping email.")
    elif changed or args.manual:
        key = require_key()
        subject = ("Piedmont Competition Pool schedule — "
                   + blocks[0]["start"].strftime("updated %b %-d"))
        if args.manual and not changed:
            subject += " (manual)"
        eid = send_email(key, subject, html, text, ics=ics)
        verify_delivery(key, eid)
        state["last_sent"] = today.isoformat()
    else:
        print("competition-pool schedule unchanged; .ics refreshed, no email.")

    state["content_hash"] = sig
    state["revised_utc"] = revised.isoformat()
    state["schedule_ranges"] = [
        f"{b['start'].isoformat()}..{b['end'].isoformat()}" for b in blocks]
    state["updated"] = today.isoformat()
    state["attempts"] = 0
    save_state(state)


if __name__ == "__main__":
    main()
