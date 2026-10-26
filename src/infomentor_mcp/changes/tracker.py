"""Fetch complete news/calendar snapshots without an LLM and record their changes."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import httpx

from infomentor_mcp.changes.store import ChangeStore
from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError, InvalidInput
from infomentor_mcp.features.attachments import AttachmentReader
from infomentor_mcp.features.calendar import Calendar
from infomentor_mcp.features.news import News
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.models import STOCKHOLM, DateRange


class RequestPacer:
    """An httpx request hook keeping at least `delay` seconds between requests."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.last: float | None = None

    def __call__(self, request: httpx.Request) -> None:
        if self.last is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()


class ChangeTracker:
    SOURCES = ("news", "calendar")

    def __init__(self, hub: Hub, changes: ChangeStore) -> None:
        self.hub = hub
        self.changes = changes
        self.news = News(hub)
        self.calendar = Calendar(hub)

    def sync(
        self, sources: list[str] | None = None, child_ids: list[str] | None = None,
        start_date: date | None = None, end_date: date | None = None,
        include_attachments: bool = True, request_delay_seconds: float = 1.0,
        consumer: str = "default", limit: int = 100,
    ) -> dict:
        """Fetch, record, and return a page of the consumer's pending changes, including
        unacknowledged changes from earlier syncs."""
        self.changes.check_consumer(consumer, limit)
        with self.hub.store.locked():
            self.hub.refresh()
            self.changes.check_binding(self.hub.transport.mobile_binding)
            start_date = start_date or datetime.now(STOCKHOLM).date()
            end_date = end_date or start_date + timedelta(days=90)
            batch = self.collect(
                list(self.SOURCES) if sources is None else sources, child_ids,
                start_date, end_date, include_attachments, request_delay_seconds,
            )
            result = self.changes.record(batch, include_attachments)
            # Retry outstanding work even when this particular fetch discovered nothing new.
            result.update(self.changes.get(consumer, limit))
            return result

    def collect(
        self, sources: list[str], child_ids: list[str] | None, start_date: date, end_date: date,
        include_attachments: bool = True, request_delay_seconds: float = 1.0,
    ) -> dict:
        """Collect independent, complete child/source snapshots before persisting anything."""
        if not sources or set(sources) - set(self.SOURCES):
            raise InvalidInput("Choose news and/or calendar.")
        period = DateRange.checked(start_date, end_date, Calendar.MAX_DAYS)
        if not 0 <= request_delay_seconds <= 60:
            raise InvalidInput("Request delay must be between 0 and 60 seconds.")
        pacer = RequestPacer(request_delay_seconds)
        return self.hub.run(
            lambda: self._snapshots(sources, child_ids, period, include_attachments, pacer)
        )

    def _snapshots(self, sources, child_ids, period, include_attachments, pacer) -> dict:
        snapshots, errors, attachment_checks = [], [], []
        # Attach to the current connection pool on every replay after renewal.
        pupils = self.hub.pupils
        with self.hub.transport.request_hook(pacer), pupils.visiting(child_ids) as children:
            for child in children:
                pupils.select(child.id)
                for source in dict.fromkeys(sources):
                    try:
                        items = self._read(child.id, source, period, include_attachments)
                    except AuthenticationExpiredError:
                        raise
                    except (InfoMentorError, httpx.HTTPError):
                        errors.append({"child_id": child.id, "source": source,
                                       "code": "fetch_failed"})
                        continue
                    snapshots.append({
                        "child_id": child.id, "source": source,
                        "items": [item.model_dump(mode="json") for item in items],
                    })
                    if source == "news" and include_attachments:
                        attachment_checks += [{
                            "child_id": child.id, "news_id": item.id,
                            "attachment_id": attachment.id, "status": attachment.status,
                            "content_checked": bool(attachment.content_sha256),
                        } for item in items for attachment in item.attachments]
        return {"snapshots": snapshots, "errors": errors,
                "binding": self.hub.transport.mobile_binding,
                "attachment_checks": attachment_checks}

    def _read(self, child_id, source, period, include_attachments) -> list:
        if source == "calendar":
            return self.calendar.read_selected(child_id, period)
        items = self.news.read_selected(child_id)
        if include_attachments:
            AttachmentReader(self.hub.transport).enrich(items)
        return items
