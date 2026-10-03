"""Tests for the agenda, next event, busy state, tasks and the board layout."""

from datetime import datetime, timedelta

import pytest
from src.devices import BoardContext
from src.oauth.provider import parse_provider_block, validate_provider_block

from plugins.google_calendar import (
    GoogleCalendarPlugin,
    board_safe,
    format_in,
    format_time,
    resolve_timezone,
)

from .conftest import CAL_API, TASKS_API
from .fixtures import all_day, timed

FLAGSHIP = BoardContext.from_device_type("flagship")
NOTE = BoardContext.from_device_type("note")


def fetch(plugin, board=None):
    with plugin._bound_board(board):
        return plugin.fetch_data()


def data(plugin):
    result = fetch(plugin)
    assert result.available is True, result.error
    return result.data


# ----------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------


class TestManifest:
    def test_oauth_block_is_valid_and_has_no_secret(self, manifest_data):
        block = manifest_data["oauth"]
        assert validate_provider_block(block) == []
        assert "client_secret" not in block
        provider = parse_provider_block(block, manifest_data["name"])
        assert provider.flows == ("relay",)

    def test_oauth_block_matches_google(self, manifest_data):
        block = manifest_data["oauth"]
        assert block["authorization_url"] == "https://accounts.google.com/o/oauth2/v2/auth"
        assert block["token_url"] == "https://oauth2.googleapis.com/token"
        assert block["scopes"] == [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/tasks.readonly",
        ]
        assert block["authorization_params"] == {"access_type": "offline", "prompt": "consent"}
        assert block["client_secret_setting"] == "client_secret"
        assert "client_id" not in block

    def test_secret_setting_is_a_password(self, manifest_data):
        props = manifest_data["settings_schema"]["properties"]
        assert props["client_secret"]["ui:widget"] == "password"
        assert "required" not in manifest_data["settings_schema"]

    def test_manifest_loads_in_core(self, manifest_data):
        from src.plugins.manifest import PluginManifest

        manifest = PluginManifest.from_dict(manifest_data)
        assert manifest.id == "google_calendar"
        assert manifest.category == "utility"

    def test_plugin_id(self, plugin):
        assert plugin.plugin_id == "google_calendar"

    def test_every_declared_variable_is_produced(self, plugin, google_api, manifest_data):
        produced = data(plugin)
        for name in manifest_data["variables"]["simple"]:
            assert name in produced, name
        for name, spec in manifest_data["variables"]["arrays"].items():
            assert produced[name], name
            for field in spec["item_fields"]:
                assert field in produced[name][0], (name, field)

    def test_max_lengths_hold(self, plugin, google_api, manifest_data):
        produced = data(plugin)
        for name, spec in manifest_data["variables"]["simple"].items():
            if "max_length" in spec and spec["type"] == "string" and not name.endswith("_tile"):
                assert len(produced[name]) <= spec["max_length"], name


# ----------------------------------------------------------------------
# Requests
# ----------------------------------------------------------------------


