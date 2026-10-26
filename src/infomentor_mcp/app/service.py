"""The operations shared by the MCP server and the CLI, as safe structured responses."""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import keyring.errors

from infomentor_mcp.auth.mobile import MobileAuthorization
from infomentor_mcp.auth.renewal import Renewal
from infomentor_mcp.changes.store import ChangeStore
from infomentor_mcp.changes.tracker import ChangeTracker
from infomentor_mcp.errors import InfoMentorError, InvalidInput, MobileFlowError
from infomentor_mcp.features.calendar import Calendar
from infomentor_mcp.features.childcare import Childcare, ChildcareWriter
from infomentor_mcp.features.news import News
from infomentor_mcp.features.notifications import Notifications
from infomentor_mcp.features.timetable import Timetable
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.storage.lock import SessionLockTimeout
from infomentor_mcp.storage.session import SessionStore


class Response:
    """JSON-compatible data, or an error that never echoes secrets or upstream details."""

    @staticmethod
    def error(code: str, message: str) -> dict:
        return {"error": {"code": code, "message": message}}

    @classmethod
    def data(cls, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, list):
            return [cls.data(item) for item in value]
        if isinstance(value, dict):
            return {key: cls.data(item) for key, item in value.items()}
        return value

    @classmethod
    def guarded(cls, method: Callable[..., Any]) -> Callable[..., dict]:
        """Decorate a service method so callers always get data or a sanitized error."""

        @functools.wraps(method)
        def wrapper(*args, **kwargs):
            try:
                return cls.data(method(*args, **kwargs))
            except SessionLockTimeout:
                return cls.error("session_busy", "Session is busy; retry shortly.")
            except InfoMentorError as error:
                return error.as_response()
            except httpx.HTTPError:
                return cls.error("infomentor_request_failed", "HTTP/network request failed.")
            except (ValueError, OSError, keyring.errors.KeyringError):
                return cls.error("session_storage_error", "Cannot read or save local session.")

        return wrapper


@dataclass(frozen=True)
class Page:
    offset: int
    limit: int

    def __post_init__(self) -> None:
        if self.offset < 0 or not 1 <= self.limit <= 100:
            raise InvalidInput("Use offset >= 0 and limit 1-100.", code="invalid_pagination")

    def slice(self, items: list) -> list:
        return items[self.offset : self.offset + self.limit]

    @property
    def bounds(self) -> dict[str, int]:
        return {"offset": self.offset, "limit": self.limit}


