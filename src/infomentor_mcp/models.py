"""Normalized school-data models, parsed fail-closed from Hub responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as Date
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from infomentor_mcp.errors import InvalidInput, UnexpectedInfoMentorResponse

STOCKHOLM = ZoneInfo("Europe/Stockholm")


def stockholm_datetime(value: object) -> datetime:
    """Interpret verified Hub wall-clock timestamps in Stockholm, preserving aware instants."""
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=STOCKHOLM)
    return parsed.astimezone(STOCKHOLM)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class DateRange:
    """An inclusive Europe/Stockholm date range of at most `max_days` calendar days."""

    start: Date
    end: Date

    @classmethod
    def checked(cls, start: Date, end: Date, max_days: int) -> DateRange:
        if not 0 <= (end - start).days < max_days:
            raise InvalidInput(
                f"Use end_date >= start_date and at most {max_days} calendar days.",
                code="invalid_date_range",
            )
        return cls(start, end)

    def overlaps(self, starts_at: datetime, ends_at: datetime) -> bool:
        return starts_at.date() <= self.end and ends_at.date() >= self.start

    def __contains__(self, day: Date) -> bool:
        return self.start <= day <= self.end


class Child(BaseModel):
    """A guardian-linked pupil discovered from the current Hub session."""

    id: str
    name: str
    selected: bool = False
    source_reference: str = "/Account/PupilSwitcher/SwitchPupil/{child_id}"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Attachment(BaseModel):
    """A discovered news attachment and, when requested, its inline text."""

    id: str | None
    name: str | None = None
    status: str = "available"
    text: str | None = None
    truncated: bool = False
    content_sha256: str | None = None

    @classmethod
    def list_from_hub(cls, news_id: str, raw: object) -> list[Attachment]:
        """Accept only Hub's own download references for this news item."""
        attachments = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get("title") if isinstance(item.get("title"), str) else None
            attachment = cls(id=None, name=name, status="invalid_reference")
            try:
                url = urlsplit(item.get("url", ""))
                query = parse_qs(url.query, strict_parsing=True)
                prefix = "/Resources/Resource/Download/"
                resource_id = url.path.removeprefix(prefix)
                if (
                    url.scheme not in {"", "https"}
                    or url.netloc not in {"", "hub.infomentor.se"}
                    or url.fragment or not url.path.startswith(prefix)
                    or not resource_id.isascii() or not resource_id.isdecimal()
                    or query != {"api": ["IM2"], "ModuleType": ["NewsItem"],
                                 "ConnectionId": [news_id]}
                ):
                    raise ValueError("Invalid attachment reference")
                attachment.id, attachment.status = resource_id, "available"
            except (TypeError, ValueError, AttributeError):
                pass
            attachments.append(attachment)
        return attachments


class NewsItem(BaseModel):
    """A school-news item, retaining its original API payload for completeness."""

    id: str
    child_ids: list[str]
    title: str | None = None
    content: str | None = None
    published_at: str | None = None
    source_type: str = "news"
    source_reference: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attachments: list[Attachment] = Field(default_factory=list)
    raw: dict[str, Any]

    @classmethod
    def from_hub(cls, child_id: str, item: dict[str, object]) -> NewsItem:
        if item.get("id") is None:
            raise UnexpectedInfoMentorResponse("News item did not contain an ID.")
        item_id = str(item["id"])
        return cls(
            id=item_id,
            child_ids=[child_id],
            title=_string_or_none(item.get("title")),
            content=_string_or_none(item.get("content")),
            published_at=_string_or_none(item.get("publishedDate")),
            source_reference=f"/#/communication/news/{item_id}",
            attachments=Attachment.list_from_hub(item_id, item.get("attachments")),
            raw=item,
        )


class Notification(BaseModel):
    """One server notification, preserving its independent state for each child."""

    id: str
    child_ids: list[str]
    states: dict[str, str | None]
    title: str | None = None
    subtitle: str | None = None
    subjects_courses: str | None = None
    sent_at: str | None = None
    order_date: str | None = None
    app_type: str | None = None
    notification_type: str | None = None
    source_reference: str | None = None
    source_type: str = "notification"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_hub(cls, child_id: str, row: dict) -> Notification:
        return cls(
            id=str(row["id"]), child_ids=[child_id],
            states={child_id: _string_or_none(row.get("state"))},
            title=_string_or_none(row.get("title")),
            subtitle=_string_or_none(row.get("subTitle")),
            subjects_courses=_string_or_none(row.get("subjectsCourses")),
            sent_at=_string_or_none(row.get("dateSent")),
            order_date=_string_or_none(row.get("orderDate")),
            app_type=_string_or_none(row.get("appType")),
            notification_type=_string_or_none(row.get("type")),
            source_reference=_string_or_none(row.get("url")),
        )