class TestRequests:
    def test_events_request(self, plugin, google_api):
        fetch(plugin)
        url, kwargs = google_api.calls[0]
        assert url == f"{CAL_API}/calendars/primary/events"
        assert kwargs["params"] == {
            "timeMin": "2026-10-02T00:00:00-07:00",
            "timeMax": "2026-10-10T00:00:00-07:00",
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
            "timeZone": "America/Los_Angeles",
        }
        assert kwargs["headers"]["Authorization"] == "Bearer test_access_token"
        assert kwargs["timeout"] == 10

    def test_tasks_requests(self, plugin, google_api):
        fetch(plugin)
        urls = google_api.urls()
        assert f"{TASKS_API}/users/@me/lists" in urls
        tasks_call = [kw for url, kw in google_api.calls if url == f"{TASKS_API}/lists/list_mine/tasks"][0]
        assert tasks_call["params"] == {
            "showCompleted": "false",
            "showHidden": "false",
            "maxResults": 100,
            "dueMin": "2026-10-02T00:00:00.000Z",
            "dueMax": "2026-10-03T00:00:00.000Z",
        }

    def test_calendar_ids_are_url_encoded(self, plugin, google_api):
        holidays = "en.usa#holiday@group.v.calendar.google.com"
        google_api.calendars[holidays] = []
        plugin.config = {**plugin.config, "calendar_ids": f"primary, {holidays}"}
        fetch(plugin)
        assert f"{CAL_API}/calendars/en.usa%23holiday%40group.v.calendar.google.com/events" in google_api.urls()

    def test_blank_calendar_ids_mean_primary(self, plugin, google_api):
        plugin.config = {**plugin.config, "calendar_ids": " , "}
        fetch(plugin)
        assert google_api.calls[0][0] == f"{CAL_API}/calendars/primary/events"

    def test_tasks_off_makes_no_tasks_requests(self, plugin, google_api):
        plugin.config = {**plugin.config, "show_tasks": False}
        produced = data(plugin)
        assert not any("tasks.googleapis.com" in url for url in google_api.urls())
        assert produced["tasks_due_count"] == 0
        assert produced["tasks_line"] == ""
        assert produced["tasks"] == []

    def test_one_fetch_serves_every_board(self, plugin, google_api):
        fetch(plugin, FLAGSHIP)
        count = len(google_api.calls)
        fetch(plugin, NOTE)
        assert len(google_api.calls) == count

    def test_snapshot_expires(self, plugin, google_api, clock):
        fetch(plugin)
        count = len(google_api.calls)
        clock.advance(31)
        fetch(plugin)
        # Task lists are remembered for an hour, so one request fewer.
        assert len(google_api.calls) == count * 2 - 1

    def test_new_day_fetches_again(self, plugin, google_api, clock):
        fetch(plugin)
        clock.advance(15 * 3600)  # 01:00 on Saturday
        produced = data(plugin)
        assert produced["date_label"] == "SAT OCT 3"
        assert google_api.calls[-4][1]["params"]["timeMin"] == "2026-10-03T00:00:00-07:00"

    def test_config_change_resets(self, plugin, google_api):
        fetch(plugin)
        count = len(google_api.calls)
        plugin.config = {**plugin.config, "time_format": "24h"}
        fetch(plugin)
        assert len(google_api.calls) == count * 2


# ----------------------------------------------------------------------
# Agenda
# ----------------------------------------------------------------------


