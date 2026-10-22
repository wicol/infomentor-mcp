"""Normalized, read-only models returned by the reusable InfoMentor client."""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as Date
from typing import Any

from pydantic import BaseModel, Field


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


class ChildcareComment(BaseModel):
    author_type: str
    author: str | None = None
    text: str
    sent_at: datetime | None = None


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
