"""Tests for signing in, Google's error answers, cooldowns and stale data."""

import pytest
import requests

from plugins.google_calendar import (
    NOT_CONNECTED_ERROR,
    PLATFORM_TOO_OLD_ERROR,
    UNAUTHORIZED_ERROR,
    GoogleCalendarPlugin,
)

from .conftest import CAL_API, FakeResponse
from .fixtures import google_error

EVENTS = "/calendars/primary/events"
TASKS = "/lists/list_mine/tasks"


def fetch(plugin):
    return plugin.fetch_data()


class TestToken:
    @pytest.mark.parametrize("value", [None, ""])
    def test_not_connected(self, plugin, google_api, token, value):
        token.value = value
        result = fetch(plugin)
        assert result.available is False
        assert result.error == NOT_CONNECTED_ERROR
        assert google_api.calls == []

    def test_token_is_asked_for_on_every_fetch(self, plugin, google_api, clock, token):
        fetch(plugin)
        token.value = "test_refreshed_token"
        clock.advance(31)
        fetch(plugin)
        assert google_api.calls[-1][1]["headers"]["Authorization"] == "Bearer test_refreshed_token"

    def test_platform_without_oauth(self, manifest_data, google_api, clock, monkeypatch):
        from src.plugins.base import PluginBase

        monkeypatch.delattr(PluginBase, "get_oauth_token")
        result = GoogleCalendarPlugin(manifest_data).fetch_data()
        assert result.error == PLATFORM_TOO_OLD_ERROR

    def test_uses_the_platform_token(self, manifest_data, google_api, clock, monkeypatch):
        from src.plugins.base import PluginBase

        monkeypatch.setattr(PluginBase, "get_oauth_token", lambda self: "test_platform_token")
        plugin = GoogleCalendarPlugin(manifest_data)
        plugin.config = {"timezone": "America/Los_Angeles"}
        assert plugin.fetch_data().available is True
        assert google_api.calls[0][1]["headers"]["Authorization"] == "Bearer test_platform_token"


