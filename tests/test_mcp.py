import asyncio

import httpx
import pytest

from infomentor_mcp.app.mcp import McpApp
from infomentor_mcp.app.service import InfoMentorService
from infomentor_mcp.storage.session import AuthSession, SessionStore

NEWS = "/Communication/News/GetNewsList"
CALENDAR = "/calendarv2/calendarv2/getentries"
WEEKS = "/TimeRegistration/TimeRegistration/GetTimeRegistrations/"
TIMETABLE = "/timetable/timetable/gettimetablelist"
NOTIFICATIONS = "/NotificationApp/NotificationApp/GetNotifications"
WEEK = {"start_date": "2026-10-19", "end_date": "2026-10-23"}


def call(server, tool, args=None):
    return asyncio.run(server.call_tool(tool, args or {})).structured_content


@pytest.fixture
def server(fake_hub, store):
    return McpApp(InfoMentorService(store)).build()


def test_server_registers_only_verified_data_tools() -> None:
    tools = asyncio.run(McpApp().build().list_tools())

    assert {tool.name for tool in tools} == {
        "auth_status", "auth_start", "auth_progress", "auth_cancel", "auth_revoke",
        "auth_wait", "get_news", "get_attachments", "get_calendar", "get_notifications",
        "get_childcare_schedule", "list_children", "set_childcare_schedule",
        "set_childcare_comment", "get_timetable", "sync_changes", "get_changes", "ack_changes",
    }
    by_name = {tool.name: tool for tool in tools}
    assert by_name["get_news"].input_schema["properties"]["include_attachments"]["default"]
    childcare = by_name["get_childcare_schedule"]
    assert childcare.input_schema["properties"]["include_comments"]["default"]
    assert not childcare.annotations.read_only_hint
    for name in ("get_calendar", "get_timetable", "get_notifications"):
        assert by_name[name].annotations.read_only_hint


def test_list_children_and_news_return_structured_data(fake_hub, server):
    fake_hub.route(NEWS, lambda request, child: {"items": [
        {"id": f"news-{index}", "publishedDate": f"2026-10-0{index}"} for index in range(1, 4)
    ]})
    children = call(server, "list_children")["children"]
    assert [(c["id"], c["selected"]) for c in children] == [("first", False), ("second", True)]
    result = call(server, "get_news", {"child_ids": ["first"]})
    assert result["items"][0]["child_ids"] == ["first"]
    page = call(server, "get_news", {"offset": 1, "limit": 1})
    assert [item["id"] for item in page["items"]] == ["news-2"]
    assert page["items"][0]["child_ids"] == ["first", "second"]


def test_data_tools_explain_missing_authentication(fake_hub):
    server = McpApp(InfoMentorService(SessionStore())).build()
    assert call(server, "list_children")["error"]["code"] == "authentication_required"
    assert not fake_hub.httpx_mock.get_requests()


def test_notifications_tool_paginates(fake_hub, server):
    fake_hub.route(NOTIFICATIONS, lambda request, child: {"notifications": [{
        "id": index, "orderDate": f"2026-10-0{index}", "pupilIM2Id": 1, "pupilSourceId": "1",
        "currentlySelectedPupil": True,
    } for index in range(1, 4)]})
    result = call(server, "get_notifications", {"child_ids": ["first"], "offset": 1, "limit": 1})
    assert [item["id"] for item in result["items"]] == ["2"]
    invalid = call(server, "get_notifications", {"limit": 0})
    assert invalid["error"]["code"] == "invalid_pagination"
    assert fake_hub.read_by(NOTIFICATIONS) == ["first"]


def test_calendar_tool_paginates_in_stockholm_time(fake_hub, server):
    fake_hub.route(CALENDAR, lambda request, child: [{
        "id": index, "isAllDayEvent": False,
        "startDateFull": "2026-10-22T10:00:00", "endDateFull": "2026-10-22T11:00:00",
    } for index in range(3)])
    result = call(server, "get_calendar", {
        "start_date": "2026-10-22", "end_date": "2026-10-30", "offset": 1, "limit": 1,
    })
    assert [item["id"] for item in result["items"]] == ["1"]
    assert result["items"][0]["starts_at"] == "2026-10-22T10:00:00+02:00"
    assert result["timezone"] == "Europe/Stockholm"


def test_childcare_tool_serializes_and_paginates(fake_hub, server):
    fake_hub.route(WEEKS, lambda request, child: {
        "startDate": "2026-10-19", "endDate": "2026-10-25", "days": [
            {"date": f"2026-10-{day}T00:00:00", "onLeave": False, "isSchoolClosed": False}
            for day in (19, 20)
        ],
    })
    result = call(server, "get_childcare_schedule", {
        **WEEK, "child_ids": ["first"], "offset": 1, "limit": 1, "include_comments": False,
    })
    assert [item["date"] for item in result["items"]] == ["2026-10-20"]


def test_timetable_tool_serializes_and_reports_remaining_page(fake_hub, server):
    fake_hub.route(TIMETABLE, lambda request, child: [{
        "title": f"Lesson {index}", "allDay": False,
        "start": "2026-10-22T10:00:00", "end": "2026-10-22T11:00:00",
    } for index in range(3)])
    result = call(server, "get_timetable", {
        **WEEK, "child_ids": ["first"], "offset": 1, "limit": 1,
    })
    assert result["total"] == 3 and result["has_more"]
    assert result["items"][0]["title"] == "Lesson 1"
    assert result["items"][0]["starts_at"] == "2026-10-22T10:00:00+02:00"


@pytest.mark.parametrize("tool,end", [
    ("get_calendar", "2026-10-21"), ("get_calendar", "2027-10-23"),
    ("get_childcare_schedule", "2026-10-21"), ("get_childcare_schedule", "2026-11-22"),
    ("get_timetable", "2026-10-21"), ("get_timetable", "2026-11-22"),
])
def test_schedule_invalid_range_is_rejected_before_auth(httpx_mock, tool, end):
    server = McpApp(InfoMentorService(SessionStore())).build()
    result = call(server, tool, {"start_date": "2026-10-22", "end_date": end})
    assert result["error"]["code"] == "invalid_date_range"
    assert not httpx_mock.get_requests()


def test_rejected_mobile_authorization_is_reported_as_auth_required(fake_hub, store):
    # A bound session without stored mobile credentials cannot be renewed.
    store.save(AuthSession(mobile_binding="binding"))
    fake_hub.expired = True
    server = McpApp(InfoMentorService(store)).build()
    assert call(server, "list_children")["error"]["code"] == "authentication_required"
    assert len(fake_hub.httpx_mock.get_requests()) == 1


def test_http_errors_never_echo_signed_urls_or_response_details(fake_hub, server):
    fake_hub.unavailable = httpx.ConnectError("secret-token in private-url")
    result = call(server, "list_children")
    assert result["error"]["code"] == "infomentor_request_failed"
    assert "secret-token" not in str(result)


def test_corrupt_local_session_does_not_leak_validation_input(httpx_mock):
    class BrokenStore(SessionStore):
        def load(self):
            raise ValueError("secret-cookie in malformed local data")

    server = McpApp(InfoMentorService(BrokenStore())).build()
    for tool in ("list_children", "auth_status"):
        result = call(server, tool)
        assert result["error"]["code"] == "session_storage_error"
        assert "secret-cookie" not in str(result)
