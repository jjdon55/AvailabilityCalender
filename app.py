"""
AvailabilityCalender - Phase 2 with view-rendering cache and access control.

Connects to your real Google Calendar via the secret iCal URL configured
in .streamlit/secrets.toml.

Run from a Command Prompt in this folder:
    python -m streamlit run app.py
"""

from __future__ import annotations

import re
import sys
import time as time_module
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import streamlit as st

try:
    from icalendar import Calendar as ICalendar
except ImportError:
    ICalendar = None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DISPLAY_TZ = ZoneInfo("America/New_York")
DAY_START_HOUR = 8
DAY_END_HOUR = 18
WEEKS_PER_PAGE = 3
MAX_WEEKS_FORWARD = 52
MAX_WEEKS_BACK = 0
PAGE_HORIZONTAL_MARGIN_PX = 80

ICAL_CACHE_SECONDS = 300
ICAL_FETCH_TIMEOUT_SECONDS = 30

# Client tokens used throughout the app
CLIENT_ABC = "abc"
CLIENT_OTD = "otd"
CLIENT_JD = "jd"  # full-access owner view

ABC_DOMAINS = {"abc.com"}
OTD_DOMAINS = {"opentodebate.org"}

PREFIX_VACATION = "vacation"
PREFIX_ABC = "abc"
PREFIX_OTD = "otd"
PREFIX_EXCEPTION = "exception"

# Single regex matches any of: vacation, abc, otd, exception
# at the start of a title, with optional colon, case-insensitive
PREFIX_RE = re.compile(
    r"^\s*(vacation|abc|otd|exception)\s*:?\s+(.*)$",
    re.IGNORECASE,
)

LABEL_NOT_AVAILABLE = "Not available"
LABEL_VACATION = "Vacation"

COLOR_AVAILABLE_BG = "#E8F0E2"
COLOR_AVAILABLE_BORDER = "#C8DDB8"
COLOR_NOT_AVAILABLE_BG = "#F8DDE5"
COLOR_NOT_AVAILABLE_FG = "#7A2A40"
COLOR_NOT_AVAILABLE_BORDER = "#E8B5C4"
COLOR_ABC_BG = "#D6E8C0"
COLOR_ABC_FG = "#2A4810"
COLOR_ABC_BORDER = "#A8C880"
COLOR_OTD_BG = "#DCD4ED"
COLOR_OTD_FG = "#3A2A6A"
COLOR_OTD_BORDER = "#B8A8DC"
COLOR_PERSONAL_BG = "#E5E2D8"
COLOR_PERSONAL_FG = "#3A3A35"
COLOR_PERSONAL_BORDER = "#C4C0B0"
COLOR_VACATION_BG = "#F5DEB3"
COLOR_VACATION_FG = "#5A3A0A"
COLOR_VACATION_BORDER = "#D8B878"
COLOR_VACATION_DAY_BG = "#FAEAC8"      # soft tint for full vacation-day background
COLOR_VACATION_DAY_BORDER = "#E8CBA0"
COLOR_HOUR_LINE = "#D8E5CC"
COLOR_TEXT_PRIMARY = "#2C2C2A"
COLOR_TEXT_SECONDARY = "#5F5E5A"
COLOR_TEXT_TERTIARY = "#888780"


# -----------------------------------------------------------------------------
# Event data model
# -----------------------------------------------------------------------------

@dataclass
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    attendee_domains: frozenset[str] = field(default_factory=frozenset)
    organizer_domain: str = ""
    classification_reason: str = ""


# -----------------------------------------------------------------------------
# Access control - read keys from secrets, validate URL key
# -----------------------------------------------------------------------------

def _read_ical_url() -> str | None:
    try:
        url = st.secrets.get("JOHN_DONVAN_ICAL_URL", "")
    except (FileNotFoundError, KeyError, AttributeError):
        return None
    url = (url or "").strip()
    if not url or url.startswith("PASTE_"):
        return None
    return url


def _read_access_keys() -> dict[str, str]:
    """
    Return a dict of {client_token: key_value} for keys defined in secrets.
    Missing keys are simply absent from the dict.
    Empty or placeholder keys are treated as missing.
    """
    out: dict[str, str] = {}
    for client_token, secret_name in (
        (CLIENT_JD, "JD_KEY"),
        (CLIENT_ABC, "ABC_KEY"),
        (CLIENT_OTD, "OTD_KEY"),
    ):
        try:
            val = st.secrets.get(secret_name, "")
        except (FileNotFoundError, KeyError, AttributeError):
            val = ""
        val = (val or "").strip()
        if val and not val.startswith("PASTE_"):
            out[client_token] = val
    return out


