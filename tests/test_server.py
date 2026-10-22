import asyncio
from contextlib import nullcontext
from datetime import date, datetime

import httpx
import pytest

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.http import AuthenticationExpiredError
from infomentor_mcp.client.models import CalendarEntry, Child, ChildcareDay, NewsItem, Notification
from infomentor_mcp.server import create_server


class MemorySessionStore:
    def __init__(self, session: AuthSession | None) -> None:
        self.session = session

    def load(self) -> AuthSession | None:
        return self.session

    def locked(self, timeout=30):
        return nullcontext()


class FakeClient:
    def __init__(self, session: AuthSession) -> None:
        self.session = session

    def close(self) -> None:
        pass

    def list_children(self) -> list[Child]:
        return [Child(id="child-1", name="Synthetic Child", selected=True)]

    def get_notifications(self, child_ids):
        return [Notification(
            id=str(index), child_ids=child_ids or ["child-1"], states={"child-1": "New"}
        ) for index in range(3)]

    def get_calendar(self, start_date, end_date, child_ids):
        assert start_date.isoformat() == "2026-10-22"
        assert end_date.isoformat() == "2026-10-30"
        return [CalendarEntry(
            id=str(index), child_ids=child_ids or ["child-1"], all_day=False,
            starts_at=datetime.fromisoformat("2026-10-22T10:00:00+02:00"),
            ends_at=datetime.fromisoformat("2026-10-22T11:00:00+02:00"),
        ) for index in range(3)]

    def get_childcare_schedule(self, start_date, end_date, child_ids, *, include_comments=True,
                              offset=0, limit=None):
        assert start_date.isoformat() == "2026-10-22" and end_date.isoformat() == "2026-10-30"
        items = [ChildcareDay(
            child_id="child-1", date=date(2026, 10, day), on_leave=False, is_closed=False,
        ) for day in (22, 23, 26)]
        return items[offset : offset + limit if limit is not None else None]

    def get_news(self, child_ids: list[str] | None, *, offset=0, limit=None,
                 include_attachments=False) -> list[NewsItem]:
        items = [
            NewsItem(
                id=f"news-{index}",
                child_ids=child_ids if child_ids is not None else ["child-1"],
                title="Synthetic news",
                source_reference=f"/#/communication/news/news-{index}",
                raw={"id": f"news-{index}"},
            )
            for index in range(1, 4)
        ]
        return items[offset : offset + limit if limit is not None else None]


def test_server_registers_only_verified_data_tools() -> None:
    server = create_server(MemorySessionStore(None), FakeClient)

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {
        "auth_status", "auth_start", "auth_progress", "auth_cancel", "auth_revoke",
        "auth_wait", "get_news", "get_attachments", "get_calendar", "get_notifications",
        "get_childcare_schedule", "list_children",
    }


def test_news_tool_returns_structured_data() -> None:
    server = create_server(MemorySessionStore(AuthSession()), FakeClient)

    result = asyncio.run(server.call_tool("get_news", {"child_ids": ["child-1"]}))

    assert result.structured_content["items"][0]["child_ids"] == ["child-1"]
    page = asyncio.run(server.call_tool("get_news", {"offset": 1, "limit": 1}))
    assert [item["id"] for item in page.structured_content["items"]] == ["news-2"]


def test_data_tools_explain_missing_authentication() -> None:
    server = create_server(MemorySessionStore(None), FakeClient)

    result = asyncio.run(server.call_tool("list_children", {}))

    assert result.structured_content["error"]["code"] == "authentication_required"


def test_notifications_tool_paginates_and_is_read_only():
    async def scenario():
        server = create_server(MemorySessionStore(AuthSession()), FakeClient)
        result = await server.call_tool("get_notifications", {
            "child_ids": ["child-1"], "offset": 1, "limit": 1,
        })
        assert [item["id"] for item in result.structured_content["items"]] == ["1"]
        invalid = await server.call_tool("get_notifications", {"limit": 0})
        assert invalid.structured_content["error"]["code"] == "invalid_pagination"
        tool = next(t for t in await server.list_tools() if t.name == "get_notifications")
        assert tool.annotations.read_only_hint

    asyncio.run(scenario())


def test_calendar_tool_validates_dates_and_paginates():
    server = create_server(MemorySessionStore(AuthSession()), FakeClient)
    result = asyncio.run(server.call_tool("get_calendar", {
        "start_date": "2026-10-22", "end_date": "2026-10-30", "offset": 1, "limit": 1,
    }))
    assert [item["id"] for item in result.structured_content["items"]] == ["1"]
    assert result.structured_content["timezone"] == "Europe/Stockholm"


def test_childcare_tool_serializes_and_paginates():
    server = create_server(MemorySessionStore(AuthSession()), FakeClient)
    result = asyncio.run(server.call_tool("get_childcare_schedule", {
        "start_date": "2026-10-22", "end_date": "2026-10-30", "offset": 1, "limit": 1,
    }))
    assert len(result.structured_content["items"]) == 1
    assert result.structured_content["items"][0]["date"] == "2026-10-23"


@pytest.mark.parametrize("tool,end", [
    ("get_calendar", "2026-10-21"), ("get_calendar", "2027-10-23"),
    ("get_childcare_schedule", "2026-10-21"), ("get_childcare_schedule", "2026-11-22"),
])
def test_schedule_invalid_range_is_rejected_before_auth(tool, end):
    server = create_server(MemorySessionStore(None), FakeClient)
    result = asyncio.run(server.call_tool(tool, {
        "start_date": "2026-10-22", "end_date": end,
    }))
    assert result.structured_content["error"]["code"] == "invalid_date_range"


def test_rejected_mobile_authorization_is_reported_as_auth_required():
    class ExpiredClient(FakeClient):
        def list_children(self):
            raise AuthenticationExpiredError("Explicit reauthorization is required.")

    server = create_server(MemorySessionStore(AuthSession()), ExpiredClient)
    result = asyncio.run(server.call_tool("list_children", {}))
    assert result.structured_content["error"]["code"] == "authentication_required"


def test_http_errors_never_echo_signed_urls_or_response_details():
    class NetworkFailureClient(FakeClient):
        def list_children(self):
            raise httpx.ConnectError("secret-token in private-url")

    server = create_server(MemorySessionStore(AuthSession()), NetworkFailureClient)
    result = asyncio.run(server.call_tool("list_children", {}))
    assert result.structured_content["error"]["code"] == "infomentor_request_failed"
    assert "secret-token" not in str(result)


def test_corrupt_local_session_does_not_leak_validation_input():
    class BrokenStore(MemorySessionStore):
        def load(self):
            raise ValueError("secret-cookie in malformed local data")

    server = create_server(BrokenStore(None), FakeClient)
    for tool in ("list_children", "auth_status"):
        result = asyncio.run(server.call_tool(tool, {}))
        assert result.structured_content["error"]["code"] == "session_storage_error"
        assert "secret-cookie" not in str(result)