class InfoMentorService:
    """Local guardian-session operations: fail fast when the session is busy, and renew
    expired Hub sessions through the bound mobile authorization or Stockholm SSO."""

    def __init__(
        self, store: SessionStore | None = None, changes: ChangeStore | None = None,
        hub_factory: Callable[[SessionStore], Hub] | None = None,
    ) -> None:
        self.store = store or SessionStore()
        self.changes = changes or ChangeStore(self.store.state_dir / "changes.sqlite3")
        self.hub_factory = hub_factory or (lambda store: Hub(store, Renewal(store)))
        # The MCP SDK runs sync tools in worker threads. Queue parallel attachment reads
        # instead of returning session_busy to each other.
        self.attachment_queue = threading.Lock()

    @contextmanager
    def hub(self) -> Iterator[Hub]:
        with self.store.locked(timeout=0), self.hub_factory(self.store) as hub:
            yield hub

    @Response.guarded
    def session_status(self) -> dict:
        with self.store.locked(timeout=0):
            session = self.store.load()
        return {
            "authenticated_session_stored": session is not None,
            "mobile_session_bound": bool(session and session.mobile_binding),
            "live_verified": False,
        }

    def revoke_mobile(self, confirm: bool) -> dict:
        if not confirm:
            return Response.error("confirmation_required", "Ask the user first.")
        try:
            with self.store.locked(timeout=0), MobileAuthorization(self.store) as authorization:
                authorization.revoke()
            return {"status": "refresh_credential_removed"}
        except SessionLockTimeout:
            return Response.error("session_busy", "Session is busy; retry shortly.")
        except MobileFlowError as error:
            return error.as_response()
        except Exception:
            # Keyring backends can raise errors with credential-bearing diagnostics.
            return Response.error(
                "mobile_authorization_failed", "Operation failed; details withheld.",
            )

    @Response.guarded
    def list_children(self) -> dict:
        with self.hub() as hub:
            return {"children": hub.run(hub.pupils.discover)}

    @Response.guarded
    def news(
        self, child_ids: list[str] | None = None, offset: int = 0, limit: int = 50,
        include_attachments: bool = True,
    ) -> dict:
        page = Page(offset, limit)
        with self.hub() as hub:
            items = News(hub).items(
                child_ids, offset=offset, limit=limit, include_attachments=include_attachments,
            )
        return {"items": items, **page.bounds}

    @Response.guarded
    def attachment(self, child_id: str, news_id: str, attachment_id: str) -> dict:
        with self.attachment_queue, self.hub() as hub:
            return {"attachment": News(hub).attachment(child_id, news_id, attachment_id)}

    @Response.guarded
    def calendar(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
        offset: int = 0, limit: int = 50,
    ) -> dict:
        page = Page(offset, limit)
        with self.hub() as hub:
            items = Calendar(hub).entries(start_date, end_date, child_ids)
        return {"items": page.slice(items), **page.bounds, **self._period(start_date, end_date)}

    @Response.guarded
    def childcare_schedule(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
        offset: int = 0, limit: int = 100, include_comments: bool = True,
    ) -> dict:
        page = Page(offset, limit)
        with self.hub() as hub:
            items = Childcare(hub).schedule(
                start_date, end_date, child_ids, include_comments=include_comments,
                offset=offset, limit=limit,
            )
        return {"items": items, **page.bounds, **self._period(start_date, end_date)}

    @Response.guarded
    def timetable(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
        offset: int = 0, limit: int = 100,
    ) -> dict:
        page = Page(offset, limit)
        with self.hub() as hub:
            items = Timetable(hub).lessons(start_date, end_date, child_ids)
        return {
            "items": page.slice(items), **page.bounds,
            "total": len(items), "has_more": offset + limit < len(items),
            **self._period(start_date, end_date),
        }

    @Response.guarded
    def notifications(
        self, child_ids: list[str] | None = None, offset: int = 0, limit: int = 50,
    ) -> dict:
        page = Page(offset, limit)
        with self.hub() as hub:
            items = Notifications(hub).items(child_ids)
        return {"items": page.slice(items), **page.bounds}

    @Response.guarded
    def set_childcare_schedule(
        self, child_id: str, dates: list[date], start_time: str | None = None,
        end_time: str | None = None, on_leave: bool = False, confirm: bool = False,
    ) -> dict:
        with self.hub() as hub:
            return ChildcareWriter(hub).set_schedule(
                child_id, dates, start_time=start_time, end_time=end_time,
                on_leave=on_leave, confirm=confirm,
            )

    @Response.guarded
    def set_childcare_comment(
        self, child_id: str, dates: list[date], comment: str, confirm: bool = False,
    ) -> dict:
        with self.hub() as hub:
            return ChildcareWriter(hub).set_comment(child_id, dates, comment, confirm=confirm)

    @Response.guarded
    def sync_changes(
        self, sources: list[str] | None = None, child_ids: list[str] | None = None,
        start_date: date | None = None, end_date: date | None = None,
        include_attachments: bool = True, request_delay_seconds: float = 1.0,
        consumer: str = "default", limit: int = 100,
    ) -> dict:
        with self.hub() as hub:
            return ChangeTracker(hub, self.changes).sync(
                sources, child_ids, start_date, end_date, include_attachments,
                request_delay_seconds, consumer, limit,
            )

    @Response.guarded
    def get_changes(self, consumer: str = "default", limit: int = 100) -> dict:
        return self.changes.get(consumer, limit)

    @Response.guarded
    def ack_changes(self, consumer: str, cursor: str) -> dict:
        return self.changes.ack(consumer, cursor)

    @Response.guarded
    def reset_changes(self) -> dict:
        # Serialize the reset with syncs, which hold the same session lock.
        with self.store.locked():
            return self.changes.reset()

    @staticmethod
    def _period(start_date: date, end_date: date) -> dict[str, str]:
        return {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                "timezone": "Europe/Stockholm"}