def _constant_time_equals(a: str, b: str) -> bool:
    """Length-safe equality check that doesn't short-circuit on mismatch."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def _resolve_access(url_key: str) -> str | None:
    """
    Match the URL `key` parameter against configured secrets.
    Returns the matching client token (CLIENT_JD/ABC/OTD) or None.
    """
    if not url_key:
        return None
    keys = _read_access_keys()
    for client_token, secret_value in keys.items():
        if _constant_time_equals(url_key, secret_value):
            return client_token
    return None


# -----------------------------------------------------------------------------
# iCal fetching
# -----------------------------------------------------------------------------

def _fetch_ical_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AvailabilityCalender/1.0"},
    )
    with urllib.request.urlopen(req, timeout=ICAL_FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read()


@st.cache_data(ttl=ICAL_CACHE_SECONDS, show_spinner=False)
def _cached_fetch_ical(url: str) -> tuple[bytes, float]:
    raw = _fetch_ical_bytes(url)
    return raw, time_module.time()


@st.cache_data(ttl=ICAL_CACHE_SECONDS, show_spinner=False)
def _cached_parse_events_window(
    raw_ical: bytes,
    window_start_iso: str,
    window_end_iso: str,
) -> list[Event]:
    """
    Parse iCal bytes into Event objects, restricted to a date window.
    Cached on (raw_ical, window_start, window_end).
    """
    window_start = datetime.fromisoformat(window_start_iso)
    window_end = datetime.fromisoformat(window_end_iso)
    return _parse_ical_to_events(raw_ical, window_start, window_end)


def _to_aware_datetime(value, all_day: bool) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(DISPLAY_TZ)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=DISPLAY_TZ)
    raise ValueError(f"Unrecognized date/time value: {value!r}")


def _safe_text(component, key: str, default: str = "") -> str:
    try:
        v = component.get(key)
    except Exception:
        return default
    if v is None:
        return default
    try:
        s = str(v)
    except UnicodeError:
        try:
            s = bytes(v).decode("utf-8", errors="replace")
        except Exception:
            return default
    return s.strip()


def _extract_email_domain(value: str) -> str:
    if not value:
        return ""
    s = str(value).strip()
    s = re.sub(r"(?i)^mailto:", "", s)
    if "@" in s:
        return s.split("@", 1)[1].strip().strip(">").lower()
    return ""


def _attendee_domains(component) -> frozenset[str]:
    raw = component.get("ATTENDEE")
    if raw is None:
        return frozenset()
    items = raw if isinstance(raw, list) else [raw]
    domains: set[str] = set()
    for item in items:
        d = _extract_email_domain(str(item))
        if d:
            domains.add(d)
    return frozenset(domains)


def _organizer_domain(component) -> str:
    raw = component.get("ORGANIZER")
    if raw is None:
        return ""
    return _extract_email_domain(str(raw))


def _expand_recurrences(
    vevent, window_start: datetime, window_end: datetime
) -> list[tuple[datetime, datetime, bool]]:
    try:
        dtstart = vevent.get("DTSTART")
        dtend = vevent.get("DTEND")
        if dtstart is None:
            return []
        start_val = dtstart.dt
        all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)
        if dtend is not None:
            end_val = dtend.dt
        else:
            if all_day:
                end_val = start_val + timedelta(days=1)
            else:
                end_val = start_val + timedelta(hours=1)

        start_dt = _to_aware_datetime(start_val, all_day)
        end_dt = _to_aware_datetime(end_val, all_day)

        rrule = vevent.get("RRULE")
        if rrule is None:
            return [(start_dt, end_dt, all_day)]

        from dateutil.rrule import rrulestr
        rrule_str = "RRULE:" + rrule.to_ical().decode("utf-8")
        try:
            rule = rrulestr(rrule_str, dtstart=start_dt)
        except Exception:
            return [(start_dt, end_dt, all_day)]

        exdates: set[datetime] = set()
        ex = vevent.get("EXDATE")
        if ex is not None:
            ex_list = ex if isinstance(ex, list) else [ex]
            for e in ex_list:
                try:
                    for sub in e.dts:
                        exdates.add(_to_aware_datetime(sub.dt, all_day))
                except Exception:
                    pass

        duration = end_dt - start_dt
        instances: list[tuple[datetime, datetime, bool]] = []
        try:
            occurrences = rule.between(
                window_start - timedelta(days=2),
                window_end + timedelta(days=2),
                inc=True,
            )
        except Exception:
            return [(start_dt, end_dt, all_day)]

        for occ in occurrences:
            if occ in exdates:
                continue
            instances.append((occ, occ + duration, all_day))
        return instances

    except Exception:
        return []


def _parse_ical_to_events(
    raw: bytes, window_start: datetime, window_end: datetime
) -> list[Event]:
    if ICalendar is None:
        raise RuntimeError(
            "The 'icalendar' library is not installed. "
            "Run: python -m pip install icalendar"
        )

    cal = ICalendar.from_ical(raw)
    events: list[Event] = []
    cancelled_uids: set[str] = set()

    for component in cal.walk("VEVENT"):
        status = _safe_text(component, "STATUS").upper()
        if status == "CANCELLED":
            uid = _safe_text(component, "UID")
            if uid:
                cancelled_uids.add(uid)

    for component in cal.walk("VEVENT"):
        try:
            uid = _safe_text(component, "UID") or f"noUID-{id(component)}"
            if uid in cancelled_uids:
                continue
            status = _safe_text(component, "STATUS").upper()
            if status == "CANCELLED":
                continue

            title = _safe_text(component, "SUMMARY") or "(no title)"
            attendees = _attendee_domains(component)
            organizer = _organizer_domain(component)

            for s, e, all_day in _expand_recurrences(
                component, window_start, window_end
            ):
                if e <= window_start or s >= window_end:
                    continue
                events.append(Event(
                    uid=f"{uid}@{s.isoformat()}",
                    title=title,
                    start=s,
                    end=e,
                    all_day=all_day,
                    attendee_domains=attendees,
                    organizer_domain=organizer,
                ))
        except Exception:
            continue

    return events


# -----------------------------------------------------------------------------
# Privacy filtering
# -----------------------------------------------------------------------------

@dataclass
class RenderedEvent:
    title: str
    start: datetime
    end: datetime
    all_day: bool
    bg: str
    fg: str
    border: str
    is_blocked_only: bool
    event_id: str = ""
    full_title: str = ""
    classification: str = ""


def _strip_prefix(title: str) -> tuple[str | None, str]:
    """
    Strip ONE prefix off the title (the leftmost). Returns
    (prefix_lower or None, remainder).
    """
    if not title:
        return None, ""
    m = PREFIX_RE.match(title)
    if not m:
        return None, title.strip()
    return m.group(1).lower(), m.group(2).strip()


def _parse_prefixes(title: str) -> tuple[bool, str | None, str]:
    """
    Parse a title that may have an Exception: prefix followed by another
    prefix (or none). Returns:
      (is_exception, classification_prefix or None, displayable_text)

    Examples:
      "Exception: OTD prep call"  -> (True, "otd", "prep call")
      "Exception: Family thing"   -> (True, None, "Family thing")
      "OTD prep call"             -> (False, "otd", "prep call")
      "Vacation: Greece"          -> (False, "vacation", "Greece")
      "Random meeting"            -> (False, None, "Random meeting")
    """
    if not title:
        return False, None, ""

    is_exception = False
    first_prefix, rest = _strip_prefix(title)
    if first_prefix == PREFIX_EXCEPTION:
        is_exception = True
        # Look for a second prefix on what's left
        second_prefix, deeper_rest = _strip_prefix(rest)
        if second_prefix and second_prefix != PREFIX_EXCEPTION:
            return True, second_prefix, deeper_rest
        # No second prefix; remainder is plain personal text
        return True, None, rest

    return False, first_prefix, rest


def _classify_owner(event: Event) -> tuple[str, str, bool]:
    """
    Returns (owner, reason, is_exception).
    owner is one of "vacation", "abc", "otd", "personal".
    is_exception is True if the title started with "Exception:" - meaning
    the event punches through vacation suppression.
    """
    is_exception, classification_prefix, _ = _parse_prefixes(event.title)

    # Exception cannot itself be a Vacation marker
    if classification_prefix == PREFIX_VACATION:
        return "vacation", "Vacation: prefix", False  # vacation never bears Exception

    if classification_prefix == PREFIX_ABC:
        reason = "ABC: prefix" + (" with Exception" if is_exception else "")
        return "abc", reason, is_exception
    if classification_prefix == PREFIX_OTD:
        reason = "OTD: prefix" + (" with Exception" if is_exception else "")
        return "otd", reason, is_exception

    # No classification prefix - fall back to domain detection
    domains = set(event.attendee_domains)
    if event.organizer_domain:
        domains.add(event.organizer_domain.lower())

    has_abc = bool(domains & ABC_DOMAINS)
    has_otd = bool(domains & OTD_DOMAINS)
    if has_abc and has_otd:
        return "personal", "mixed domains -> personal", is_exception
    if has_abc:
        reason = "abc.com domain" + (" with Exception" if is_exception else "")
        return "abc", reason, is_exception
    if has_otd:
        reason = "opentodebate.org domain" + (" with Exception" if is_exception else "")
        return "otd", reason, is_exception
    reason = "no prefix or known domain" + (" with Exception" if is_exception else "")
    return "personal", reason, is_exception


def _displayed_title(event: Event) -> str:
    """The title with all prefixes stripped, ready to show."""
    _, _, remainder = _parse_prefixes(event.title)
    return remainder if remainder else "Meeting"


def filter_for_client(events: Iterable[Event], client: str) -> list[RenderedEvent]:
    out: list[RenderedEvent] = []

    # Identify vacation date ranges
    vacation_dates: set[date] = set()
    for ev in events:
        owner, _, _ = _classify_owner(ev)
        if owner == "vacation" and ev.all_day:
            d = ev.start.date()
            while d < ev.end.date():
                vacation_dates.add(d)
                d += timedelta(days=1)

    for ev in events:
        owner, reason, is_exception = _classify_owner(ev)
        ev.classification_reason = f"{owner} ({reason})"
        classification_str = f"{owner} - {reason}"

        if client == CLIENT_JD:
            if owner == "vacation":
                bg, fg, br = COLOR_VACATION_BG, COLOR_VACATION_FG, COLOR_VACATION_BORDER
                _, _, rem = _parse_prefixes(ev.title)
                t = f"Vacation: {rem}" if rem else "Vacation"
            elif owner == "abc":
                bg, fg, br = COLOR_ABC_BG, COLOR_ABC_FG, COLOR_ABC_BORDER
                t = _displayed_title(ev)
            elif owner == "otd":
                bg, fg, br = COLOR_OTD_BG, COLOR_OTD_FG, COLOR_OTD_BORDER
                t = _displayed_title(ev)
            else:
                bg, fg, br = COLOR_PERSONAL_BG, COLOR_PERSONAL_FG, COLOR_PERSONAL_BORDER
                t = ev.title or "Event"
            out.append(RenderedEvent(
                title=t, start=ev.start, end=ev.end,
                all_day=ev.all_day, bg=bg, fg=fg, border=br,
                is_blocked_only=False,
                event_id=ev.uid, full_title=ev.title or "Event",
                classification=classification_str,
            ))
            continue

        # Client view
        if owner == "vacation":
            out.append(RenderedEvent(
                title=LABEL_VACATION, start=ev.start, end=ev.end,
                all_day=True, bg=COLOR_VACATION_BG, fg=COLOR_VACATION_FG,
                border=COLOR_VACATION_BORDER, is_blocked_only=True,
                event_id=ev.uid, full_title=LABEL_VACATION,
                classification=classification_str,
            ))
            continue

        # Suppress non-all-day events on vacation days, EXCEPT when the
        # event is explicitly marked as an Exception.
        if (
            ev.start.date() in vacation_dates
            and not ev.all_day
            and not is_exception
        ):
            continue

        if ev.all_day:
            if owner == "abc":
                if client == CLIENT_ABC:
                    out.append(RenderedEvent(
                        title=_displayed_title(ev), start=ev.start, end=ev.end,
                        all_day=True, bg=COLOR_ABC_BG, fg=COLOR_ABC_FG,
                        border=COLOR_ABC_BORDER, is_blocked_only=False,
                        event_id=ev.uid, full_title=_displayed_title(ev),
                        classification=classification_str,
                    ))
                else:
                    out.append(RenderedEvent(
                        title=LABEL_NOT_AVAILABLE, start=ev.start, end=ev.end,
                        all_day=True, bg=COLOR_NOT_AVAILABLE_BG,
                        fg=COLOR_NOT_AVAILABLE_FG,
                        border=COLOR_NOT_AVAILABLE_BORDER, is_blocked_only=True,
                        event_id=ev.uid, full_title=LABEL_NOT_AVAILABLE,
                        classification=classification_str,
                    ))
            elif owner == "otd":
                if client == CLIENT_OTD:
                    out.append(RenderedEvent(
                        title=_displayed_title(ev), start=ev.start, end=ev.end,
                        all_day=True, bg=COLOR_OTD_BG, fg=COLOR_OTD_FG,
                        border=COLOR_OTD_BORDER, is_blocked_only=False,
                        event_id=ev.uid, full_title=_displayed_title(ev),
                        classification=classification_str,
                    ))
                else:
                    out.append(RenderedEvent(
                        title=LABEL_NOT_AVAILABLE, start=ev.start, end=ev.end,
                        all_day=True, bg=COLOR_NOT_AVAILABLE_BG,
                        fg=COLOR_NOT_AVAILABLE_FG,
                        border=COLOR_NOT_AVAILABLE_BORDER, is_blocked_only=True,
                        event_id=ev.uid, full_title=LABEL_NOT_AVAILABLE,
                        classification=classification_str,
                    ))
            continue

        # Hourly events (now potentially including vacation-day exceptions)
        if owner == "abc":
            if client == CLIENT_ABC:
                out.append(RenderedEvent(
                    title=_displayed_title(ev), start=ev.start, end=ev.end,
                    all_day=False, bg=COLOR_ABC_BG, fg=COLOR_ABC_FG,
                    border=COLOR_ABC_BORDER, is_blocked_only=False,
                    event_id=ev.uid, full_title=_displayed_title(ev),
                    classification=classification_str,
                ))
            else:
                out.append(RenderedEvent(
                    title=LABEL_NOT_AVAILABLE, start=ev.start, end=ev.end,
                    all_day=False, bg=COLOR_NOT_AVAILABLE_BG,
                    fg=COLOR_NOT_AVAILABLE_FG, border=COLOR_NOT_AVAILABLE_BORDER,
                    is_blocked_only=True,
                    event_id=ev.uid, full_title=LABEL_NOT_AVAILABLE,
                    classification=classification_str,
                ))
        elif owner == "otd":
            if client == CLIENT_OTD:
                out.append(RenderedEvent(
                    title=_displayed_title(ev), start=ev.start, end=ev.end,
                    all_day=False, bg=COLOR_OTD_BG, fg=COLOR_OTD_FG,
                    border=COLOR_OTD_BORDER, is_blocked_only=False,
                    event_id=ev.uid, full_title=_displayed_title(ev),
                    classification=classification_str,
                ))
            else:
                out.append(RenderedEvent(
                    title=LABEL_NOT_AVAILABLE, start=ev.start, end=ev.end,
                    all_day=False, bg=COLOR_NOT_AVAILABLE_BG,
                    fg=COLOR_NOT_AVAILABLE_FG, border=COLOR_NOT_AVAILABLE_BORDER,
                    is_blocked_only=True,
                    event_id=ev.uid, full_title=LABEL_NOT_AVAILABLE,
                    classification=classification_str,
                ))
        else:
            out.append(RenderedEvent(
                title=LABEL_NOT_AVAILABLE, start=ev.start, end=ev.end,
                all_day=False, bg=COLOR_NOT_AVAILABLE_BG,
                fg=COLOR_NOT_AVAILABLE_FG, border=COLOR_NOT_AVAILABLE_BORDER,
                is_blocked_only=True,
                event_id=ev.uid, full_title=LABEL_NOT_AVAILABLE,
                classification=classification_str,
            ))

    return out


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------

def _format_date_short(d: date) -> str:
    if sys.platform == "win32":
        return d.strftime("%b %#d")
    return d.strftime("%b %-d")


def _clip_to_workday(ev: RenderedEvent, day: date) -> tuple[float, float] | None:
    if ev.all_day:
        return None
    day_start = datetime.combine(day, time(DAY_START_HOUR, 0), tzinfo=DISPLAY_TZ)
    day_end = datetime.combine(day, time(DAY_END_HOUR, 0), tzinfo=DISPLAY_TZ)
    s = max(ev.start, day_start)
    e = min(ev.end, day_end)
    if e <= s:
        return None
    s_h = (s - day_start).total_seconds() / 3600.0 + DAY_START_HOUR
    e_h = (e - day_start).total_seconds() / 3600.0 + DAY_START_HOUR
    return (s_h, e_h)


def _allday_band_for_day(rendered: list[RenderedEvent], day: date) -> RenderedEvent | None:
    for ev in rendered:
        if not ev.all_day:
            continue
        if ev.start.date() <= day < ev.end.date():
            return ev
    return None


def _is_vacation_day(rendered: list[RenderedEvent], day: date) -> bool:
    """Return True if any all-day event covering this day is a Vacation."""
    band = _allday_band_for_day(rendered, day)
    if band is None:
        return False
    return band.title == LABEL_VACATION or band.title.startswith("Vacation")


def _hour_label(h: int) -> str:
    if h == 0:
        return "12 am"
    if h < 12:
        return f"{h} am"
    if h == 12:
        return "12 pm"
    return f"{h - 12} pm"


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def render_pages_html(rendered_by_week: list[tuple[date, list[RenderedEvent]]],
                       client: str,
                       url_key: str = "") -> str:
    hours = list(range(DAY_START_HOUR, DAY_END_HOUR + 1))
    hour_count = DAY_END_HOUR - DAY_START_HOUR
    hour_px = 38
    show_described_border = client in (CLIENT_ABC, CLIENT_OTD)
    key_param = f"key={_html_escape(url_key)}&" if url_key else ""

    css = f"""
    <style>
      .av-root {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: {COLOR_TEXT_PRIMARY};
      }}
      .av-week-header {{
        font-size: 15px; font-weight: 500; margin: 24px 0 10px 4px;
      }}
      .av-week-grid {{
        display: grid;
        grid-template-columns: 56px repeat(5, minmax(0, 170px));
        gap: 8px;
        justify-content: start;
      }}
      .av-day-card {{
        background: {COLOR_AVAILABLE_BG};
        border: 1px solid {COLOR_AVAILABLE_BORDER};
        border-radius: 10px;
        overflow: hidden;
      }}
      .av-day-header {{
        text-align: center; padding: 8px 4px;
        background: rgba(255,255,255,0.4);
        border-bottom: 1px solid {COLOR_AVAILABLE_BORDER};
      }}
      .av-day-name {{ font-size: 13px; font-weight: 500; line-height: 1.1; }}
      .av-day-date {{
        font-size: 11px; color: {COLOR_TEXT_SECONDARY};
        margin-top: 2px;
      }}
      .av-allday-band {{
        font-size: 11px; padding: 5px 8px; font-weight: 500;
        border-bottom: 1px solid;
        text-decoration: none; display: block; cursor: pointer;
      }}
      .av-day-body {{
        position: relative;
        height: {hour_count * hour_px}px;
      }}
      .av-hour-line {{
        position: absolute; left: 0; right: 0; height: 1px;
        background: {COLOR_HOUR_LINE};
      }}
      .av-event {{
        position: absolute; left: 4px; right: 4px;
        border: 1px solid;
        border-radius: 6px;
        padding: 4px 6px; font-size: 11px;
        overflow: hidden; box-sizing: border-box;
        line-height: 1.25;
        text-decoration: none; cursor: pointer;
      }}
      .av-event:hover, .av-allday-band:hover {{
        filter: brightness(0.96);
      }}
      .av-event-detail {{ font-weight: 500; }}
      .av-event-described {{
        border: 1.5px solid {COLOR_TEXT_PRIMARY} !important;
      }}
      .av-allday-described {{
        border: 1.5px solid {COLOR_TEXT_PRIMARY} !important;
      }}
      .av-hours-col {{
        padding-top: {18 + 30}px;
        font-size: 10px;
        color: {COLOR_TEXT_TERTIARY};
        text-align: right;
        padding-right: 6px;
      }}
      .av-hour-label {{
        height: {hour_px}px;
        line-height: 1;
        position: relative;
        top: -4px;
      }}
    </style>
    """

    parts = [css, f'<div class="av-root" style="padding: 0 {PAGE_HORIZONTAL_MARGIN_PX}px;">']

    for week_monday, rendered in rendered_by_week:
        days = [week_monday + timedelta(days=i) for i in range(5)]
        week_label = (
            f"Week of {_format_date_short(days[0])} - {_format_date_short(days[-1])}"
        )
        parts.append(f'<div class="av-week-header">{week_label}</div>')
        parts.append('<div class="av-week-grid">')

        parts.append('<div class="av-hours-col">')
        for h in hours[:-1]:
            parts.append(f'<div class="av-hour-label">{_hour_label(h)}</div>')
        parts.append('</div>')

        for d in days:
            band = _allday_band_for_day(rendered, d)
            is_vac = _is_vacation_day(rendered, d)

            # Day card: vacation days get an orange tint instead of mint
            if is_vac:
                card_style = (
                    f"background: {COLOR_VACATION_DAY_BG}; "
                    f"border-color: {COLOR_VACATION_DAY_BORDER};"
                )
            else:
                card_style = ""

            parts.append(f'<div class="av-day-card" style="{card_style}">')
            parts.append('<div class="av-day-header"' +
                          (f' style="border-bottom-color: {COLOR_VACATION_DAY_BORDER};"' if is_vac else '') +
                          '>')
            parts.append(f'<div class="av-day-name">{d.strftime("%a")}</div>')
            parts.append(f'<div class="av-day-date">{_format_date_short(d)}</div>')
            parts.append('</div>')

            if band is not None:
                described_cls = " av-allday-described" if (
                    show_described_border and not band.is_blocked_only
                ) else ""
                eid = _html_escape(band.event_id)
                parts.append(
                    f'<a href="?{key_param}event={eid}&v={client}" target="_self" '
                    f'class="av-allday-band{described_cls}" '
                    f'style="background: {band.bg}; color: {band.fg}; '
                    f'border-color: {band.border};">'
                    f'{_html_escape(band.title)}</a>'
                )
            else:
                parts.append(
                    '<div class="av-allday-band" style="background: transparent; '
                    'border-color: transparent; color: transparent; cursor: default;">.</div>'
                )

            parts.append('<div class="av-day-body">')
            for i, h in enumerate(hours):
                top = i * hour_px
                parts.append(
                    f'<div class="av-hour-line" style="top: {top}px;"></div>'
                )
            for ev in rendered:
                if ev.all_day:
                    continue
                if ev.start.date() != d and ev.end.date() != d:
                    if not (ev.start.date() <= d <= ev.end.date()):
                        continue
                clip = _clip_to_workday(ev, d)
                if clip is None:
                    continue
                s_h, e_h = clip
                top = (s_h - DAY_START_HOUR) * hour_px
                height = max((e_h - s_h) * hour_px - 2, 18)
                cls = "av-event"
                if not ev.is_blocked_only:
                    cls += " av-event-detail"
                if show_described_border and not ev.is_blocked_only:
                    cls += " av-event-described"
                eid = _html_escape(ev.event_id)
                parts.append(
                    f'<a href="?{key_param}event={eid}&v={client}" target="_self" '
                    f'class="{cls}" '
                    f'style="top: {top}px; height: {height}px; '
                    f'background: {ev.bg}; color: {ev.fg}; '
                    f'border-color: {ev.border};">'
                    f'{_html_escape(ev.title)}'
                    '</a>'
                )
            parts.append('</div>')
            parts.append('</div>')

        parts.append('</div>')

    parts.append('</div>')
    return "".join(parts)


# -----------------------------------------------------------------------------
# Cached rendering of an entire view
# -----------------------------------------------------------------------------

@st.cache_data(ttl=ICAL_CACHE_SECONDS, show_spinner=False)
def _render_view_cached(
    raw_ical: bytes,
    client: str,
    page_offset_weeks: int,
    base_monday_iso: str,
    url_key: str,
) -> str:
    base_monday = date.fromisoformat(base_monday_iso)
    page_first_monday = base_monday + timedelta(weeks=page_offset_weeks)
    window_start = datetime.combine(
        page_first_monday, time(0, 0), tzinfo=DISPLAY_TZ,
    )
    window_end = datetime.combine(
        page_first_monday + timedelta(weeks=WEEKS_PER_PAGE),
        time(0, 0), tzinfo=DISPLAY_TZ,
    )
    events = _cached_parse_events_window(
        raw_ical, window_start.isoformat(), window_end.isoformat()
    )
    rendered_all = filter_for_client(events, client)

    rendered_by_week: list[tuple[date, list[RenderedEvent]]] = []
    for week_idx in range(WEEKS_PER_PAGE):
        wm = page_first_monday + timedelta(weeks=week_idx)
        wk_end_excl = wm + timedelta(days=7)
        wk_events = [
            ev for ev in rendered_all
            if ev.start.date() < wk_end_excl and ev.end.date() >= wm
        ]
        rendered_by_week.append((wm, wk_events))

    return render_pages_html(rendered_by_week, client, url_key)


# -----------------------------------------------------------------------------
# Streamlit page helpers
# -----------------------------------------------------------------------------

def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _set_query_params_preserving_key(url_key: str) -> None:
    """Clear query params except `key`, preserving authentication."""
    try:
        st.query_params.clear()
        if url_key:
            st.query_params["key"] = url_key
    except Exception:
        pass


def _months_for_index(today: date) -> list[date]:
    months: list[date] = []
    y, m = today.year, today.month
    for _ in range(13):
        months.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _page_offset_for_month(month_first: date, base_monday: date) -> int:
    target_monday = _monday_of(month_first)
    delta_days = (target_monday - base_monday).days
    delta_weeks = delta_days // 7
    max_offset = MAX_WEEKS_FORWARD - WEEKS_PER_PAGE
    return max(0, min(delta_weeks, max_offset))


def _format_event_time_range(rev: RenderedEvent) -> str:
    if rev.all_day:
        s_d = rev.start.date()
        e_d = rev.end.date() - timedelta(days=1)
        if s_d == e_d:
            return f"All day, {s_d.strftime('%A, %B ')}{s_d.day}"
        return (
            f"All day, {s_d.strftime('%A, %B ')}{s_d.day} - "
            f"{e_d.strftime('%A, %B ')}{e_d.day}"
        )
    s_label = rev.start.strftime("%A, %B ") + str(rev.start.day)
    s_time = rev.start.strftime("%I:%M %p").lstrip("0")
    e_time = rev.end.strftime("%I:%M %p").lstrip("0")
    duration = rev.end - rev.start
    total_minutes = int(duration.total_seconds() // 60)
    if total_minutes >= 60 and total_minutes % 60 == 0:
        dur = f"{total_minutes // 60}h"
    elif total_minutes >= 60:
        dur = f"{total_minutes // 60}h {total_minutes % 60}m"
    else:
        dur = f"{total_minutes}m"
    return f"{s_label}, {s_time} - {e_time}  ({dur})"


def _build_event_lookup(rendered: list[RenderedEvent]) -> dict[str, RenderedEvent]:
    out: dict[str, RenderedEvent] = {}
    for rev in rendered:
        if rev.event_id and rev.event_id not in out:
            out[rev.event_id] = rev
    return out


def _events_outside_window(
    rendered: list[RenderedEvent], week_monday: date
) -> list[RenderedEvent]:
    """
    Return events that fall in the given week (Mon-Sun) but lie outside
    the visible Mon-Fri 8am-6pm grid. Excludes all-day events (those are
    rendered as bands at the top of the grid).
    """
    week_start = week_monday
    week_end_excl = week_monday + timedelta(days=7)  # full Mon-Sun
    out: list[RenderedEvent] = []
    for ev in rendered:
        if ev.all_day:
            continue
        # Must fall within this week
        if ev.start.date() >= week_end_excl or ev.end.date() < week_start:
            continue
        # Determine if any portion is outside Mon-Fri 8-6
        ev_start_day = ev.start.weekday()  # 0=Mon, 6=Sun
        ev_end_day = (ev.end - timedelta(microseconds=1)).weekday()
        ev_start_hour = ev.start.hour + ev.start.minute / 60.0
        ev_end_hour = ev.end.hour + ev.end.minute / 60.0
        # If event is on weekend at all
        is_weekend = ev_start_day >= 5 or ev_end_day >= 5
        # If event is outside business hours on a weekday
        is_after_hours = (
            ev_start_day < 5 and ev_end_day < 5 and (
                ev_end_hour <= DAY_START_HOUR or ev_start_hour >= DAY_END_HOUR
            )
        )
        if is_weekend or is_after_hours:
            out.append(ev)
    out.sort(key=lambda e: e.start)
    return out


@st.dialog("Outside business hours")
def _show_outside_hours_modal(
    events_outside: list[RenderedEvent],
    week_monday: date,
    url_key: str = "",
) -> None:
    """Modal listing events that fall outside Mon-Fri 8am-6pm for one week."""
    week_label = (
        f"Week of {_format_date_short(week_monday)} - "
        f"{_format_date_short(week_monday + timedelta(days=4))}"
    )
    st.caption(week_label)

    if not events_outside:
        st.markdown("_No out-of-hours events this week._")
    else:
        for rev in events_outside:
            day_label = rev.start.strftime("%A, %b ") + str(rev.start.day)
            time_label = (
                rev.start.strftime("%I:%M %p").lstrip("0") + " - " +
                rev.end.strftime("%I:%M %p").lstrip("0")
            )
            # Color swatch + label
            st.markdown(
                f"<div style='display: flex; align-items: center; gap: 10px; "
                f"padding: 8px 0; border-bottom: 1px solid #EEE;'>"
                f"<div style='width: 12px; height: 12px; border-radius: 3px; "
                f"background: {rev.bg}; border: 1px solid {rev.border};'></div>"
                f"<div style='flex: 1;'>"
                f"<div style='font-weight: 500;'>{_html_escape(day_label)}</div>"
                f"<div style='font-size: 12px; color: {COLOR_TEXT_SECONDARY};'>"
                f"{_html_escape(time_label)} &middot; {_html_escape(rev.title)}"
                f"</div></div></div>",
                unsafe_allow_html=True,
            )

    if st.button("Close"):
        _set_query_params_preserving_key(url_key)
        st.rerun()


@st.dialog("Event details")
def _show_event_modal(rev: RenderedEvent, view: str, url_key: str = "") -> None:
    """Modal shown when an event block is clicked. Re-applies privacy."""
    if view == CLIENT_JD:
        st.markdown(f"### {rev.title}")
        st.caption(_format_event_time_range(rev))
        if rev.full_title and rev.full_title != rev.title:
            st.markdown(f"**Full title:** {rev.full_title}")
        if rev.classification:
            st.markdown(f"**Classification:** {rev.classification}")
    else:
        if rev.is_blocked_only:
            st.markdown(f"### {LABEL_NOT_AVAILABLE}")
            st.caption(_format_event_time_range(rev))
            st.markdown("This time is blocked.")
        else:
            st.markdown(f"### {rev.title}")
            st.caption(_format_event_time_range(rev))
            st.markdown("This is a confirmed engagement on the calendar.")

    if st.button("Close"):
        _set_query_params_preserving_key(url_key)
        st.rerun()


# -----------------------------------------------------------------------------
# Streamlit page main
# -----------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="AvailabilityCalender",
        layout="wide",
    )

    # ---- Access control ----
    try:
        url_key = st.query_params.get("key", "")
    except Exception:
        url_key = ""

    permitted_view = _resolve_access(url_key)

    if permitted_view is None:
        st.markdown(
            "<div style='max-width: 520px; margin: 80px auto; "
            "text-align: center; font-family: -apple-system, BlinkMacSystemFont, "
            "Segoe UI, sans-serif;'>"
            "<h2 style='font-weight: 500; margin-bottom: 12px;'>Calendar private</h2>"
            "<p style='color: #5F5E5A; line-height: 1.5;'>"
            "This calendar is private. If you believe you should have access, "
            "please contact John Donvan."
            "</p>"
            "</div>",
            unsafe_allow_html=True,
        )
        return

    # User is authenticated. Initialize state.
    if "page_offset_weeks" not in st.session_state:
        st.session_state.page_offset_weeks = 0

    if permitted_view == CLIENT_JD:
        try:
            url_v = st.query_params.get("v", "")
        except Exception:
            url_v = ""
        if "view" not in st.session_state:
            st.session_state.view = (
                url_v if url_v in (CLIENT_JD, CLIENT_ABC, CLIENT_OTD)
                else CLIENT_JD
            )
        elif url_v in (CLIENT_JD, CLIENT_ABC, CLIENT_OTD):
            st.session_state.view = url_v
    else:
        st.session_state.view = permitted_view

    st.markdown(
        "<h2 style='margin-bottom: 0.25rem;'>AvailabilityCalender</h2>"
        f"<p style='color: {COLOR_TEXT_SECONDARY}; margin-top: 0;'>"
        "Live data from John Donvan's calendar."
        "</p>",
        unsafe_allow_html=True,
    )

    today = datetime.now(DISPLAY_TZ).date()
    base_monday = _monday_of(today)

    # ---- View buttons (only for JD) ----
    if permitted_view == CLIENT_JD:
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Your view (full detail)", use_container_width=True):
                st.session_state.view = CLIENT_JD
                _set_query_params_preserving_key(url_key)
        with c2:
            if st.button("ABC view", use_container_width=True):
                st.session_state.view = CLIENT_ABC
                _set_query_params_preserving_key(url_key)
        with c3:
            if st.button("Open to Debate view", use_container_width=True):
                st.session_state.view = CLIENT_OTD
                _set_query_params_preserving_key(url_key)

        view_label = {
            CLIENT_JD: "YOUR VIEW",
            CLIENT_ABC: "ABC",
            CLIENT_OTD: "OPEN TO DEBATE",
        }[st.session_state.view]
        st.caption(f"Currently viewing: {view_label}")

    # Navigation
    nav1, nav2, nav3 = st.columns([1, 2, 1])
    with nav1:
        prev_disabled = st.session_state.page_offset_weeks <= 0
        if st.button("Previous 3 weeks", use_container_width=True,
                     disabled=prev_disabled):
            st.session_state.page_offset_weeks = max(
                0,
                st.session_state.page_offset_weeks - WEEKS_PER_PAGE,
            )
            _set_query_params_preserving_key(url_key)
    with nav3:
        next_disabled = (
            st.session_state.page_offset_weeks + WEEKS_PER_PAGE
            >= MAX_WEEKS_FORWARD
        )
        if st.button("Next 3 weeks", use_container_width=True,
                     disabled=next_disabled):
            st.session_state.page_offset_weeks = min(
                MAX_WEEKS_FORWARD - WEEKS_PER_PAGE,
                st.session_state.page_offset_weeks + WEEKS_PER_PAGE,
            )
            _set_query_params_preserving_key(url_key)

    url = _read_ical_url()
    if not url:
        st.error(
            "No iCal URL configured. Edit `.streamlit/secrets.toml` and add "
            "your Google Calendar secret iCal URL as `JOHN_DONVAN_ICAL_URL`."
        )
        return

    page_first_monday = base_monday + timedelta(
        weeks=st.session_state.page_offset_weeks
    )
    window_start = datetime.combine(
        page_first_monday, time(0, 0), tzinfo=DISPLAY_TZ
    )
    window_end = datetime.combine(
        page_first_monday + timedelta(weeks=WEEKS_PER_PAGE),
        time(0, 0), tzinfo=DISPLAY_TZ,
    )

    fetch_error: str | None = None
    fetched_at_unix: float | None = None
    raw: bytes | None = None
    events: list[Event] = []

    try:
        raw, fetched_at_unix = _cached_fetch_ical(url)
        events = _cached_parse_events_window(
            raw, window_start.isoformat(), window_end.isoformat()
        )
    except urllib.error.HTTPError as e:
        fetch_error = f"Google returned HTTP {e.code}. Check your iCal URL."
    except urllib.error.URLError as e:
        fetch_error = f"Network problem fetching calendar: {e.reason}"
    except Exception as e:
        fetch_error = f"Error reading calendar: {type(e).__name__}: {e}"

    refresh_col, status_col = st.columns([1, 4])
    with refresh_col:
        if st.button("Refresh now", use_container_width=True):
            _cached_fetch_ical.clear()
            _cached_parse_events_window.clear()
            _render_view_cached.clear()
            _set_query_params_preserving_key(url_key)
            st.rerun()
    with status_col:
        if fetched_at_unix is not None:
            age_secs = int(time_module.time() - fetched_at_unix)
            if age_secs < 60:
                age = f"{age_secs}s ago"
            elif age_secs < 3600:
                age = f"{age_secs // 60}m ago"
            else:
                age = f"{age_secs // 3600}h ago"
            st.caption(
                f"Fetched {age}. Auto-refresh every {ICAL_CACHE_SECONDS // 60} min."
            )

    if fetch_error:
        st.error(fetch_error)
        return

    main_col, side_col = st.columns([6, 1])

    with main_col:
        if raw is not None:
            html = _render_view_cached(
                raw,
                st.session_state.view,
                st.session_state.page_offset_weeks,
                base_monday.isoformat(),
                url_key,
            )
            st.markdown(html, unsafe_allow_html=True)

            # ---- Out-of-hours summary buttons (one per visible week) ----
            rendered_all = filter_for_client(events, st.session_state.view)
            for week_idx in range(WEEKS_PER_PAGE):
                wm = page_first_monday + timedelta(weeks=week_idx)
                outside = _events_outside_window(rendered_all, wm)
                if not outside:
                    continue
                count = len(outside)
                noun = "event" if count == 1 else "events"
                btn_label = (
                    f"Week of {_format_date_short(wm)}: "
                    f"+ {count} weekend or evening {noun}"
                )
                btn_key = f"outside_{wm.isoformat()}"
                if st.button(btn_label, key=btn_key):
                    st.session_state["_show_outside_for"] = wm.isoformat()
                    st.session_state["_outside_events"] = outside
                    st.rerun()

            # If a per-week click was triggered, open the modal
            shown_for = st.session_state.get("_show_outside_for")
            if shown_for:
                outside_events = st.session_state.get("_outside_events", [])
                wm_obj = date.fromisoformat(shown_for)
                # Clear before opening so it doesn't reopen on next rerun
                del st.session_state["_show_outside_for"]
                st.session_state.pop("_outside_events", None)
                _show_outside_hours_modal(outside_events, wm_obj, url_key)

            # ---- Click-to-modal: check ?event=<id> in URL ----
            try:
                qp = st.query_params
                event_id = qp.get("event", "")
            except Exception:
                event_id = ""

            if event_id:
                lookup = _build_event_lookup(rendered_all)
                rev = lookup.get(event_id)
                if rev is not None:
                    _show_event_modal(rev, st.session_state.view, url_key)
                else:
                    _set_query_params_preserving_key(url_key)

    with side_col:
        st.markdown(
            "<div style='font-size: 12px; color: " + COLOR_TEXT_SECONDARY +
            "; margin-top: 8px; margin-bottom: 6px; font-weight: 500;'>"
            "Jump to month</div>",
            unsafe_allow_html=True,
        )
        current_first_monday = base_monday + timedelta(
            weeks=st.session_state.page_offset_weeks
        )
        current_month_key = (current_first_monday.year, current_first_monday.month)

        for m in _months_for_index(today):
            label = m.strftime("%b")
            is_current = (m.year, m.month) == current_month_key
            btn_key = f"month_{m.year}_{m.month}"
            if is_current:
                st.markdown(
                    f"<div style='padding: 6px 10px; margin: 2px 0; "
                    f"background: {COLOR_AVAILABLE_BG}; "
                    f"border: 1px solid {COLOR_AVAILABLE_BORDER}; "
                    f"border-radius: 6px; font-size: 13px; "
                    f"font-weight: 500; text-align: center;'>"
                    f"{label}</div>",
                    unsafe_allow_html=True,
                )
            else:
                if st.button(label, key=btn_key, use_container_width=True):
                    st.session_state.page_offset_weeks = _page_offset_for_month(
                        m, base_monday
                    )
                    _set_query_params_preserving_key(url_key)
                    st.rerun()

    if st.session_state.view == CLIENT_JD:
        with st.expander("How each event was classified (your view only)"):
            classified: list[tuple[Event, str]] = []
            for ev in events:
                owner, reason, _ = _classify_owner(ev)
                classified.append((ev, f"{owner} - {reason}"))
            classified.sort(key=lambda pair: pair[0].start)

            counts = {"abc": 0, "otd": 0, "vacation": 0, "personal": 0}
            for ev, label in classified:
                key = label.split(" ")[0]
                counts[key] = counts.get(key, 0) + 1
            st.markdown(
                f"**Counts:** ABC = {counts.get('abc', 0)}, "
                f"OTD = {counts.get('otd', 0)}, "
                f"Vacation = {counts.get('vacation', 0)}, "
                f"Personal = {counts.get('personal', 0)}"
            )
            for ev, label in classified[:60]:
                when = ev.start.strftime("%a %b %d %I:%M %p")
                st.markdown(f"- **{when}** - {ev.title} -> _{label}_")
            if len(classified) > 60:
                st.caption(f"... and {len(classified) - 60} more not shown")


if __name__ == "__main__":
    main()