class TestAgenda:
    def test_today(self, plugin, google_api):
        produced = data(plugin)
        assert produced["date_label"] == "FRI OCT 2"
        # Fundraiser, Standup, Design Review, Lunch, Dentist. Declined,
        # cancelled, working location and tomorrow's Gym are left out.
        assert produced["event_count"] == 5
        assert produced["headline"] == "5 EVENTS TODAY"
        assert produced["all_day_count"] == 1
        assert produced["events_left"] == 4
        assert [e["title"] for e in produced["events"]] == [
            "FALL FUNDRAISER",
            "STANDUP",
            "DESIGN REVIEW",
            "LUNCH WITH SAM",
            "DENTIST",
        ]

    def test_event_fields(self, plugin, google_api):
        events = data(plugin)["events"]
        assert events[0] == {
            "title": "FALL FUNDRAISER",
            "time": "ALL DAY",
            "end_time": "",
            "line": "FALL FUNDRAISER",
            "location": "",
        }
        assert events[2] == {
            "title": "DESIGN REVIEW",
            "time": "9:45AM",
            "end_time": "10:30AM",
            "line": "9:45AM DESIGN REVIEW",
            "location": "ROOM 4B",
        }
        assert events[1]["time"] == "9AM"

    def test_24h(self, plugin, google_api):
        plugin.config = {**plugin.config, "time_format": "24h"}
        events = data(plugin)["events"]
        assert events[2]["line"] == "09:45 DESIGN REVIEW"
        assert events[4]["end_time"] == "17:00"

    def test_show_declined(self, plugin, google_api):
        plugin.config = {**plugin.config, "show_declined": True}
        assert "BUDGET SYNC" in [e["title"] for e in data(plugin)["events"]]

    def test_hide_all_day(self, plugin, google_api):
        plugin.config = {**plugin.config, "show_all_day": False}
        produced = data(plugin)
        assert produced["event_count"] == 4
        assert produced["all_day_count"] == 0

    @pytest.mark.parametrize("count,headline", [(0, "NO EVENTS TODAY"), (1, "1 EVENT TODAY")])
    def test_headline(self, plugin, google_api, count, headline):
        google_api.calendars["primary"] = [timed("e1", "Only", "11:00", "12:00")][:count]
        assert data(plugin)["headline"] == headline

    def test_multi_day_event_overlaps_today(self, plugin, google_api):
        google_api.calendars["primary"] = [
            all_day("trip", "Trip", "2026-09-30", "2026-10-04"),
            timed("red_eye", "Red-eye", "2026-10-01T23:00:00-07:00", "2026-10-02T06:00:00-07:00"),
            all_day("yesterday", "Yesterday", "2026-10-01", "2026-10-02"),
        ]
        assert [e["title"] for e in data(plugin)["events"]] == ["TRIP", "RED-EYE"]

    def test_merges_and_dedupes_calendars(self, plugin, google_api):
        shared = timed("shared", "Family Dinner", "18:00", "19:00")
        google_api.calendars["primary"] = [timed("a", "Call", "11:00", "11:30"), shared]
        google_api.calendars["family@example.com"] = [dict(shared, id="copy"), timed("b", "Soccer", "10:30", "11:00")]
        plugin.config = {**plugin.config, "calendar_ids": "primary,family@example.com"}
        assert [e["title"] for e in data(plugin)["events"]] == ["SOCCER", "CALL", "FAMILY DINNER"]

    def test_untitled_event(self, plugin, google_api):
        event = timed("x", "", "11:00", "12:00")
        del event["summary"]
        google_api.calendars["primary"] = [event]
        assert data(plugin)["events"][0]["title"] == "(NO TITLE)"

    def test_events_array_is_capped(self, plugin, google_api):
        google_api.calendars["primary"] = [
            timed(f"e{i}", f"Slot {i}", f"{11 + i // 4}:{(i % 4) * 15:02d}", "23:00") for i in range(12)
        ]
        produced = data(plugin)
        assert produced["event_count"] == 12
        assert len(produced["events"]) == 10

    def test_long_titles_fit(self, plugin, google_api):
        google_api.calendars["primary"] = [timed("x", "Quarterly business review with everyone", "11:30", "12:00")]
        event = data(plugin)["events"][0]
        assert event["title"] == "QUARTERLY BUSINESS REV"
        assert event["line"] == "11:30AM QUARTERLY BUSI"

    def test_bad_event_times_are_skipped(self, plugin, google_api):
        broken = timed("x", "Broken", "11:00", "12:00")
        broken["start"] = {}
        google_api.calendars["primary"] = [broken, timed("y", "Fine", "11:00", "12:00")]
        assert [e["title"] for e in data(plugin)["events"]] == ["FINE"]


# ----------------------------------------------------------------------
# Now and next
# ----------------------------------------------------------------------