class CalendarEntry(BaseModel):
    """A Hub calendar event with local times resolved in the Stockholm timezone."""

    id: str
    child_ids: list[str]
    title: str | None = None
    description: str | None = None
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    timezone: str = "Europe/Stockholm"
    has_attachments: bool = False
    source_type: str = "calendar"
    source_reference: str = "/calendarv2/calendarv2/getentries"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_hub(cls, child_id: str, row: object) -> CalendarEntry:
        try:
            if not isinstance(row, dict):
                raise ValueError("Not an event")
            id_ = row["id"]
            if isinstance(id_, bool) or not isinstance(id_, (int, str)) or not str(id_).strip():
                raise ValueError("Missing identity")
            if not isinstance(row["isAllDayEvent"], bool):
                raise ValueError("Missing all-day flag")
            start = stockholm_datetime(row["startDateFull"])
            end = stockholm_datetime(row["endDateFull"])
            if end.timestamp() < start.timestamp():
                raise ValueError("Reversed event dates")
            return cls(
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


class TimetableLesson(BaseModel):
    """One child's scheduled lesson/activity; Hub supplies no stable lesson ID."""

    child_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    room: str | None = None
    teachers: str | None = None
    notes: str | None = None
    details: str | None = None
    establishment: str | None = None
    timezone: str = "Europe/Stockholm"
    source_type: str = "timetable"
    source_reference: str = "/timetable/timetable/gettimetablelist"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_hub(cls, child_id: str, row: object) -> TimetableLesson:
        try:
            if (
                not isinstance(row, dict) or not isinstance(row.get("title"), str)
                or not isinstance(row.get("allDay"), bool)
            ):
                raise ValueError("Missing lesson fields")
            start = stockholm_datetime(row["start"])
            end = stockholm_datetime(row["end"])
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
            return cls(
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


class ChildcareComment(BaseModel):
    author_type: str
    author: str | None = None
    text: str
    sent_at: datetime | None = None

    @classmethod
    def list_from_hub(cls, payload: dict) -> list[ChildcareComment]:
        """Staff and guardian comments; raises ValueError/KeyError/TypeError on bad shapes."""
        comments = []
        for prefix, kind, name in (("teacher", "staff", "teacherName"),
                                   ("user", "guardian", "userName")):
            text = payload[prefix + "Comment"]
            if text is not None and not isinstance(text, str):
                raise ValueError("Invalid comment text")
            if text:
                sent = payload.get(prefix + "CommentDate")
                comments.append(cls(
                    author_type=kind, author=_string_or_none(payload.get(name)), text=text,
                    sent_at=stockholm_datetime(sent) if sent else None,
                ))
        return comments


class ChildcareDay(BaseModel):
    """One child's submitted childcare schedule, not classroom lesson times."""

    child_id: str
    date: Date
    registration_id: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    on_leave: bool
    is_closed: bool
    closed_reason: str | None = None
    is_locked: bool | None = None
    has_comments: bool | None = None
    has_unread_comments: bool | None = None
    comments: list[ChildcareComment] = Field(default_factory=list)
    comments_status: str = "not_requested"
    comments_marked_read: bool | None = False
    timezone: str = "Europe/Stockholm"
    source_type: str = "childcare_schedule"
    source_reference: str = "/TimeRegistration/TimeRegistration/GetTimeRegistrations/"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def flags_comments(self) -> bool:
        return bool(self.has_comments or self.has_unread_comments)

    @classmethod
    def from_hub(cls, child_id: str, row: object) -> ChildcareDay:
        try:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(key), bool) for key in ("onLeave", "isSchoolClosed")
            ):
                raise ValueError("Missing childcare flags")
            day = stockholm_datetime(row["date"]).date()
            times = {}
            for source, target in (
                ("startDateTime", "starts_at"), ("endDateTime", "ends_at"),
                ("schoolOpeningTime", "opens_at"), ("schoolClosingTime", "closes_at"),
            ):
                value = row.get(source)
                times[target] = None if value is None or value == "" else stockholm_datetime(value)
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
            return cls(
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
