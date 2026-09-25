# odyssey-watch

A small ticket-drop watcher for Cinema City Czech Republic. It polls the public booking API and sends a push notification the moment new showtimes appear.

Built to catch tickets for Christopher Nolan's *The Odyssey* in **70mm IMAX at Cinema City Flora, Prague**, one of the few screens in the world that can project it. Screenings there were selling out within minutes of release.

**It worked.** The alert fired within minutes of the drop and I booked two centre seats at face value.

> **Status:** retired. The schedule is removed and the workflow is disabled. The code still works for any Cinema City film or cinema by changing a few IDs.

## How it works

Cinema City's website renders showtimes client-side from a public JSON API (no key, no login). The watcher uses two endpoints:

| Endpoint | Purpose |
|---|---|
| `…/quickbook/10101/dates/in-cinema/{cinemaId}/until/{date}` | Cheap pre-filter: which dates the cinema has *any* programme on |
| `…/quickbook/10101/film-events/in-cinema/{cinemaId}/at-date/{date}` | The real check: is *this film*, in *this format*, screening on that date |

Two alert tiers:

- **Priority**: the target film appears on one of your target dates. Sent at maximum priority with showtimes, seats remaining and a direct booking link.
- **Info**: new dates were added to the cinema's programme.

State lives in `state.json`, so each alert fires once.

## Running it

Python 3, standard library only.

```bash
export NTFY_TOPIC="your-unguessable-topic"
export TARGET_DATES="2026-08-28,2026-08-29"
export FORMAT_FILTER="70-mm"

python3 watch.py --check              # print API URLs and test connectivity
python3 watch.py --inspect 2026-08-06 # dump every film and screening on a date
python3 watch.py --dry-run            # full run without saving state
python3 watch.py                      # normal run
```

Notification backends (configure any combination; all of them fire): **ntfy**, **Pushover**, **Telegram**, **Discord**, or a generic webhook.

To watch a different film or cinema, change `CC_FILM_ID` and `CC_CINEMA_ID`. Both are visible in the film page URL and in the API responses.

## Where it ran

- **Local (primary):** a macOS `launchd` agent every 3 minutes.
- **GitHub Actions (backup):** a scheduled workflow that committed state back to the repo.

GitHub's scheduler was a weak backup. Scheduled runs on free public repos are best-effort, and in practice this workflow fired roughly hourly instead of every 10 minutes. The local agent did the real work.

## Lessons learned

- **Test the alert path against live data before it matters.** The format filter originally looked for `imax` in `attributeIds`, but the API tags the format as `70-mm` and mentions IMAX only in the auditorium name. Every match was silently dropped, and the logs looked exactly like "nothing on sale yet." The `--inspect` mode caught it.
- **Cinema-wide dates are not film dates.** A date on the booking calendar doesn't mean the film is showing that day. Only the per-date film-events check is authoritative.
- **Release timing was irregular.** Observed additions landed on a Monday evening and a Friday morning. There was no weekday to aim for, so continuous polling was the only reliable strategy.
- **macOS gotchas:** `launchd` agents can't read `~/Downloads` (TCC protection), and a sleeping Mac polls nothing.

## Credits

[TarkDetrius/cinemacity-watchdog](https://github.com/TarkDetrius/cinemacity-watchdog) independently tracked the same screenings and helped confirm the API tenant ID.
