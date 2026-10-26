"""Hub calendar events for linked children."""

from __future__ import annotations

from datetime import date

from infomentor_mcp.errors import UnexpectedInfoMentorResponse
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.models import CalendarEntry, DateRange


class Calendar:
    PATH = "/calendarv2/calendarv2/getentries"
    MAX_DAYS = 366

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def entries(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
    ) -> list[CalendarEntry]:
        """Events overlapping an inclusive Stockholm date range, shared IDs merged."""
        period = DateRange.checked(start_date, end_date, self.MAX_DAYS)
        return self.hub.run(lambda: self._entries(period, child_ids))

    def read_selected(self, child_id: str, period: DateRange) -> list[CalendarEntry]:
        """The calendar of the pupil currently selected in Hub."""
        response = self.hub.transport.post(self.PATH, {
            "startDate": period.start.isoformat(), "endDate": period.end.isoformat(),
        })
        try:
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Not a list")
        except ValueError:
            raise UnexpectedInfoMentorResponse(
                "Calendar response did not contain an event list."
            ) from None
        entries = [CalendarEntry.from_hub(child_id, row) for row in rows]
        return [entry for entry in entries if period.overlaps(entry.starts_at, entry.ends_at)]

    def _entries(self, period, child_ids) -> list[CalendarEntry]:
        items: dict[str, CalendarEntry] = {}
        with self.hub.pupils.visiting(child_ids) as children:
            for child in children:
                self.hub.pupils.select(child.id)
                for entry in self.read_selected(child.id, period):
                    if entry.id not in items:
                        items[entry.id] = entry
                    elif child.id not in items[entry.id].child_ids:
                        items[entry.id].child_ids.append(child.id)
        return sorted(items.values(), key=lambda item: (item.starts_at, item.id))
