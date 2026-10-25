"""Reusable Swedish InfoMentor Hub client."""

from __future__ import annotations

import hashlib
import html
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
import keyring.errors

from infomentor_mcp.auth.session import AuthSession, SessionStore
from infomentor_mcp.client.attachments import (
    MAX_ATTACHMENTS,
    MAX_FILE_BYTES,
    MAX_TOTAL_TEXT_CHARS,
    extract_text,
    parse_attachments,
)
from infomentor_mcp.client.http import (
    HUB_ORIGIN,
    AuthenticationExpiredError,
    InfoMentorError,
    InfoMentorHttpClient,
)
from infomentor_mcp.client.models import (
    Attachment,
    CalendarEntry,
    Child,
    ChildcareComment,
    ChildcareDay,
    NewsItem,
    Notification,
    TimetableLesson,
)

_PUPIL_SWITCH_PATH = re.compile(r"/Account/PupilSwitcher/SwitchPupil/([^/?#'\"\\]+)", re.I)
_SELECTED_PUPIL_NAME = re.compile(r"selectedPupilName\s*:\s*'((?:\\.|[^'])*)'", re.I)
MAX_CALENDAR_DAYS = 366
MAX_CHILDCARE_DAYS = 31
MAX_TIMETABLE_DAYS = 31


class UnexpectedInfoMentorResponse(InfoMentorError):
    """Hub responded successfully but not in the documented shape."""


class ChildSession:
    """Verified targets in a locked operation; switching never restores between children."""

    def __init__(self, client, children, child_ids):
        self.client = client
        self.linked_ids = {child.id for child in children}
        self.targets = ([client._find_child(children, id_) for id_ in dict.fromkeys(child_ids)]
                        if child_ids is not None else children)

    def select(self, child):
        if child not in self.targets:
            raise ValueError("Child is not in this session's targets.")
        self.client._select_child(child.id, self.linked_ids)


def parse_child_ids(page: str) -> list[str]:
    """Extract linked pupil IDs from Hub's generated switcher routes."""
    return list(dict.fromkeys(match.group(1) for match in _PUPIL_SWITCH_PATH.finditer(page)))


def _selected_name(page: str) -> str | None:
    match = _SELECTED_PUPIL_NAME.search(page)
    if match is None:
        return None
    return html.unescape(match.group(1).replace("\\'", "'").replace("\\\\", "\\"))


def _same_name(first: str, second: str | None) -> bool:
    if second is None:
        return False
    normalized_first = " ".join(first.lower().replace(",", " ").split())
    normalized_second = " ".join(second.lower().replace(",", " ").split())
    return normalized_first == normalized_second