class TestErrors:
    def test_401(self, plugin, google_api):
        google_api.fail(EVENTS, FakeResponse(google_error(401, "authError", status="UNAUTHENTICATED"), 401))
        result = fetch(plugin)
        assert result.available is False
        assert result.error == UNAUTHORIZED_ERROR

    def test_401_cooldown_is_per_token(self, plugin, google_api, token, clock):
        google_api.fail(EVENTS, FakeResponse(None, 401))
        fetch(plugin)
        count = len(google_api.calls)
        clock.advance(5)
        assert fetch(plugin).error == UNAUTHORIZED_ERROR
        assert len(google_api.calls) == count
        token.value = "test_new_token"
        assert fetch(plugin).available is True

    def test_calendar_api_disabled(self, plugin, google_api):
        google_api.fail(
            EVENTS,
            FakeResponse(
                google_error(403, "accessNotConfigured", "Google Calendar API has not been used", "SERVICE_DISABLED"),
                403,
            ),
        )
        error = fetch(plugin).error
        assert "Google Calendar API is not enabled" in error
        assert "APIs & Services > Library" in error

    def test_tasks_api_disabled(self, plugin, google_api):
        google_api.fail(
            "/users/@me/lists", FakeResponse(google_error(403, "forbidden", detail_reason="SERVICE_DISABLED"), 403)
        )
        error = fetch(plugin).error
        assert "Google Tasks API is not enabled" in error
        assert "Show Tasks" in error

    def test_scope_not_granted(self, plugin, google_api):
        google_api.fail(
            "/users/@me/lists",
            FakeResponse(
                google_error(403, "insufficientPermissions", detail_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT"), 403
            ),
        )
        error = fetch(plugin).error
        assert "permission to read your tasks" in error
        assert "Reconnect" in error

    def test_calendar_scope_not_granted(self, plugin, google_api):
        google_api.fail(EVENTS, FakeResponse(google_error(403, "insufficientPermissions"), 403))
        assert "permission to read your calendars" in fetch(plugin).error

    def test_other_403(self, plugin, google_api):
        google_api.fail(
            EVENTS, FakeResponse(google_error(403, "forbidden", "The caller does not have permission"), 403)
        )
        assert fetch(plugin).error == "Google refused access (403): The caller does not have permission"

    def test_403_without_body(self, plugin, google_api):
        google_api.fail(EVENTS, FakeResponse(None, 403))
        assert fetch(plugin).error == "Google refused access (403)"

    def test_calendar_not_found(self, plugin, google_api):
        plugin.config = {**plugin.config, "calendar_ids": "primary,test_missing@example.com"}
        error = fetch(plugin).error
        assert error == "Calendar not found: test_missing@example.com. Check Calendars in this plugin's settings."

    @pytest.mark.parametrize(
        "response",
        [
            FakeResponse(
                google_error(429, "rateLimitExceeded", status="RESOURCE_EXHAUSTED"), 429, {"Retry-After": "120"}
            ),
            FakeResponse(google_error(403, "userRateLimitExceeded"), 403),
        ],
    )
    def test_rate_limit_cools_down(self, plugin, google_api, clock, response):
        google_api.fail(EVENTS, response)
        assert "rate limit" in fetch(plugin).error
        count = len(google_api.calls)
        clock.advance(30)
        assert "rate limit" in fetch(plugin).error
        assert len(google_api.calls) == count
        clock.advance(200)
        assert fetch(plugin).available is True

    def test_retry_after_is_bounded(self, plugin, google_api, clock):
        google_api.fail(EVENTS, FakeResponse(None, 429, {"Retry-After": "nonsense"}))
        assert "60s" in fetch(plugin).error

    def test_server_error(self, plugin, google_api):
        google_api.fail(EVENTS, FakeResponse(None, 503))
        assert "Google is having trouble (HTTP 503)" in fetch(plugin).error

    def test_unexpected_status(self, plugin, google_api):
        google_api.fail(EVENTS, FakeResponse(None, 418))
        assert fetch(plugin).error == "Unexpected response from Google (HTTP 418)"

    def test_bad_json(self, plugin, google_api):
        class Broken(FakeResponse):
            def json(self):
                raise ValueError("bad json")

        google_api.fail(EVENTS, Broken({}, 200))
        assert "Unexpected response from Google" in fetch(plugin).error

    def test_timeout(self, plugin, google_api):
        google_api.fail(EVENTS, requests.exceptions.Timeout("slow"))
        assert fetch(plugin).error == "Timed out contacting Google"

    def test_connection_error(self, plugin, google_api):
        google_api.fail(EVENTS, requests.exceptions.ConnectionError("down"))
        assert fetch(plugin).error == "Could not reach Google: ConnectionError"

    def test_unexpected_exception_never_raises(self, plugin, google_api, monkeypatch):
        monkeypatch.setattr(plugin, "_build_data", lambda *a: 1 / 0)
        result = fetch(plugin)
        assert result.available is False
        assert "division" in result.error


class TestStaleData:
    def test_outage_keeps_last_data(self, plugin, google_api, clock):
        first = fetch(plugin)
        clock.advance(60)
        google_api.fail(EVENTS, FakeResponse(None, 500))
        result = fetch(plugin)
        assert result.available is True
        assert result.data["event_count"] == first.data["event_count"]
        # The cooldown means the next render does not ask Google again.
        count = len(google_api.calls)
        clock.advance(5)
        assert fetch(plugin).available is True
        assert len(google_api.calls) == count

    def test_stale_data_expires(self, plugin, google_api, clock):
        fetch(plugin)
        clock.advance(31 * 60)
        google_api.fail(EVENTS, FakeResponse(None, 500))
        assert fetch(plugin).available is False

    def test_stale_data_is_not_shown_for_another_day(self, plugin, google_api, clock):
        clock.advance(13 * 3600 + 50 * 60)  # 23:50
        fetch(plugin)
        clock.advance(15 * 60)  # 00:05 the next day
        google_api.fail(EVENTS, FakeResponse(None, 500))
        assert fetch(plugin).available is False

    def test_auth_error_does_not_show_stale_data(self, plugin, google_api, clock):
        fetch(plugin)
        clock.advance(60)
        google_api.fail(EVENTS, FakeResponse(None, 401))
        assert fetch(plugin).available is False

    def test_tasks_failure_fails_the_fetch(self, plugin, google_api):
        google_api.fail(TASKS, FakeResponse(None, 500))
        assert "Google is having trouble" in fetch(plugin).error

    def test_events_url(self, plugin, google_api):
        fetch(plugin)
        assert google_api.calls[0][0] == f"{CAL_API}/calendars/primary/events"
