"""Google Calendar & Tasks plugin for FiestaBoard.

Shows today's agenda from one or more Google calendars, what is happening
right now, the next event to start (looking up to a week ahead), and the
Google Tasks due today.

Sign-in uses FiestaBoard's platform OAuth (the manifest's ``oauth`` block):
each user creates their own Google Cloud OAuth client of type "Web
application", pastes its Client ID and Client Secret into the plugin's
Account connection section and presses Sign in. The platform runs the flow
(``access_type=offline`` and ``prompt=consent`` so Google issues a refresh
token), stores the tokens and refreshes them; this plugin only ever calls
``self.get_oauth_token()`` and sends the result as a bearer token.

Endpoints:

* ``GET calendar/v3/calendars/{calendarId}/events``   events for 8 days from local midnight
* ``GET tasks/v1/users/@me/lists``                    task lists (remembered for an hour)
* ``GET tasks/v1/lists/{tasklist}/tasks``             open tasks due up to today
"""

import logging
import math
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from src.board_chars import BoardChars
from src.plugins.base import PluginBase, PluginResult
from src.text_to_board import take_tiles

logger = logging.getLogger(__name__)

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"
USER_AGENT = "FiestaBoard Google Calendar Plugin (https://github.com/Fiestaboard/fiestaboard-plugin--google-calendar)"
REQUEST_TIMEOUT_SECONDS = 10

# A Flagship and a Note each ask for data; one snapshot serves both.
SNAPSHOT_TTL_SECONDS = 30
TASK_LISTS_TTL_SECONDS = 3600
LOOKAHEAD_DAYS = 8  # today plus a week, for "next event"
MAX_EVENTS_PER_CALENDAR = 250
MAX_TASKS_PER_LIST = 100
MAX_LISTED = 10  # entries in the events and tasks arrays

TRANSIENT_COOLDOWN_SECONDS = 30
AUTH_COOLDOWN_SECONDS = 60
DEFAULT_RETRY_AFTER_SECONDS = 60
MAX_RETRY_AFTER_SECONDS = 3600
# During an outage, keep showing the last good data (same day) this long.
STALE_DATA_LIMIT_SECONDS = 30 * 60

DEFAULT_COLS = 22
DEFAULT_ROWS = 6
FIELD_WIDTH = 22

TILE_BUSY = "{63}"  # red
TILE_FREE = "{66}"  # green

NOT_CONNECTED_ERROR = "Not signed in to Google. Open this plugin's settings and sign in."
PLATFORM_TOO_OLD_ERROR = "This FiestaBoard version cannot sign in to Google. Update FiestaBoard to use this plugin."
UNAUTHORIZED_ERROR = "Google rejected the sign-in (401). Open this plugin's settings and press Reconnect."

RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RATE_LIMIT_EXCEEDED"}
DISABLED_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}
SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}

CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"
TIME_FORMATS = ("12h", "24h")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class GoogleError(Exception):
    """A Google request failed; the message is shown to the user."""

    def __init__(self, message: str, *, transient: bool, cooldown: float, status: int = 0):
        super().__init__(message)
        self.transient = transient
        self.cooldown = cooldown
        self.status = status


class _Api:
    """What to call each API in messages."""

    def __init__(self, name: str, what: str, hint: str = ""):
        self.name = name
        self.what = what
        self.hint = hint


CALENDAR = _Api("Google Calendar", "calendars")
TASKS = _Api("Google Tasks", "tasks", " Or turn off Show Tasks.")


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def _monotonic() -> float:
    return time.monotonic()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def board_safe(text: Any) -> str:
    """Uppercase *text* and keep only characters the board can show."""
    folded = unicodedata.normalize("NFKD", str(text or ""))
    kept = []
    for ch in folded.upper():
        if ch.isspace():
            kept.append(" ")
        elif BoardChars.get_char_code(ch) is not None and ch not in "{}":
            kept.append(ch)
    return re.sub(r" {2,}", " ", "".join(kept)).strip()


