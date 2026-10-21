"""Normalized, read-only models returned by the reusable InfoMentor client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Child(BaseModel):
    """A guardian-linked pupil discovered from the current Hub session."""

    id: str
    name: str
    selected: bool = False
    source_reference: str = "/Account/PupilSwitcher/SwitchPupil/{child_id}"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NewsItem(BaseModel):
    """A school-news item, retaining its original API payload for completeness."""

    id: str
    child_id: str
    title: str | None = None
    content: str | None = None
    published_at: str | None = None
    source_type: str = "news"
    source_reference: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw: dict[str, Any]
