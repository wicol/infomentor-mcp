"""Lesson/activity timetables (Schema), not childcare registrations or attendance."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from infomentor_mcp.errors import UnexpectedInfoMentorResponse
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.models import STOCKHOLM, DateRange, TimetableLesson


class Timetable:
    PATH = "/timetable/timetable/gettimetablelist"
    MAX_DAYS = 31

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def lessons(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
    ) -> list[TimetableLesson]:
        period = DateRange.checked(start_date, end_date, self.MAX_DAYS)
        return self.hub.run(lambda: self._lessons(period, child_ids))

    def _lessons(self, period, child_ids) -> list[TimetableLesson]:
        # Match the web client's current browser offset (JS getTimezoneOffset sign).
        # Response timestamps are Stockholm wall times, resolved individually.
        offset = datetime.now(STOCKHOLM).utcoffset()
        payload = {
            "start": period.start.isoformat(),
            "end": (period.end + timedelta(days=1)).isoformat(),
            "UTCOffset": str(-int(offset.total_seconds() // 60)),
        }
        lessons = []
        with self.hub.pupils.visiting(child_ids) as children:
            for child in children:
                self.hub.pupils.select(child.id)
                response = self.hub.transport.post(self.PATH, payload, form=True)
                try:
                    rows = response.json()
                    if not isinstance(rows, list):
                        raise ValueError("Not a lesson list")
                except ValueError:
                    raise UnexpectedInfoMentorResponse(
                        "Timetable response had invalid lessons or unexpected dates."
                    ) from None
                for row in rows:
                    lesson = TimetableLesson.from_hub(child.id, row)
                    if lesson.starts_at.date() not in period:
                        raise UnexpectedInfoMentorResponse(
                            "Timetable response had invalid lessons or unexpected dates."
                        )
                    lessons.append(lesson)
        return sorted(lessons, key=lambda item: (
            item.starts_at, item.child_id, item.ends_at, item.title,
        ))