def fit(text: str, width: int = FIELD_WIDTH) -> str:
    """Truncate *text* to *width* tiles (colour markers count as one)."""
    return take_tiles(text, width)[0].rstrip()


def _general_timezone() -> str:  # pragma: no cover - reads the board's configuration
    """FiestaBoard's own timezone setting (Settings > General)."""
    try:
        from src.config import Config

        return str(Config.GENERAL_TIMEZONE or "")
    except Exception:
        return ""


def resolve_timezone(name: str) -> Optional[ZoneInfo]:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def format_time(moment: datetime, fmt: str) -> str:
    """``9AM``, ``9:30AM`` or ``09:30``."""
    if fmt == "24h":
        return f"{moment.hour:02d}:{moment.minute:02d}"
    hour = moment.hour % 12 or 12
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{hour}{minutes}{'AM' if moment.hour < 12 else 'PM'}"


def format_in(delta: timedelta) -> str:
    """``IN 25M``, ``IN 1H 30M``, ``IN 14H`` or ``IN 2D``."""
    minutes = max(math.ceil(delta.total_seconds() / 60), 1)
    if minutes < 60:
        return f"IN {minutes}M"
    hours, rest = divmod(minutes, 60)
    if hours < 10:
        return f"IN {hours}H {rest}M" if rest else f"IN {hours}H"
    if hours < 48:
        return f"IN {hours}H"
    return f"IN {hours // 24}D"


def date_label(day: date) -> str:
    """``FRI OCT 2``."""
    return f"{WEEKDAYS[day.weekday()]} {MONTHS[day.month - 1]} {day.day}"


def _rfc3339_utc_midnight(day: date) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 'S'}"


# ----------------------------------------------------------------------
# Plugin
# ----------------------------------------------------------------------


