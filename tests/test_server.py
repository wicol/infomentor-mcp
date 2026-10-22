import asyncio
from contextlib import nullcontext

import httpx

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.http import AuthenticationExpiredError
from infomentor_mcp.client.models import Child, NewsItem
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
        "auth_status", "auth_start", "auth_progress", "auth_cancel", "auth_pair", "auth_revoke",
        "auth_wait", "get_news", "get_attachments", "list_children",
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