class TestNowAndNext:
    def test_busy(self, plugin, google_api):
        produced = data(plugin)
        assert produced["is_busy"] is True
        assert produced["busy_tile"] == "{63}"
        assert produced["now_title"] == "DESIGN REVIEW"
        assert produced["now_until"] == "UNTIL 10:30AM"

    def test_free(self, plugin, google_api, clock):
        clock.advance(45 * 60)  # 10:45
        produced = data(plugin)
        assert produced["is_busy"] is False
        assert produced["busy_tile"] == "{66}"
        assert produced["now_title"] == ""
        assert produced["now_until"] == ""

    def test_free_time_event_is_not_busy(self, plugin, google_api):
        google_api.calendars["primary"] = [timed("x", "Focus", "09:00", "11:00", transparency="transparent")]
        produced = data(plugin)
        assert produced["is_busy"] is False
        assert produced["now_title"] == "FOCUS"

    def test_event_running_past_midnight(self, plugin, google_api):
        google_api.calendars["primary"] = [timed("x", "Hackathon", "09:00", "2026-10-03T02:00:00-07:00")]
        assert data(plugin)["now_until"] == "UNTIL SAT"

    def test_next_event(self, plugin, google_api):
        produced = data(plugin)
        assert produced["next_title"] == "LUNCH WITH SAM"
        assert produced["next_time"] == "1PM"
        assert produced["next_day"] == "TODAY"
        assert produced["next_in"] == "IN 3H"
        assert produced["next_location"] == "CAFE RIO, 12 MAIN ST"
        assert produced["next_line"] == "1PM LUNCH WITH SAM"

    def test_next_event_tomorrow(self, plugin, google_api, clock):
        clock.advance(7 * 3600 + 60)  # 17:01, after the Dentist started
        produced = data(plugin)
        assert produced["next_title"] == "GYM"
        assert produced["next_day"] == "TOMORROW"
        assert produced["next_in"] == "IN 14H"

    def test_next_event_later_in_the_week(self, plugin, google_api):
        google_api.calendars["primary"] = [all_day("x", "Holiday", "2026-10-05", "2026-10-06")]
        produced = data(plugin)
        assert produced["next_day"] == "MON OCT 5"
        assert produced["next_time"] == "ALL DAY"
        assert produced["next_line"] == "HOLIDAY"
        assert produced["next_in"] == "IN 2D"

    def test_nothing_next(self, plugin, google_api):
        google_api.calendars["primary"] = []
        produced = data(plugin)
        assert produced["next_title"] == ""
        assert produced["next_in"] == ""
        assert produced["next_line"] == ""


# ----------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------


class TestTasks:
    def test_due_today(self, plugin, google_api):
        produced = data(plugin)
        assert produced["tasks_due_count"] == 2
        assert produced["tasks_overdue_count"] == 0
        assert produced["tasks_line"] == "2 TASKS DUE TODAY"
        assert produced["next_task"] == "WATER THE PLANTS"
        assert produced["tasks"] == [
            {"title": "WATER THE PLANTS", "list": "MY TASKS", "overdue": False},
            {"title": "TAKE OUT TRASH", "list": "HOME", "overdue": False},
        ]

    def test_include_overdue(self, plugin, google_api):
        plugin.config = {**plugin.config, "include_overdue": True}
        produced = data(plugin)
        params = [kw for url, kw in google_api.calls if url.endswith("/lists/list_mine/tasks")][0]["params"]
        assert "dueMin" not in params
        assert produced["tasks_due_count"] == 2
        assert produced["tasks_overdue_count"] == 1
        assert produced["tasks"][-1] == {"title": "FILE EXPENSES", "list": "MY TASKS", "overdue": True}

    @pytest.mark.parametrize("count,line", [(0, "NO TASKS DUE"), (1, "1 TASK DUE TODAY")])
    def test_tasks_line(self, plugin, google_api, count, line):
        google_api.tasks = {"list_mine": google_api.tasks["list_mine"][:count], "list_home": []}
        assert data(plugin)["tasks_line"] == line

    def test_task_lists_are_remembered(self, plugin, google_api, clock):
        fetch(plugin)
        clock.advance(60)
        fetch(plugin)
        assert google_api.urls().count(f"{TASKS_API}/users/@me/lists") == 1
        clock.advance(3600)
        fetch(plugin)
        assert google_api.urls().count(f"{TASKS_API}/users/@me/lists") == 2

    def test_no_task_lists(self, plugin, google_api):
        google_api.task_lists = {"kind": "tasks#taskLists"}
        produced = data(plugin)
        assert produced["tasks"] == []
        assert produced["next_task"] == ""


# ----------------------------------------------------------------------
# Timezone and settings
# ----------------------------------------------------------------------