class GoogleCalendarPlugin(PluginBase):
    """Today's Google Calendar agenda and Google Tasks due today."""

    def __init__(self, manifest: Dict[str, Any]):
        super().__init__(manifest)
        self._lock = threading.RLock()
        self._reset_state()

    def _reset_state(self) -> None:
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_at = 0.0
        self._last_good: Optional[Dict[str, Any]] = None
        self._last_good_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_error: Optional[GoogleError] = None
        self._cooldown_token: Optional[str] = None
        self._task_lists: Optional[List[Dict[str, Any]]] = None
        self._task_lists_at = 0.0

    @property
    def plugin_id(self) -> str:
        return "google_calendar"

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        # Empty client fields are fine: the sign-in button stays disabled
        # until they are filled in.
        client_id = str(config.get("client_id") or "").strip()
        if client_id and not client_id.endswith(CLIENT_ID_SUFFIX):
            errors.append(f"Client ID should be the OAuth client ID ending in {CLIENT_ID_SUFFIX}")
        tz_name = str(config.get("timezone") or "").strip()
        if tz_name and resolve_timezone(tz_name) is None:
            errors.append(f"Unknown timezone: {tz_name}")
        fmt = config.get("time_format")
        if fmt is not None and fmt not in TIME_FORMATS:
            errors.append("Time format must be 12h or 24h")
        errors.extend(self._validate_refresh_seconds(config))
        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        super().on_config_change(old_config, new_config)
        with self._lock:
            self._reset_state()

    def _setting(self, key: str, default: bool) -> bool:
        value = self.config.get(key)
        return default if value is None else bool(value)

    def _timezone(self) -> ZoneInfo:
        tz = resolve_timezone(str(self.config.get("timezone") or "").strip())
        if tz is not None:
            return tz
        return resolve_timezone(_general_timezone()) or ZoneInfo("UTC")

    def _calendar_ids(self) -> List[str]:
        raw = str(self.config.get("calendar_ids") or "primary")
        ids = [part.strip() for part in raw.split(",") if part.strip()]
        return list(dict.fromkeys(ids)) or ["primary"]

    def _time_format(self) -> str:
        fmt = self.config.get("time_format")
        return fmt if fmt in TIME_FORMATS else "12h"

    # ------------------------------------------------------------------
    # Google requests
    # ------------------------------------------------------------------

    def _get(self, token: str, url: str, params: Dict[str, Any], api: _Api) -> Dict[str, Any]:
        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.Timeout:
            raise GoogleError("Timed out contacting Google", transient=True, cooldown=TRANSIENT_COOLDOWN_SECONDS)
        except requests.exceptions.RequestException as exc:
            raise GoogleError(
                f"Could not reach Google: {type(exc).__name__}", transient=True, cooldown=TRANSIENT_COOLDOWN_SECONDS
            )

        status = response.status_code
        if status == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise GoogleError(
                    "Unexpected response from Google (not JSON)", transient=True, cooldown=TRANSIENT_COOLDOWN_SECONDS
                )
            return body
        raise self._error_for(response, api)

    @staticmethod
    def _reasons(response: requests.Response) -> Tuple[Set[str], str]:
        try:
            error = (response.json() or {}).get("error") or {}
        except (ValueError, AttributeError):
            return set(), ""
        if not isinstance(error, dict):
            return set(), ""
        reasons = {e.get("reason") for e in error.get("errors") or [] if isinstance(e, dict)}
        reasons |= {d.get("reason") for d in error.get("details") or [] if isinstance(d, dict)}
        reasons.discard(None)
        return reasons, str(error.get("message") or "")

    def _error_for(self, response: requests.Response, api: _Api) -> GoogleError:
        status = response.status_code
        reasons, message = self._reasons(response)
        if status == 401:
            return GoogleError(UNAUTHORIZED_ERROR, transient=False, cooldown=AUTH_COOLDOWN_SECONDS)
        if status == 429 or (status == 403 and reasons & RATE_LIMIT_REASONS):
            wait = self._retry_after(response)
            return GoogleError(
                f"Google rate limit reached; trying again in {int(wait)}s", transient=True, cooldown=wait
            )
        if status == 403:
            if reasons & DISABLED_REASONS:
                return GoogleError(
                    f"{api.name} API is not enabled in your Google Cloud project. Enable it under "
                    f"APIs & Services > Library, then wait a few minutes.{api.hint}",
                    transient=False,
                    cooldown=AUTH_COOLDOWN_SECONDS,
                )
            if reasons & SCOPE_REASONS:
                return GoogleError(
                    f"Google did not give FiestaBoard permission to read your {api.what}. Open this plugin's "
                    "settings, press Reconnect and tick every box on Google's consent screen.",
                    transient=False,
                    cooldown=AUTH_COOLDOWN_SECONDS,
                )
            detail = f": {message}" if message else ""
            return GoogleError(f"Google refused access (403){detail}", transient=False, cooldown=AUTH_COOLDOWN_SECONDS)
        if status >= 500:
            return GoogleError(
                f"Google is having trouble (HTTP {status}); will retry",
                transient=True,
                cooldown=TRANSIENT_COOLDOWN_SECONDS,
            )
        return GoogleError(
            f"Unexpected response from Google (HTTP {status})",
            transient=False,
            cooldown=TRANSIENT_COOLDOWN_SECONDS,
            status=status,
        )

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        try:
            wait = float(response.headers.get("Retry-After", DEFAULT_RETRY_AFTER_SECONDS))
        except (TypeError, ValueError):
            wait = DEFAULT_RETRY_AFTER_SECONDS
        return min(max(wait, 1.0), MAX_RETRY_AFTER_SECONDS)

    # ------------------------------------------------------------------
    # Snapshot, shared between boards
    # ------------------------------------------------------------------

    def _start_cooldown(self, exc: GoogleError, token: str) -> None:
        self._cooldown_until = max(self._cooldown_until, _monotonic() + exc.cooldown)
        self._cooldown_error = exc
        # A rejected token only blocks that token; reconnecting gives a new one.
        self._cooldown_token = None if exc.transient else token

    def _in_cooldown(self, token: str) -> bool:
        if self._cooldown_error is None or _monotonic() >= self._cooldown_until:
            return False
        return self._cooldown_token is None or self._cooldown_token == token

    def _load_snapshot(self, token: str, today: date, tz: ZoneInfo) -> Dict[str, Any]:
        snapshot = self._snapshot
        if (
            snapshot is not None
            and snapshot["day"] == today
            and _monotonic() - self._snapshot_at < SNAPSHOT_TTL_SECONDS
        ):
            return snapshot
        if self._in_cooldown(token):
            raise self._cooldown_error  # type: ignore[misc]
        try:
            snapshot = {
                "day": today,
                "events": self._load_events(token, today, tz),
                "tasks": self._load_tasks(token, today) if self._setting("show_tasks", True) else [],
            }
        except GoogleError as exc:
            self._start_cooldown(exc, token)
            raise
        self._snapshot = snapshot
        self._snapshot_at = _monotonic()
        return snapshot

    def _load_events(self, token: str, today: date, tz: ZoneInfo) -> List[Dict[str, Any]]:
        start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(today + timedelta(days=LOOKAHEAD_DAYS), datetime.min.time(), tzinfo=tz)
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": MAX_EVENTS_PER_CALENDAR,
            "timeZone": tz.key,
        }
        events: List[Dict[str, Any]] = []
        for calendar_id in self._calendar_ids():
            url = f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events"
            try:
                body = self._get(token, url, params, CALENDAR)
            except GoogleError as exc:
                if exc.status == 404:
                    raise GoogleError(
                        f"Calendar not found: {calendar_id}. Check Calendars in this plugin's settings.",
                        transient=False,
                        cooldown=AUTH_COOLDOWN_SECONDS,
                    )
                raise
            events.extend(item for item in body.get("items") or [] if isinstance(item, dict))
        return events

    def _load_task_lists(self, token: str) -> List[Dict[str, Any]]:
        if self._task_lists is not None and _monotonic() - self._task_lists_at < TASK_LISTS_TTL_SECONDS:
            return self._task_lists
        body = self._get(token, f"{TASKS_API}/users/@me/lists", {"maxResults": 100}, TASKS)
        self._task_lists = [item for item in body.get("items") or [] if isinstance(item, dict) and item.get("id")]
        self._task_lists_at = _monotonic()
        return self._task_lists

    def _load_tasks(self, token: str, today: date) -> List[Dict[str, Any]]:
        # Google keeps only the date of a due time, as midnight UTC.
        params: Dict[str, Any] = {
            "showCompleted": "false",
            "showHidden": "false",
            "maxResults": MAX_TASKS_PER_LIST,
        }
        if not self._setting("include_overdue", False):
            params["dueMin"] = _rfc3339_utc_midnight(today)
        params["dueMax"] = _rfc3339_utc_midnight(today + timedelta(days=1))
        tasks: List[Dict[str, Any]] = []
        for index, task_list in enumerate(self._load_task_lists(token)):
            url = f"{TASKS_API}/lists/{quote(task_list['id'], safe='')}/tasks"
            body = self._get(token, url, params, TASKS)
            for item in body.get("items") or []:
                if isinstance(item, dict):
                    tasks.append({**item, "_list": task_list.get("title", ""), "_list_index": index})
        return tasks

    # ------------------------------------------------------------------
    # fetch_data
    # ------------------------------------------------------------------

    def _board_size(self) -> Tuple[int, int]:
        board = self.board
        if board is None:
            return DEFAULT_COLS, DEFAULT_ROWS
        return board.cols, board.rows

    def fetch_data(self) -> PluginResult:
        try:
            get_token = getattr(self, "get_oauth_token", None)
            if get_token is None:
                return PluginResult(available=False, error=PLATFORM_TOO_OLD_ERROR)
            token = get_token()
            if not token:
                return PluginResult(available=False, error=NOT_CONNECTED_ERROR)

            tz = self._timezone()
            now = _utcnow().astimezone(tz)
            today = now.date()
            with self._lock:
                try:
                    snapshot = self._load_snapshot(token, today, tz)
                except GoogleError as exc:
                    stale = self._stale_snapshot(today)
                    if exc.transient and stale is not None:
                        logger.info("Google Calendar: %s; showing the last data", exc)
                        snapshot = stale
                    else:
                        logger.warning("Google Calendar: %s", exc)
                        return PluginResult(available=False, error=str(exc))
                else:
                    self._last_good = snapshot
                    self._last_good_at = _monotonic()

            cols, rows = self._board_size()
            data = self._build_data(snapshot, now, tz)
            return PluginResult(available=True, data=data, formatted_lines=self._format_display(data, cols, rows))
        except Exception as exc:  # fetch_data must never raise
            logger.exception("Error fetching Google Calendar data")
            return PluginResult(available=False, error=str(exc))

    def _stale_snapshot(self, today: date) -> Optional[Dict[str, Any]]:
        if self._last_good is None or self._last_good["day"] != today:
            return None
        if _monotonic() - self._last_good_at > STALE_DATA_LIMIT_SECONDS:
            return None
        return self._last_good

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------

    def _parse_event(self, event: Dict[str, Any], tz: ZoneInfo) -> Optional[Dict[str, Any]]:
        if event.get("status") == "cancelled" or event.get("eventType") == "workingLocation":
            return None
        if not self._setting("show_declined", False):
            for attendee in event.get("attendees") or []:
                if isinstance(attendee, dict) and attendee.get("self") and attendee.get("responseStatus") == "declined":
                    return None
        start_raw, end_raw = event.get("start") or {}, event.get("end") or {}
        try:
            if "date" in start_raw:
                start_day = date.fromisoformat(start_raw["date"])
                end_day = date.fromisoformat(end_raw.get("date") or start_raw["date"])
                start = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
                end = datetime.combine(max(end_day, start_day + timedelta(days=1)), datetime.min.time(), tzinfo=tz)
                all_day = True
            else:
                start = datetime.fromisoformat(start_raw["dateTime"].replace("Z", "+00:00")).astimezone(tz)
                end = datetime.fromisoformat(end_raw["dateTime"].replace("Z", "+00:00")).astimezone(tz)
                all_day = False
        except (KeyError, TypeError, ValueError):
            return None
        if all_day and not self._setting("show_all_day", True):
            return None
        return {
            "key": (event.get("iCalUID") or event.get("id"), start),
            "title": board_safe(event.get("summary")) or "(NO TITLE)",
            "location": board_safe(event.get("location")),
            "start": start,
            "end": end,
            "all_day": all_day,
            "busy": event.get("transparency") != "transparent",
        }

    def _events(self, raw: List[Dict[str, Any]], tz: ZoneInfo) -> List[Dict[str, Any]]:
        seen = set()
        events = []
        for item in raw:
            event = self._parse_event(item, tz)
            if event is None or event["key"] in seen:
                continue
            seen.add(event["key"])
            events.append(event)
        events.sort(key=lambda e: (e["start"], not e["all_day"], e["end"]))
        return events

    def _event_entry(self, event: Dict[str, Any], fmt: str) -> Dict[str, str]:
        if event["all_day"]:
            time_text, end_text, line = "ALL DAY", "", event["title"]
        else:
            time_text = format_time(event["start"], fmt)
            end_text = format_time(event["end"], fmt)
            line = f"{time_text} {event['title']}"
        return {
            "title": fit(event["title"]),
            "time": time_text,
            "end_time": end_text,
            "line": fit(line),
            "location": fit(event["location"]),
        }

    def _build_data(self, snapshot: Dict[str, Any], now: datetime, tz: ZoneInfo) -> Dict[str, Any]:
        fmt = self._time_format()
        today = now.date()
        day_start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        events = self._events(snapshot["events"], tz)

        agenda = [e for e in events if e["start"] < day_end and e["end"] > day_start]
        # All-day events first, then by start time.
        agenda.sort(key=lambda e: (not e["all_day"], e["start"]))
        count = len(agenda)
        headline = "NO EVENTS TODAY" if count == 0 else f"{_plural(count, 'EVENT')} TODAY"

        running = [e for e in events if not e["all_day"] and e["start"] <= now < e["end"]]
        running.sort(key=lambda e: not e["busy"])
        current = running[0] if running else None
        is_busy = any(e["busy"] for e in running)
        now_until = ""
        if current:
            end = current["end"]
            now_until = "UNTIL " + (format_time(end, fmt) if end.date() == today else WEEKDAYS[end.weekday()])

        upcoming = [e for e in events if e["start"] > now]
        nxt = upcoming[0] if upcoming else None
        next_fields = {
            "next_title": "",
            "next_time": "",
            "next_day": "",
            "next_in": "",
            "next_location": "",
            "next_line": "",
        }
        if nxt:
            entry = self._event_entry(nxt, fmt)
            day = nxt["start"].date()
            if day == today:
                day_text = "TODAY"
            elif day == today + timedelta(days=1):
                day_text = "TOMORROW"
            else:
                day_text = date_label(day)
            next_fields = {
                "next_title": entry["title"],
                "next_time": entry["time"],
                "next_day": day_text,
                "next_in": format_in(nxt["start"] - now),
                "next_location": entry["location"],
                "next_line": entry["line"],
            }

        tasks_data = self._tasks(snapshot["tasks"], today)
        return {
            "date_label": date_label(today),
            "headline": headline,
            "event_count": count,
            "events_left": sum(1 for e in agenda if e["end"] > now),
            "all_day_count": sum(1 for e in agenda if e["all_day"]),
            "is_busy": is_busy,
            "busy_tile": TILE_BUSY if is_busy else TILE_FREE,
            "now_title": fit(current["title"]) if current else "",
            "now_until": now_until,
            **next_fields,
            "events": [self._event_entry(e, fmt) for e in agenda[:MAX_LISTED]],
            **tasks_data,
        }

    def _tasks(self, raw: List[Dict[str, Any]], today: date) -> Dict[str, Any]:
        show = self._setting("show_tasks", True)
        due_today, overdue = [], []
        for item in raw:
            if item.get("status") == "completed" or not item.get("due"):
                continue
            try:
                due = date.fromisoformat(str(item["due"])[:10])
            except ValueError:
                continue
            entry = (due, item.get("_list_index", 0), str(item.get("position", "")), item)
            if due == today:
                due_today.append(entry)
            elif due < today and self._setting("include_overdue", False):
                overdue.append(entry)
        due_today.sort(key=lambda t: t[:3])
        overdue.sort(key=lambda t: t[:3])
        listed = [
            {
                "title": fit(board_safe(item.get("title")) or "(NO TITLE)"),
                "list": fit(board_safe(item["_list"])),
                "overdue": due < today,
            }
            for due, _, _, item in due_today + overdue
        ][:MAX_LISTED]
        n = len(due_today)
        if not show:
            line = ""
        elif n == 0:
            line = "NO TASKS DUE"
        else:
            line = f"{_plural(n, 'TASK')} DUE TODAY"
        return {
            "tasks_due_count": n,
            "tasks_overdue_count": len(overdue),
            "tasks_line": line,
            "next_task": listed[0]["title"] if listed else "",
            "tasks": listed,
        }

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _format_display(self, data: Dict[str, Any], cols: int, rows: int) -> List[str]:
        """Fallback layout for a board *cols* x *rows* tiles when there is no template."""
        if rows < 6:
            if data["next_title"]:
                lines = [f"NEXT {data['next_in']}", data["next_title"], f"{data['next_time']} {data['next_day']}"]
            else:
                lines = ["NOTHING NEXT", "", ""]
        else:
            footer = [data["tasks_line"]] if self._setting("show_tasks", True) else []
            slots = rows - 1 - len(footer)
            lines = [f"{data['busy_tile']} {data['headline']}"]
            lines += [e["line"] for e in data["events"][:slots]]
            lines += [""] * (slots - min(slots, len(data["events"])))
            lines += footer
        return [fit(line, cols) for line in lines[:rows]]

    def get_formatted_display(self) -> Optional[List[str]]:
        result = self.get_data(self.board)
        if not result.available or not result.data:
            return None
        cols, rows = self._board_size()
        return self._format_display(result.data, cols, rows)


# Export hook: the loader looks for a module-level `Plugin`.
Plugin = GoogleCalendarPlugin
