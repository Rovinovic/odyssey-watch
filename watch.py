#!/usr/bin/env python3
"""
Odyssey @ IMAX Praha (Cinema City Flora) ticket watcher.

Polls the Cinema City "quickbook" JSON API and alerts via Telegram when:
  - PRIORITY: showtimes appear on any of your TARGET_DATES
  - INFO:     the booking window extends to a new furthest date

Stdlib only. No pip install needed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

# Dates you actually care about (YYYY-MM-DD). Priority alerts fire for these.
TARGET_DATES = os.environ.get("TARGET_DATES", "2026-08-28,2026-08-29").split(",")
TARGET_DATES = [d.strip() for d in TARGET_DATES if d.strip()]

# Cinema City Czech Republic. These were read off the live site.
BASE = "https://www.cinemacity.cz"
SITE_ID = os.environ.get("CC_SITE_ID", "10101")   # tenant id (confirmed from site assets)
CINEMA_ID = os.environ.get("CC_CINEMA_ID", "1052")  # Flora
FILM_ID = os.environ.get("CC_FILM_ID", "7268S2R")   # Odyssea
# URL path prefix is the COUNTRY code (cz), not the language code (cs).
# LANG is cs_CZ but the path is /cz/... — these differ for Czech.
PATH_PREFIX = os.environ.get("CC_PATH_PREFIX", "cz")
LANG = os.environ.get("CC_LANG", "cs_CZ")

# How far ahead to ask the API about.
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "120"))

# Only count IMAX 70mm screenings? Set to "" to accept any screening of the film.
FORMAT_FILTER = os.environ.get("FORMAT_FILTER", "imax").lower()

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# Priority alerts are sent this many times so your phone buzzes repeatedly.
PRIORITY_REPEATS = int(os.environ.get("PRIORITY_REPEATS", "3"))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

BOOKING_PAGE = f"{BASE}/films/odyssea/{FILM_ID.lower()}"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def get_json(url, retries=3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
            "Referer": BOOKING_PAGE,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} :: {last}")


# ----------------------------------------------------------------------------
# Cinema City API
# ----------------------------------------------------------------------------

def api(path):
    return (f"{BASE}/{PATH_PREFIX}/data-api-service/v1/quickbook/"
            f"{SITE_ID}{path}")


def fetch_open_dates():
    """All dates the cinema currently has any programme for."""
    until = (date.today() + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    url = api(f"/dates/in-cinema/{CINEMA_ID}/until/{until}") + \
        "?" + urllib.parse.urlencode({"attr": "", "lang": LANG})
    data = get_json(url)
    return sorted(data.get("body", {}).get("dates", []))


def day_has_any_programme(day):
    """True if the cinema has ANY screening listed on this date."""
    url = api(f"/film-events/in-cinema/{CINEMA_ID}/at-date/{day}") + \
        "?" + urllib.parse.urlencode({"attr": "", "lang": LANG})
    try:
        data = get_json(url, retries=2)
    except Exception:  # noqa: BLE001
        return False
    return bool(data.get("body", {}).get("events", []))


def find_max_date_by_probe():
    """Fallback if /dates/ is unavailable.

    NOTE: do not binary-search here. The live data shows the open dates are
    NOT contiguous — e.g. 2026-08-24 then a gap, then 27/28/29/30, then 09-02.
    Special screenings get released as isolated days. A binary search would
    land in a gap and wrongly report the window as ending early.

    So: scan forward, and keep going past gaps rather than stopping at the
    first miss. Coarse step first to stay cheap, then confirm.
    """
    today = date.today()
    found = []
    for offset in range(0, LOOKAHEAD_DAYS + 1):
        day = (today + timedelta(days=offset)).isoformat()
        if day_has_any_programme(day):
            found.append(day)
    return found[-1] if found else None


def fetch_film_events(day):
    """Screenings of our film on a given date. Returns list of dicts."""
    url = api(f"/film-events/in-cinema/{CINEMA_ID}/at-date/{day}") + \
        "?" + urllib.parse.urlencode({"attr": "", "lang": LANG})
    data = get_json(url)
    body = data.get("body", {})
    events = []
    for ev in body.get("events", []):
        if ev.get("filmId", "").upper() != FILM_ID.upper():
            continue
        # IMAX is NOT in attributeIds (those carry "70-mm"). It only appears
        # in the auditorium fields, e.g. "IMAX VOLVO" / "IMAX". Search all
        # three so either "imax" or "70-mm" works as a filter.
        haystack = " ".join(ev.get("attributeIds", []) + [
            ev.get("auditorium", "") or "",
            ev.get("auditoriumTinyName", "") or "",
        ]).lower()
        if FORMAT_FILTER and FORMAT_FILTER not in haystack:
            continue
        events.append({
            "time": ev.get("eventDateTime", ""),
            "attrs": ev.get("attributeIds", []),
            "auditorium": ev.get("auditorium", ""),
            "link": ev.get("bookingLink", ""),
            "sold_out": ev.get("soldOut", False),
            "avail": ev.get("availabilityRatio"),
        })
    events.sort(key=lambda e: e["time"])
    return events


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"max_date": None, "notified_targets": [], "last_ok": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def _post(url, data, headers=None, form=False):
    if form:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
    elif isinstance(data, (dict, list)):
        body = json.dumps(data).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
    else:
        body = data.encode("utf-8") if isinstance(data, str) else data
        hdrs = {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# --- backends: each takes (title, body, link, priority) ---

def send_ntfy(title, body, link, priority):
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": title.encode("ascii", "ignore").decode() or "Odyssey watch",
        "Priority": "5" if priority else "3",
        "Tags": "rotating_light" if priority else "calendar",
    }
    if link:
        headers["Click"] = link
    tok = os.environ.get("NTFY_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    _post(f"{server}/{topic}", body, headers)
    return True


def send_pushover(title, body, link, priority):
    tok = os.environ.get("PUSHOVER_TOKEN", "")
    usr = os.environ.get("PUSHOVER_USER", "")
    if not (tok and usr):
        return False
    data = {
        "token": tok, "user": usr, "title": title, "message": body,
        "priority": 2 if priority else 0,
    }
    if priority:
        # Emergency: re-alert every 60s for 15 min until acknowledged.
        data["retry"] = 60
        data["expire"] = 900
    if link:
        data["url"] = link
        data["url_title"] = "Open booking page"
    _post("https://api.pushover.net/1/messages.json", data, form=True)
    return True


def send_discord(title, body, link, priority):
    hook = os.environ.get("DISCORD_WEBHOOK", "")
    if not hook:
        return False
    content = f"{'@everyone ' if priority else ''}**{title}**\n{body}"
    if link:
        content += f"\n{link}"
    _post(hook, {"content": content[:1900]})
    return True


def send_telegram(title, body, link, priority):
    if not (TG_TOKEN and TG_CHAT):
        return False
    text = f"<b>{title}</b>\n\n{body}"
    if link:
        text += f'\n\n<a href="{link}">Open booking page</a>'
    payload = {
        "chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": False,
    }
    # Telegram has no priority concept, so repeat to keep the phone buzzing.
    for i in range(PRIORITY_REPEATS if priority else 1):
        _post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", payload)
        if priority and i < PRIORITY_REPEATS - 1:
            time.sleep(3)
    return True


def send_webhook(title, body, link, priority):
    """Generic JSON POST — Slack, Google Chat, n8n, whatever."""
    hook = os.environ.get("GENERIC_WEBHOOK", "")
    if not hook:
        return False
    _post(hook, {"text": f"{title}\n{body}\n{link or ''}".strip(),
                 "title": title, "body": body,
                 "link": link, "priority": priority})
    return True


BACKENDS = [send_ntfy, send_pushover, send_telegram, send_discord, send_webhook]


def notify(title, body, link=None, priority=False):
    print(f"[notify priority={priority}] {title}\n{body}\n", flush=True)
    sent = 0
    for backend in BACKENDS:
        try:
            if backend(title, body, link, priority):
                sent += 1
                print(f"  -> sent via {backend.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"!! {backend.__name__} failed: {e}", file=sys.stderr)
    if not sent:
        print("!! no notifier configured, printed only", file=sys.stderr)


def fmt_events(events):
    if not events:
        return "  (no screening rows returned yet)"
    lines = []
    for e in events:
        t = e["time"].replace("T", " ")[:16]
        aud = e.get("auditorium") or ""
        if e["sold_out"]:
            state = "SOLD OUT"
        elif e.get("avail") is not None:
            pct = e["avail"] * 100
            state = f"{pct:.1f}% seats left"
        else:
            state = ""
        lines.append(f"* {t}  {aud}  {state}".rstrip())
        if e["link"]:
            lines.append(f"    {e['link']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    state = load_state()
    dry = "--dry-run" in sys.argv

    if "--inspect" in sys.argv:
        # Dump everything the API returns for a date, unfiltered.
        i = sys.argv.index("--inspect")
        day = sys.argv[i + 1] if len(sys.argv) > i + 1 else date.today().isoformat()
        url = api(f"/film-events/in-cinema/{CINEMA_ID}/at-date/{day}") + \
            "?" + urllib.parse.urlencode({"attr": "", "lang": LANG})
        print(f"GET {url}\n")
        data = get_json(url)
        body = data.get("body", {})
        films = body.get("films", [])
        events = body.get("events", [])
        print(f"{len(films)} films, {len(events)} events on {day}\n")
        print("--- FILMS ---")
        for f in films:
            star = "  <<< MATCHES CC_FILM_ID" if \
                f.get("id", "").upper() == FILM_ID.upper() else ""
            print(f"  {f.get('id'):<12} {f.get('name')}{star}")
        print("\n--- EVENTS (attributeIds matter for FORMAT_FILTER) ---")
        for e in events[:60]:
            print(f"  {e.get('eventDateTime','')[:16]}  film={e.get('filmId')}  "
                  f"attrs={e.get('attributeIds')}")
        print(f"\nLooking for FILM_ID={FILM_ID}, FORMAT_FILTER='{FORMAT_FILTER}'")
        return


        # Print the URLs and exit, so you can paste them into a browser.
        until = (date.today() + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
        print("dates      :", api(f"/dates/in-cinema/{CINEMA_ID}/until/{until}")
              + "?attr=&lang=" + LANG)
        print("film-events:", api(
            f"/film-events/in-cinema/{CINEMA_ID}/at-date/{TARGET_DATES[0]}")
            + "?attr=&lang=" + LANG)
        print("film id    :", FILM_ID)
        try:
            d = fetch_open_dates()
            print(f"OK: {len(d)} open dates, last = {d[-1]}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: {e}")
        return

    open_dates = None
    try:
        open_dates = fetch_open_dates()
    except Exception as e:  # noqa: BLE001
        print(f"/dates/ endpoint unavailable ({e}); falling back to probing")

    if open_dates:
        max_date = open_dates[-1]
        target_open = [d for d in TARGET_DATES if d in open_dates]
    else:
        # Fallback path: binary-search the edge, then test targets directly.
        max_date = find_max_date_by_probe()
        if max_date is None:
            today = date.today().isoformat()
            if state.get("last_error_day") != today:
                state["last_error_day"] = today
                save_state(state)
                notify("Odyssey watcher broke",
                       "Both the dates endpoint and date probing failed. "
                       "The Cinema City API has probably changed.")
            sys.exit(1)
        target_open = [d for d in TARGET_DATES if day_has_any_programme(d)]

    print(f"Booking window currently open through: {max_date}")
    if open_dates:
        print(f"Total open dates: {len(open_dates)}")

    # --- PRIORITY: our target dates ---
    hits = target_open
    for d in hits:
        if d in state["notified_targets"] and not dry:
            print(f"{d} already notified, skipping")
            continue
        try:
            events = fetch_film_events(d)
        except Exception as e:  # noqa: BLE001
            print(f"could not fetch events for {d}: {e}")
            events = []

        if not events:
            # Cinema is open that day but our film isn't listed (yet).
            print(f"{d}: date open but no matching Odyssea screenings")
            continue

        pretty = datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b %Y")
        notify(
            f"ODYSSEY TICKETS LIVE - {pretty}",
            f"IMAX 70mm, Cinema City Flora, Praha\n\n"
            f"{fmt_events(events)}\n\n"
            f"GO NOW. These sell out in minutes.",
            link=BOOKING_PAGE,
            priority=True,
        )
        if not dry:
            state["notified_targets"].append(d)

    # --- INFO: new dates appeared ---
    # Dates arrive in gaps (isolated special screenings), so "max moved" is a
    # weak signal — a far-future date can already be listed. Track the set.
    prev_known = set(state.get("known_dates") or [])
    now_known = set(open_dates) if open_dates else set()
    new_dates = sorted(now_known - prev_known) if prev_known else []

    if not prev_known:
        print(f"First run, baseline: {len(now_known)} dates through {max_date}")
    elif new_dates:
        pretty = ", ".join(
            datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
            for d in new_dates[:8]
        )
        remaining = [d for d in TARGET_DATES if d not in target_open]
        status = (f"Still waiting on: {', '.join(remaining)}"
                  if remaining else "All your target dates are open ✅")
        notify(
            "New dates on sale at Flora",
            f"{pretty}\n\n{status}",
            link=BOOKING_PAGE,
        )

    if not dry and now_known:
        state["known_dates"] = sorted(now_known)
        state["max_date"] = max_date

    state["last_ok"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.pop("last_error_day", None)
    if not dry:
        save_state(state)
    print("done")


if __name__ == "__main__":
    main()