class TestTimezoneAndSettings:
    def test_falls_back_to_fiestaboard_timezone(self, plugin, google_api, monkeypatch):
        monkeypatch.setattr("plugins.google_calendar._general_timezone", lambda: "Europe/London")
        plugin.config = {**plugin.config, "timezone": ""}
        fetch(plugin)
        params = google_api.calls[0][1]["params"]
        assert params["timeZone"] == "Europe/London"
        assert params["timeMin"] == "2026-10-02T00:00:00+01:00"

    def test_resolve_timezone(self):
        assert resolve_timezone("Asia/Tokyo").key == "Asia/Tokyo"
        assert resolve_timezone("Not/AZone") is None
        assert resolve_timezone("") is None

    def test_bad_timezone_falls_back_to_utc(self, plugin, google_api, monkeypatch):
        monkeypatch.setattr("plugins.google_calendar._general_timezone", lambda: "Nowhere/Land")
        plugin.config = {**plugin.config, "timezone": ""}
        fetch(plugin)
        assert google_api.calls[0][1]["params"]["timeZone"] == "UTC"

    def test_validate_config_ok(self, plugin, config):
        assert plugin.validate_config(config) == []
        assert plugin.validate_config({}) == []

    def test_validate_config_errors(self, plugin):
        errors = plugin.validate_config(
            {"client_id": "GOCSPX-oops", "timezone": "Mars/Base", "time_format": "13h", "refresh_seconds": 5}
        )
        joined = " ".join(errors)
        assert "apps.googleusercontent.com" in joined
        assert "Mars/Base" in joined
        assert "Time format" in joined
        assert len(errors) == 4


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class TestHelpers:
    def test_board_safe(self):
        assert board_safe("Café {meeting} 🎉") == "CAFE MEETING"
        assert board_safe(None) == ""

    @pytest.mark.parametrize(
        "hour,minute,fmt,expected",
        [(0, 0, "12h", "12AM"), (12, 30, "12h", "12:30PM"), (23, 5, "12h", "11:05PM"), (7, 5, "24h", "07:05")],
    )
    def test_format_time(self, hour, minute, fmt, expected):
        assert format_time(datetime(2026, 1, 1, hour, minute), fmt) == expected

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (5, "IN 1M"),
            (25 * 60, "IN 25M"),
            (90 * 60, "IN 1H 30M"),
            (2 * 3600, "IN 2H"),
            (20 * 3600, "IN 20H"),
            (3 * 86400, "IN 3D"),
        ],
    )
    def test_format_in(self, seconds, expected):
        assert format_in(timedelta(seconds=seconds)) == expected


# ----------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------


class TestDisplay:
    def test_flagship(self, plugin, google_api):
        result = fetch(plugin, FLAGSHIP)
        assert result.formatted_lines == [
            "{63} 5 EVENTS TODAY",
            "FALL FUNDRAISER",
            "9AM STANDUP",
            "9:45AM DESIGN REVIEW",
            "1PM LUNCH WITH SAM",
            "2 TASKS DUE TODAY",
        ]

    def test_flagship_without_tasks(self, plugin, google_api):
        plugin.config = {**plugin.config, "show_tasks": False}
        lines = fetch(plugin, FLAGSHIP).formatted_lines
        assert lines[-1] == "4PM DENTIST"

    def test_note(self, plugin, google_api):
        assert fetch(plugin, NOTE).formatted_lines == ["NEXT IN 3H", "LUNCH WITH SAM", "1PM TODAY"]

    def test_note_nothing_next(self, plugin, google_api):
        google_api.calendars["primary"] = []
        assert fetch(plugin, NOTE).formatted_lines == ["NOTHING NEXT", "", ""]

    def test_get_formatted_display(self, plugin, google_api):
        lines = plugin.get_formatted_display()
        assert lines[0] == "{63} 5 EVENTS TODAY"

    def test_get_formatted_display_unavailable(self, plugin, google_api, token):
        token.value = None
        assert plugin.get_formatted_display() is None


def test_module_exports_plugin():
    import plugins.google_calendar as module

    assert module.Plugin is GoogleCalendarPlugin