class InfoMentorClient:
    """A session-bound client for school reads and explicit childcare updates."""

    def __init__(
        self,
        session: AuthSession,
        session_store: SessionStore | None = None,
        operation_lock: threading.RLock | None = None,
    ) -> None:
        self._http = InfoMentorHttpClient(session)
        self._session_store = session_store
        self._lock_store = session_store or SessionStore()
        self._operation_lock = operation_lock or threading.RLock()
        self._expected_names: dict[str, str] = {}
        self._original_child_id: str | None = None

    def close(self) -> None:
        self._http.close()

    @contextmanager
    def _child_session(self, child_ids=None):
        with self._operation_lock, self._lock_store.locked():
            children = self._list_children()
            selected = next(child for child in children if child.selected)
            try:
                yield ChildSession(self, children, child_ids)
            finally:
                self._select_child(selected.id, {child.id for child in children})

    def collect_changes(self, sources, child_ids, start_date, end_date,
                        include_attachments=True, request_delay_seconds=1.0):
        """Collect independent, complete child/source snapshots before persisting any changes."""
        if not sources or set(sources) - {"news", "calendar"}:
            raise ValueError("Choose news and/or calendar.")
        if not 0 <= (end_date - start_date).days < MAX_CALENDAR_DAYS:
            raise ValueError("Use an inclusive calendar range of 1-366 days.")
        if not 0 <= request_delay_seconds <= 60:
            raise ValueError("Request delay must be between 0 and 60 seconds.")
        last_request = None

        def pace(request):
            nonlocal last_request
            if last_request is not None:
                time.sleep(max(0, request_delay_seconds - (time.monotonic() - last_request)))
            last_request = time.monotonic()

        def fetch():
            snapshots, errors, attachment_checks = [], [], []
            # Attach to the current HTTP client on every replay after renewal.
            hooks = self._http.client.event_hooks["request"]
            hooks.append(pace)
            try:
                with self._child_session(child_ids) as children:
                    for child in children.targets:
                        children.select(child)
                        for source in dict.fromkeys(sources):
                            try:
                                if source == "news":
                                    items = self._get_selected_child_news(child.id)
                                    if include_attachments:
                                        self._enrich_news(items)
                                        for item in items:
                                            for attachment in item.attachments:
                                                attachment_checks.append({
                                                    "child_id": child.id, "news_id": item.id,
                                                    "attachment_id": attachment.id,
                                                    "status": attachment.status,
                                                    "content_checked": bool(
                                                        attachment.content_sha256,
                                                    ),
                                                })
                                else:
                                    items = self._get_selected_child_calendar(
                                        child.id, start_date, end_date,
                                    )
                                snapshots.append({
                                    "child_id": child.id, "source": source,
                                    "items": [i.model_dump(mode="json") for i in items],
                                })
                            except AuthenticationExpiredError:
                                raise
                            except (InfoMentorError, httpx.HTTPError):
                                errors.append({"child_id": child.id, "source": source,
                                               "code": "fetch_failed"})
                return {"snapshots": snapshots, "errors": errors,
                        "binding": self._http.mobile_binding,
                        "attachment_checks": attachment_checks}
            finally:
                hooks.remove(pace)

        return self._with_session_renewal(fetch)

    def _enrich_news(self, items, cache=None, remaining=MAX_TOTAL_TEXT_CHARS):
        cache = {} if cache is None else cache
        for item in items:
            for index, attachment in enumerate(item.attachments):
                if attachment.status != "available":
                    continue
                if remaining <= 0 or (attachment.id not in cache and len(cache) >= MAX_ATTACHMENTS):
                    result = attachment.model_copy(update={"status": "limit_reached"})
                elif attachment.id in cache:
                    result = cache[attachment.id].model_copy(
                        deep=True, update={"name": attachment.name},
                    )
                else:
                    result = self._read_attachment(item.id, attachment)
                    cache[attachment.id] = result.model_copy(deep=True)
                if result.text:
                    result.truncated |= len(result.text) > remaining
                    result.text = result.text[:remaining]
                    remaining -= len(result.text)
                item.attachments[index] = result
        return remaining

    def list_children(self) -> list[Child]:
        return self._with_session_renewal(self._list_children)

    def _list_children(self) -> list[Child]:
        self._expected_names = {}
        page = self._get_hub_page("/")
        child_ids = parse_child_ids(page)
        initial_name = _selected_name(page)
        if not child_ids or initial_name is None:
            raise UnexpectedInfoMentorResponse(
                "Hub page did not contain linked pupil switchers and a selected pupil name."
            )
        child_id_set = set(child_ids)
        children: list[Child] = []
        original_id: str | None = None
        last_selected_id: str | None = None
        try:
            for child_id in child_ids:
                selected_page = self._select_child(child_id, child_id_set)
                name = _selected_name(selected_page)
                if name is None:
                    raise UnexpectedInfoMentorResponse(
                        "Hub did not expose the selected pupil name after switching pupil."
                    )
                is_selected = _same_name(name, initial_name)
                if any(_same_name(name, child.name) for child in children):
                    raise UnexpectedInfoMentorResponse(
                        "Pupil selection is ambiguous or the switch was ignored; "
                        "no school data read."
                    )
                if is_selected:
                    original_id = child_id
                    self._original_child_id = child_id
                children.append(Child(id=child_id, name=name, selected=is_selected))
                self._expected_names[child_id] = name
                last_selected_id = child_id
            if original_id is None:
                raise UnexpectedInfoMentorResponse(
                    "Could not identify the original pupil selection; no school data read."
                )
        finally:
            if original_id and last_selected_id != original_id:
                self._select_child(original_id, child_id_set)
        return children

    def get_news(
        self, child_ids: list[str] | None = None, *, offset: int = 0, limit: int | None = None,
        include_attachments: bool = True,
    ) -> list[NewsItem]:
        """Get unique news, newest first, for selected children or all linked children."""
        return self._with_session_renewal(
            lambda: self._get_news(child_ids, offset, limit, include_attachments)
        )

    def _get_news(
        self, child_ids: list[str] | None, offset: int = 0, limit: int | None = None,
        include_attachments: bool = False,
    ) -> list[NewsItem]:
        items: dict[str, NewsItem] = {}
        with self._child_session(child_ids) as children:
            for child in children.targets:
                children.select(child)
                for item in self._get_selected_child_news(child.id):
                    if item.id not in items:
                        items[item.id] = item
                    elif child.id not in items[item.id].child_ids:
                        items[item.id].child_ids.append(child.id)
            page = sorted(items.values(), key=lambda item: item.published_at or "", reverse=True)
            page = page[offset : offset + limit if limit is not None else None]
            if include_attachments:
                cache: dict[str, Attachment] = {}
                remaining = MAX_TOTAL_TEXT_CHARS
                for item in page:
                    if not item.attachments:
                        continue
                    self._select_child(item.child_ids[0], children.linked_ids)
                    remaining = self._enrich_news([item], cache, remaining)
            return page

    def get_attachments(self, child_id: str, news_id: str, attachment_id: str) -> Attachment:
        """Read one attachment rediscovered in a linked child's current news feed."""
        def fetch():
            items = self._get_news([child_id])
            item = next((item for item in items if item.id == news_id), None)
            attachment = (
                next((a for a in item.attachments if a.id == attachment_id), None) if item else None
            )
            if attachment is None or attachment.status != "available":
                raise InfoMentorError("Attachment was not found in the requested child's news.")
            try:
                self._select_child(child_id, set(self._expected_names))
                return self._read_attachment(news_id, attachment)
            finally:
                if self._original_child_id:
                    self._select_child(self._original_child_id, set(self._expected_names))

        return self._with_session_renewal(fetch)

    def get_childcare_schedule(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
        *, include_comments: bool = True, offset: int = 0, limit: int | None = None,
    ) -> list[ChildcareDay]:
        """Read submitted before/after-school and holiday care times, without editing."""
        if not 0 <= (end_date - start_date).days < MAX_CHILDCARE_DAYS:
            raise ValueError("Use end_date >= start_date and at most 31 calendar days.")
        return self._with_session_renewal(
            lambda: self._get_childcare_schedule(
                start_date, end_date, child_ids, include_comments, offset, limit
            )
        )

    def set_childcare_schedule(
        self, child_id: str, dates: list[date], *, start_time: str | None = None,
        end_time: str | None = None, on_leave: bool = False, confirm: bool = False,
    ) -> dict:
        from infomentor_mcp.client.childcare_writes import update_childcare

        return update_childcare(
            self, child_id, dates, start_time=start_time, end_time=end_time,
            on_leave=on_leave, confirm=confirm,
        )

    def set_childcare_comment(
        self, child_id: str, dates: list[date], comment: str, *, confirm: bool = False,
    ) -> dict:
        from infomentor_mcp.client.childcare_writes import update_childcare

        return update_childcare(self, child_id, dates, comment=comment, confirm=confirm)

    def _get_childcare_schedule(
        self, start_date: date, end_date: date, child_ids: list[str] | None,
        include_comments: bool = True, offset: int = 0, limit: int | None = None,
    ) -> list[ChildcareDay]:
        children = self._list_children()
        selected = next((child for child in children if child.selected), None)
        targets = (
            [self._find_child(children, id_) for id_ in dict.fromkeys(child_ids)]
            if child_ids is not None else children
        )
        linked_ids = {child.id for child in children}
        items: dict[tuple[str, date], ChildcareDay] = {}
        first_week = start_date - timedelta(days=start_date.weekday())
        try:
            for child in targets:
                self._select_child(child.id, linked_ids)
                week = first_week
                while week <= end_date:
                    response = self._http.hub_post(
                        "/TimeRegistration/TimeRegistration/GetTimeRegistrations/", {
                            "date": week.isoformat() + "T00:00:00.000Z",
                            "showNextWeekIfNoMoreSchoolDays": False,
                        },
                    )
                    try:
                        payload = response.json()
                        rows = payload["days"]
                        week_start = _stockholm_datetime(payload["startDate"]).date()
                        week_end = _stockholm_datetime(payload["endDate"]).date()
                        if (
                            not isinstance(rows, list) or week_start != week
                            or not week <= week_end <= week + timedelta(days=6)
                        ):
                            raise ValueError("Unexpected week")
                        for row in rows:
                            day = self._parse_childcare_day(child.id, row)
                            if not week_start <= day.date <= week_end:
                                raise ValueError("Day outside returned week")
                            if not start_date <= day.date <= end_date:
                                continue
                            key = (child.id, day.date)
                            if key in items:
                                raise ValueError("Duplicate childcare day")
                            items[key] = day
                    except (ValueError, KeyError, TypeError):
                        raise UnexpectedInfoMentorResponse(
                            "Childcare days were invalid or did not match the requested week."
                        ) from None
                    week += timedelta(days=7)
            page = sorted(items.values(), key=lambda item: (item.date, item.child_id))
            page = page[offset : offset + limit if limit is not None else None]
            if include_comments:
                for day in page:
                    if not (day.has_comments or day.has_unread_comments):
                        day.comments_status = "not_present"
                for child in targets:
                    days = [day for day in page if day.child_id == child.id
                            and (day.has_comments or day.has_unread_comments)]
                    if days:
                        self._select_child(child.id, linked_ids)
                        for day in days:
                            self._get_childcare_comments(day)
            return page
        finally:
            if selected:
                self._select_child(selected.id, linked_ids)

    @staticmethod
    def _parse_childcare_day(child_id: str, row: object) -> ChildcareDay:
        try:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(key), bool) for key in ("onLeave", "isSchoolClosed")
            ):
                raise ValueError("Missing childcare flags")
            day = _stockholm_datetime(row["date"]).date()
            times = {}
            for source, target in (
                ("startDateTime", "starts_at"), ("endDateTime", "ends_at"),
                ("schoolOpeningTime", "opens_at"), ("schoolClosingTime", "closes_at"),
            ):
                value = row.get(source)
                times[target] = None if value is None or value == "" else _stockholm_datetime(value)
                if times[target] is not None and times[target].date() != day:
                    raise ValueError("Time did not match childcare day")
            for start, end in (("starts_at", "ends_at"), ("opens_at", "closes_at")):
                if (
                    times[start] and times[end]
                    and times[end].timestamp() < times[start].timestamp()
                ):
                    raise ValueError("Reversed childcare times")
            id_ = row.get("timeRegistrationId")
            if isinstance(id_, bool) or id_ is not None and not isinstance(id_, (str, int)):
                raise ValueError("Invalid registration ID")
            return ChildcareDay(
                child_id=child_id, date=day, registration_id=str(id_) if id_ is not None else None,
                **times, on_leave=row["onLeave"], is_closed=row["isSchoolClosed"],
                closed_reason=_string_or_none(row.get("schoolClosedReason")),
                is_locked=row.get("isLocked"), has_comments=row.get("hasComments"),
                has_unread_comments=row.get("hasUnreadComments"),
            )
        except (ValueError, TypeError, KeyError):
            raise UnexpectedInfoMentorResponse(
                "Childcare day had invalid dates, times or flags; values withheld."
            ) from None

    def _get_childcare_comments(self, day: ChildcareDay) -> None:
        # Hub marks comments read as part of this request, not via a separate write.
        day.comments_marked_read = None  # An interrupted/invalid response has an unknown outcome.
        try:
            response = self._http.hub_post("/TimeRegistration/TimeRegistration/GetComments/", {
                "date": day.date.isoformat() + "T00:00:00",
            })
            payload = response.json()
            comments = []
            for prefix, kind, name in (("teacher", "staff", "teacherName"),
                                       ("user", "guardian", "userName")):
                text = payload[prefix + "Comment"]
                if text is not None and not isinstance(text, str):
                    raise ValueError("Invalid comment text")
                if text:
                    sent = payload.get(prefix + "CommentDate")
                    comments.append(ChildcareComment(
                        author_type=kind, author=_string_or_none(payload.get(name)), text=text,
                        sent_at=_stockholm_datetime(sent) if sent else None,
                    ))
            day.comments = comments
            day.comments_status = "loaded"
            day.comments_marked_read = True
            day.has_unread_comments = False
        except AuthenticationExpiredError:
            raise  # Reuse the existing guarded session-renewal/replay path.
        except (InfoMentorError, httpx.HTTPError):
            day.comments_status = "fetch_failed"
        except (ValueError, KeyError, TypeError, AttributeError):
            day.comments_status = "invalid_response"

    def get_timetable(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
    ) -> list[TimetableLesson]:
        """Read lesson/activity times, not childcare registrations or actual attendance."""
        if not 0 <= (end_date - start_date).days < MAX_TIMETABLE_DAYS:
            raise ValueError("Use end_date >= start_date and at most 31 calendar days.")
        return self._with_session_renewal(
            lambda: self._get_timetable(start_date, end_date, child_ids)
        )

    def _get_timetable(
        self, start_date: date, end_date: date, child_ids: list[str] | None,
    ) -> list[TimetableLesson]:
        children = self._list_children()
        selected = next((child for child in children if child.selected), None)
        targets = (
            [self._find_child(children, id_) for id_ in dict.fromkeys(child_ids)]
            if child_ids is not None else children
        )
        linked_ids = {child.id for child in children}
        items = []
        # Match the web client's current browser offset (JS getTimezoneOffset sign).
        # Response timestamps are Stockholm wall times, resolved individually below.
        offset = datetime.now(ZoneInfo("Europe/Stockholm")).utcoffset()
        payload = {
            "start": start_date.isoformat(), "end": (end_date + timedelta(days=1)).isoformat(),
            "UTCOffset": str(-int(offset.total_seconds() // 60)),
        }
        try:
            for child in targets:
                self._select_child(child.id, linked_ids)
                response = self._http.hub_post(
                    "/timetable/timetable/gettimetablelist", payload, form=True,
                )
                try:
                    rows = response.json()
                    if not isinstance(rows, list):
                        raise ValueError("Not a lesson list")
                    for row in rows:
                        lesson = self._parse_timetable_lesson(child.id, row)
                        if not start_date <= lesson.starts_at.date() <= end_date:
                            raise ValueError("Lesson outside requested dates")
                        items.append(lesson)
                except (ValueError, KeyError, TypeError):
                    raise UnexpectedInfoMentorResponse(
                        "Timetable response had invalid lessons or unexpected dates."
                    ) from None
            return sorted(items, key=lambda item: (
                item.starts_at, item.child_id, item.ends_at, item.title,
            ))
        finally:
            if selected:
                self._select_child(selected.id, linked_ids)

    @staticmethod
    def _parse_timetable_lesson(child_id: str, row: object) -> TimetableLesson:
        try:
            if (
                not isinstance(row, dict) or not isinstance(row.get("title"), str)
                or not isinstance(row.get("allDay"), bool)
            ):
                raise ValueError("Missing lesson fields")
            start = _stockholm_datetime(row["start"])
            end = _stockholm_datetime(row["end"])
            if end.timestamp() < start.timestamp():
                raise ValueError("Reversed lesson times")
            notes = row.get("notes")
            if notes is None:
                notes = {}
            if not isinstance(notes, dict):
                raise ValueError("Invalid lesson notes")
            for value in (
                row.get("details"), row.get("establishmentName"),
                notes.get("roomInfo"), notes.get("tutors"), notes.get("timetableNotes"),
            ):
                if value is not None and not isinstance(value, str):
                    raise ValueError("Invalid lesson detail")
            return TimetableLesson(
                child_id=child_id, title=row["title"], starts_at=start, ends_at=end,
                all_day=row["allDay"], room=notes.get("roomInfo") or None,
                teachers=notes.get("tutors") or None, notes=notes.get("timetableNotes") or None,
                details=row.get("details") or None,
                establishment=row.get("establishmentName") or None,
            )
        except (ValueError, TypeError, KeyError):
            raise UnexpectedInfoMentorResponse(
                "Timetable lesson had invalid fields; values withheld."
            ) from None

    def get_calendar(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
    ) -> list[CalendarEntry]:
        """Get events overlapping an inclusive Stockholm date range for linked children."""
        if not 0 <= (end_date - start_date).days < MAX_CALENDAR_DAYS:
            raise ValueError("Use end_date >= start_date and at most 366 calendar days.")
        return self._with_session_renewal(
            lambda: self._get_calendar(start_date, end_date, child_ids)
        )

    def _get_calendar(
        self, start_date: date, end_date: date, child_ids: list[str] | None,
    ) -> list[CalendarEntry]:
        items: dict[str, CalendarEntry] = {}
        with self._child_session(child_ids) as children:
            for child in children.targets:
                children.select(child)
                for entry in self._get_selected_child_calendar(child.id, start_date, end_date):
                    if entry.id not in items:
                        items[entry.id] = entry
                    elif child.id not in items[entry.id].child_ids:
                        items[entry.id].child_ids.append(child.id)
            return sorted(items.values(), key=lambda item: (item.starts_at, item.id))

    def _get_selected_child_calendar(self, child_id, start_date, end_date):
        response = self._http.hub_post("/calendarv2/calendarv2/getentries", {
            "startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
        })
        try:
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Not a list")
        except ValueError:
            raise UnexpectedInfoMentorResponse(
                "Calendar response did not contain an event list."
            ) from None
        entries = [self._parse_calendar_entry(child_id, row) for row in rows]
        return [entry for entry in entries if entry.starts_at.date() <= end_date
                and entry.ends_at.date() >= start_date]

    @staticmethod
    def _parse_calendar_entry(child_id: str, row: object) -> CalendarEntry:
        try:
            if not isinstance(row, dict):
                raise ValueError("Not an event")
            id_ = row["id"]
            if isinstance(id_, bool) or not isinstance(id_, (int, str)) or not str(id_).strip():
                raise ValueError("Missing identity")
            if not isinstance(row["isAllDayEvent"], bool):
                raise ValueError("Missing all-day flag")
            start = _stockholm_datetime(row["startDateFull"])
            end = _stockholm_datetime(row["endDateFull"])
            if end.timestamp() < start.timestamp():
                raise ValueError("Reversed event dates")
            return CalendarEntry(
                id=str(id_), child_ids=[child_id], title=_string_or_none(row.get("title")),
                description=(
                    _string_or_none(row.get("text")) or _string_or_none(row.get("description"))
                ),
                starts_at=start, ends_at=end, all_day=row["isAllDayEvent"],
                has_attachments=row.get("hasAttachments") is True,
            )
        except (ValueError, TypeError, KeyError):
            raise UnexpectedInfoMentorResponse(
                "Calendar event had an invalid identity, date or all-day flag; values withheld."
            ) from None

    def get_notifications(self, child_ids: list[str] | None = None) -> list[Notification]:
        """Read pupil-linked notifications without marking them seen/read or following links."""
        return self._with_session_renewal(lambda: self._get_notifications(child_ids))

    def _get_notifications(self, child_ids: list[str] | None) -> list[Notification]:
        children = self._list_children()
        selected = next((child for child in children if child.selected), None)
        targets = (
            [self._find_child(children, id_) for id_ in dict.fromkeys(child_ids)]
            if child_ids is not None else children
        )
        linked_ids = {child.id for child in children}
        items: dict[str, Notification] = {}
        owners: dict[tuple[str, str], str] = {}
        try:
            for child in targets:
                self._select_child(child.id, linked_ids)
                response = self._http.hub_post(
                    "/NotificationApp/NotificationApp/GetNotifications", {}
                )
                try:
                    payload = response.json()
                    rows = payload["notifications"]
                    if not isinstance(rows, list):
                        raise ValueError("Not a list")
                except (ValueError, KeyError, TypeError):
                    raise UnexpectedInfoMentorResponse(
                        "Notification response did not contain a notifications list."
                    ) from None
                selected_identity = None
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(
                        row.get("currentlySelectedPupil"), bool
                    ):
                        raise UnexpectedInfoMentorResponse(
                            "Notification response did not identify selected-pupil records."
                        )
                    if not row["currentlySelectedPupil"]:
                        continue
                    # These upstream pupil IDs differ from the switcher IDs. Do not guess
                    # a mapping; bind selected records to the verified selection instead.
                    identity = (row.get("pupilIM2Id"), row.get("pupilSourceId"))
                    if any(isinstance(v, bool) or not isinstance(v, (int, str))
                           or not str(v).strip() for v in (*identity, row.get("id"))):
                        raise UnexpectedInfoMentorResponse("Notification identity was missing.")
                    identity = tuple(str(v) for v in identity)
                    if (
                        selected_identity is not None and selected_identity != identity
                        or identity in owners and owners[identity] != child.id
                    ):
                        raise UnexpectedInfoMentorResponse(
                            "Notification pupil selection was ambiguous; no notifications returned."
                        )
                    owners[identity] = child.id
                    selected_identity = identity
                    id_ = str(row["id"])
                    state = _string_or_none(row.get("state"))
                    if id_ in items:
                        if child.id not in items[id_].child_ids:
                            items[id_].child_ids.append(child.id)
                        items[id_].states[child.id] = state
                    else:
                        items[id_] = Notification(
                            id=id_, child_ids=[child.id], states={child.id: state},
                            title=_string_or_none(row.get("title")),
                            subtitle=_string_or_none(row.get("subTitle")),
                            subjects_courses=_string_or_none(row.get("subjectsCourses")),
                            sent_at=_string_or_none(row.get("dateSent")),
                            order_date=_string_or_none(row.get("orderDate")),
                            app_type=_string_or_none(row.get("appType")),
                            notification_type=_string_or_none(row.get("type")),
                            source_reference=_string_or_none(row.get("url")),
                        )
            return sorted(
                items.values(), key=lambda item: item.order_date or item.sent_at or "", reverse=True
            )
        finally:
            if selected:
                self._select_child(selected.id, linked_ids)

    def _read_attachment(self, news_id: str, attachment: Attachment) -> Attachment:
        result = attachment.model_copy(deep=True)
        if not (result.name or "").lower().endswith((".pdf", ".docx")):
            result.status = "unsupported_type"
            return result
        # Construct the URL from rediscovered IDs, never accept a caller-supplied URL.
        path = f"/Resources/Resource/Download/{result.id}"
        try:
            with self._http.client.stream(
                "GET", HUB_ORIGIN + path,
                params={"api": "IM2", "ModuleType": "NewsItem", "ConnectionId": news_id},
            ) as response:
                if response.status_code == 401 or response.is_redirect:
                    raise AuthenticationExpiredError("Attachment request requires authentication.")
                response.raise_for_status()
                if int(response.headers.get("content-length", "0")) > MAX_FILE_BYTES:
                    result.status = "too_large"
                    return result
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > MAX_FILE_BYTES:
                        result.status = "too_large"
                        return result
            result.content_sha256 = hashlib.sha256(content).hexdigest()
            return extract_text(result, bytes(content))
        except (httpx.HTTPError, ValueError):
            result.status = "fetch_failed"
            return result

    def _with_session_renewal(self, operation, *, preserve_result_on_storage_error: bool = False):
        process_lock = self._lock_store.locked()
        with self._operation_lock, process_lock:
            self._original_child_id = None
            if self._session_store:
                latest = self._session_store.load()
                if latest is None:
                    raise AuthenticationExpiredError("No saved session; run auth login again.")
                saved_cookies = sorted(
                    (cookie.domain, cookie.path, cookie.name, cookie.value,
                     cookie.secure, cookie.expires)
                    for cookie in latest.cookies
                )
                active_cookies = sorted(
                    (cookie.domain, cookie.path, cookie.name, cookie.value,
                     cookie.secure, cookie.expires)
                    for cookie in self._http.client.cookies.jar
                )
                if (
                    saved_cookies != active_cookies
                    or latest.mobile_binding != self._http.mobile_binding
                ):
                    # Another cooperating process may have renewed the session
                    # on a different backend. Do not reuse this old TCP pool.
                    previous_http = self._http
                    self._http = InfoMentorHttpClient(latest)
                    previous_http.close()
            persist_current = True
            operation_completed = False
            try:
                try:
                    result = operation()
                except AuthenticationExpiredError:
                    # A renewal may persist newer credentials/session before a
                    # later step fails. Never overwrite those with this old jar.
                    persist_current = False
                    self._renew_session()
                    persist_current = True
                    result = operation()
                operation_completed = True
            finally:
                if self._session_store and persist_current:
                    try:
                        self._session_store.save(self._http.session())
                    except (OSError, ValueError, keyring.errors.KeyringError):
                        if not preserve_result_on_storage_error or not operation_completed:
                            raise
                        # A local persistence failure does not undo remote changes.
                        # Keep verified/unknown/per-day outcomes so callers do not blindly retry.
                        result.setdefault("warnings", []).append({
                            "code": "session_storage_error",
                            "message": "Could not save the local session. Operation results are "
                            "retained; do not repeat writes just because of this warning. "
                            "Read current state before retrying uncertain changes. "
                            "Reauthentication may be needed for subsequent requests.",
                        })
            return result

    def _renew_session(self) -> None:
        if self._session_store is None:
            raise AuthenticationExpiredError("InfoMentor session expired; run auth login again.")

        if self._http.mobile_binding is not None:
            from infomentor_mcp.auth.mobile_pairing import MobilePairing

            flow = MobilePairing(self._session_store)
            try:
                renewed_session = flow.renew()
            except (ValueError, OSError):
                raise InfoMentorError(
                    "Cannot read or persist mobile authorization; details withheld."
                ) from None
            finally:
                flow.close()
            previous_http = self._http
            self._http = InfoMentorHttpClient(renewed_session)
            previous_http.close()
            if self._original_child_id is not None:
                self._select_child(self._original_child_id, set(self._expected_names))
            return

        from infomentor_mcp.auth.saml import SamlSession

        renewal_http = InfoMentorHttpClient(self._http.session().stockholm_sso_only())
        try:
            renewed = SamlSession(
                renewal_http, emit_diagnostics=False
            ).resume_from_stockholm_sso()
            if not renewed:
                raise AuthenticationExpiredError(
                    "InfoMentor and Stockholm SSO sessions have expired; run auth login again."
                )
            renewal_http.verify_authenticated_read()
            renewed_session = renewal_http.session()
            self._session_store.save(renewed_session)
            previous_http = self._http
            self._http = InfoMentorHttpClient(renewed_session)
            previous_http.close()
            if self._original_child_id is not None:
                self._select_child(self._original_child_id, set(self._expected_names))
        finally:
            renewal_http.close()

    def _get_hub_page(self, path: str) -> str:
        response = self._http.client.get(f"{HUB_ORIGIN}{path}", follow_redirects=False)
        if response.status_code == 403:
            raise InfoMentorError("Hub access denied (HTTP 403); no automatic renewal attempted.")
        if response.is_redirect or response.status_code == 401:
            raise AuthenticationExpiredError(
                "InfoMentor session has expired; run auth login again."
            )
        response.raise_for_status()
        if not response.content.strip():
            raise AuthenticationExpiredError("Hub returned an empty successful response.")
        return response.text

    def _select_child(self, child_id: str, known_child_ids: set[str]) -> str:
        if child_id not in known_child_ids:
            raise ValueError("The requested child is not linked to this InfoMentor account.")
        path = f"/Account/PupilSwitcher/SwitchPupil/{quote(child_id, safe='')}"
        destination = f"{HUB_ORIGIN}{path}"
        for _ in range(6):
            response = self._http.client.get(destination, follow_redirects=False)
            if response.status_code == 403:
                raise InfoMentorError("Pupil switch denied (HTTP 403); no renewal attempted.")
            if response.status_code == 401:
                raise AuthenticationExpiredError("Session expired while changing selected child.")
            if not response.is_redirect:
                break
            location = response.headers.get("location")
            if not location:
                raise UnexpectedInfoMentorResponse("Pupil switch redirect has no destination.")
            destination = urljoin(str(response.url), location)
            parsed = urlparse(destination)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "hub.infomentor.se"
                or "login" in parsed.path.lower()
            ):
                raise AuthenticationExpiredError("Session expired while changing selected child.")
        else:
            raise UnexpectedInfoMentorResponse("Pupil switch exceeded its redirect limit.")
        response.raise_for_status()
        if not response.content:
            raise UnexpectedInfoMentorResponse("Hub returned an empty page after switching pupil.")
        expected_name = self._expected_names.get(child_id)
        if expected_name is not None and not _same_name(
            _selected_name(response.text) or "", expected_name
        ):
            raise UnexpectedInfoMentorResponse(
                "Hub did not confirm the requested pupil after switching; no school data returned."
            )
        return response.text

    def _get_selected_child_news(self, child_id: str) -> list[NewsItem]:
        response = self._http.hub_post("/Communication/News/GetNewsList", {})
        try:
            payload = response.json()
            source_items = payload["items"]
        except (KeyError, TypeError, ValueError) as error:
            raise UnexpectedInfoMentorResponse(
                "News response did not contain an items list."
            ) from error
        if not isinstance(source_items, list):
            raise UnexpectedInfoMentorResponse("News response items was not a list.")
        if any(not isinstance(item, dict) for item in source_items):
            raise UnexpectedInfoMentorResponse("News response contained invalid items.")
        return [
            self._parse_news_item(child_id, item)
            for item in source_items
        ]

    @staticmethod
    def _find_child(children: list[Child], child_id: str | None) -> Child:
        if child_id is None:
            raise ValueError("A child ID is required for this operation.")
        child = next((item for item in children if item.id == child_id), None)
        if child is None:
            raise ValueError("The requested child is not linked to this InfoMentor account.")
        return child

    @staticmethod
    def _parse_news_item(child_id: str, item: dict[str, object]) -> NewsItem:
        if item.get("id") is None:
            raise UnexpectedInfoMentorResponse("News item did not contain an ID.")
        item_id = str(item["id"])
        return NewsItem(
            id=item_id,
            child_ids=[child_id],
            title=_string_or_none(item.get("title")),
            content=_string_or_none(item.get("content")),
            published_at=_string_or_none(item.get("publishedDate")),
            source_reference=f"/#/communication/news/{item_id}",
            attachments=parse_attachments(item_id, item.get("attachments")),
            raw=item,
        )


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _stockholm_datetime(value: object) -> datetime:
    """Interpret verified Hub wall-clock timestamps in Stockholm, preserving aware instants."""
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp")
    parsed = datetime.fromisoformat(value)
    timezone = ZoneInfo("Europe/Stockholm")
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)
