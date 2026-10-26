"""Fritidsschema: submitted before/after-school and holiday care, never lesson times.

Writes match Hub's web forms for explicit dates, and are never replayed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx

from infomentor_mcp.errors import (
    AuthenticationExpiredError,
    InfoMentorError,
    InvalidInput,
    UnexpectedInfoMentorResponse,
)
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.models import (
    STOCKHOLM,
    ChildcareComment,
    ChildcareDay,
    DateRange,
    stockholm_datetime,
)


class Childcare:
    ROOT = "/TimeRegistration/TimeRegistration/"
    MAX_DAYS = 31

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def schedule(
        self, start_date: date, end_date: date, child_ids: list[str] | None = None,
        *, include_comments: bool = True, offset: int = 0, limit: int | None = None,
    ) -> list[ChildcareDay]:
        """Registered days per child, sorted by date then child. Comments are only opened for
        the returned page, and opening them marks them read in Hub."""
        period = DateRange.checked(start_date, end_date, self.MAX_DAYS)
        return self.hub.run(
            lambda: self._schedule(period, child_ids, include_comments, offset, limit)
        )

    def week_rows(self, week: date) -> dict[date, dict]:
        """The selected pupil's raw registrations for the week starting on `week`."""
        response = self.hub.transport.post(self.ROOT + "GetTimeRegistrations/", {
            "date": week.isoformat() + "T00:00:00.000Z",
            "showNextWeekIfNoMoreSchoolDays": False,
        })
        try:
            payload = response.json()
            week_start = stockholm_datetime(payload["startDate"]).date()
            week_end = stockholm_datetime(payload["endDate"]).date()
            if (
                not isinstance(payload["days"], list) or week_start != week
                or not week <= week_end <= week + timedelta(days=6)
            ):
                raise ValueError("Unexpected week")
            rows = {}
            for row in payload["days"]:
                day = ChildcareDay.from_hub("", row).date
                if not week_start <= day <= week_end or day in rows:
                    raise ValueError("Day outside returned week or duplicated")
                rows[day] = row
            return rows
        except (ValueError, KeyError, TypeError):
            raise UnexpectedInfoMentorResponse(
                "Childcare days were invalid or did not match the requested week."
            ) from None

    def comment_details(self, date_value: str) -> object:
        """Hub marks a day's comments read as part of this request."""
        return self.hub.transport.post(self.ROOT + "GetComments/", {"date": date_value}).json()

    def _schedule(self, period, child_ids, include_comments, offset, limit) -> list[ChildcareDay]:
        days = []
        with self.hub.pupils.visiting(child_ids) as children:
            for child in children:
                self.hub.pupils.select(child.id)
                week = period.start - timedelta(days=period.start.weekday())
                while week <= period.end:
                    days += [
                        ChildcareDay.from_hub(child.id, row)
                        for day, row in self.week_rows(week).items() if day in period
                    ]
                    week += timedelta(days=7)
            page = sorted(days, key=lambda day: (day.date, day.child_id))
            page = page[offset : offset + limit if limit is not None else None]
            if include_comments:
                self._load_comments(children, page)
            return page

    def _load_comments(self, children, page) -> None:
        for day in page:
            if not day.flags_comments:
                day.comments_status = "not_present"
        for child in children:
            days = [day for day in page if day.child_id == child.id and day.flags_comments]
            if days:
                self.hub.pupils.select(child.id)
            for day in days:
                # An interrupted or invalid response has an unknown read-receipt outcome.
                day.comments_marked_read = None
                try:
                    day.comments = ChildcareComment.list_from_hub(
                        self.comment_details(day.date.isoformat() + "T00:00:00")
                    )
                except AuthenticationExpiredError:
                    raise  # Reuse the guarded session-renewal/replay path.
                except (InfoMentorError, httpx.HTTPError):
                    day.comments_status = "fetch_failed"
                    continue
                except (ValueError, KeyError, TypeError, AttributeError):
                    day.comments_status = "invalid_response"
                    continue
                day.comments_status = "loaded"
                day.comments_marked_read = True
                day.has_unread_comments = False


class ScheduleUpdate:
    """Set registered times, or leave without times, preserving existing comments."""

    endpoint = "SaveTimeRegistrations/"
    marks_comments_read = False

    def __init__(self, start_time: str | None, end_time: str | None, on_leave: bool) -> None:
        valid_times = all(
            isinstance(t, str) and len(t) == 5 and t[2] == ":"
            and t[:2].isascii() and t[:2].isdigit() and t[3:].isascii() and t[3:].isdigit()
            and 0 <= int(t[:2]) < 24 and 0 <= int(t[3:]) < 60
            for t in (start_time, end_time)
        )
        if (on_leave and (start_time is not None or end_time is not None)) or (
            not on_leave and (not valid_times or start_time >= end_time)
        ):
            raise InvalidInput("Use HH:MM start_time < end_time, or on_leave=true without times.")
        self.start_time, self.end_time, self.on_leave = start_time, end_time, on_leave

    def prepare(self, childcare: Childcare, child_id: str, day: date, row: dict):
        """Return (save payload, before, requested); refuses days Hub would not accept."""
        if (
            row.get("canEdit") is not True or row.get("isLocked") is not False
            or row.get("isSchoolClosed") is not False
        ):
            raise InfoMentorError("A requested day is locked/closed; no changes sent.")
        current = ChildcareDay.from_hub(child_id, row)
        if not self.on_leave and (
            current.opens_at and self.start_time < current.opens_at.strftime("%H:%M")
            or current.closes_at and self.end_time > current.closes_at.strftime("%H:%M")
        ):
            raise InfoMentorError("Times exceed care opening hours; no changes sent.")
        updated = dict(row)
        updated.update({
            "onLeave": self.on_leave,
            "registrationType": "OnLeave" if self.on_leave else "TimeReg",
            "commentText": "", "isCommentUpdated": False, "commentId": 0,
        })
        for key, value in (("startDateTime", self.start_time), ("endDateTime", self.end_time)):
            updated.pop(key, None)
            if not self.on_leave:
                updated[key] = f"{day.isoformat()}T{value}:00"
        requested = {
            "on_leave": self.on_leave, "start_time": self.start_time, "end_time": self.end_time,
        }
        return {"days": [updated], "series": None}, current.model_dump(mode="json"), requested

    def verify(self, childcare: Childcare, child_id: str, day: date) -> bool:
        week = day - timedelta(days=day.weekday())
        actual = ChildcareDay.from_hub(child_id, childcare.week_rows(week).get(day))
        if self.on_leave:
            return actual.on_leave and actual.starts_at is None and actual.ends_at is None
        return (
            not actual.on_leave and actual.starts_at is not None and actual.ends_at is not None
            and actual.starts_at.strftime("%H:%M:%S") == self.start_time + ":00"
            and actual.ends_at.strftime("%H:%M:%S") == self.end_time + ":00"
        )


class CommentUpdate:
    """Replace the entire guardian comment; staff comments and times are untouched."""

    endpoint = "SaveComment/"
    marks_comments_read = True

    def __init__(self, comment: str) -> None:
        if not comment.strip() or len(comment) > 10000:
            raise InvalidInput(
                "Provide non-empty comment text, at most 10000 characters. Deletion unsupported."
            )
        self.comment = comment

    def prepare(self, childcare: Childcare, child_id: str, day: date, row: dict):
        if row.get("canEditComment") is False:
            raise InfoMentorError("Comment is not editable; no changes sent.")
        details = childcare.comment_details(row["date"])
        if not isinstance(details, dict) or details.get("canEdit") is not True:
            raise InfoMentorError("Comment is not editable; no changes sent.")
        registration_id = row.get("timeRegistrationId")
        comment_id = details.get("parentCommentId")
        if type(registration_id) is not int or registration_id <= 0 or (
            comment_id is not None and (type(comment_id) is not int or comment_id < 0)
        ) or not isinstance(details.get("userComment"), (str, type(None))):
            raise InfoMentorError("Invalid comment identifiers; no changes sent.")
        payload = {"commentId": comment_id or 0, "commentText": self.comment,
                   "timeRegistrationId": registration_id}
        return payload, {"comment": details.get("userComment")}, {"comment": self.comment}

    def verify(self, childcare: Childcare, child_id: str, day: date) -> bool:
        actual = childcare.comment_details(day.isoformat() + "T00:00:00")
        return isinstance(actual, dict) and actual.get("userComment") == self.comment


class ChildcareWriter:
    """Preflight every date, preview by default, then save and read back each day."""

    MAX_DATES = 31

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.childcare = Childcare(hub)

    def set_schedule(
        self, child_id: str, dates: list[date], *, start_time: str | None = None,
        end_time: str | None = None, on_leave: bool = False, confirm: bool = False,
    ) -> dict:
        return self.apply(child_id, dates, ScheduleUpdate(start_time, end_time, on_leave), confirm)

    def set_comment(
        self, child_id: str, dates: list[date], comment: str, *, confirm: bool = False,
    ) -> dict:
        return self.apply(child_id, dates, CommentUpdate(comment), confirm)

    def apply(self, child_id: str, dates: list[date], update, confirm: bool) -> dict:
        dates = sorted(set(dates))
        today = datetime.now(STOCKHOLM).date()
        if not child_id or not dates or len(dates) > self.MAX_DATES or dates[0] < today:
            raise InvalidInput("Provide one child and 1-31 explicit dates, today or later.")
        return self.hub.run(
            lambda: self._apply(child_id, dates, update, confirm),
            keep_result_on_storage_error=True,
        )

    def _apply(self, child_id, dates, update, confirm) -> dict:
        pupils = self.hub.pupils
        children = pupils.discover()
        pupils.find(children, child_id)
        original = next((child.id for child in children if child.selected), None)
        if original is None:
            raise InfoMentorError("Cannot safely restore child selection.")
        result = {"status": "preview", "child_id": child_id, "items": [],
                  "comments_marked_read": update.marks_comments_read}
        try:
            pupils.select(child_id)
            prepared = self._prepare(child_id, dates, update, result)
            if confirm:
                self._save(child_id, prepared, update, result)
            return result
        finally:
            try:
                pupils.select(original)
            except (InfoMentorError, httpx.HTTPError):
                # Once a write was sent, an auth error here must never replay it through
                # the session renewal.
                if all(item["status"] == "not_sent" for item in result["items"]):
                    raise
                result["selection_restored"] = False

    def _prepare(self, child_id, dates, update, result) -> list:
        weeks = {}
        prepared = []
        for day in dates:
            week = day - timedelta(days=day.weekday())
            if week not in weeks:
                weeks[week] = self.childcare.week_rows(week)
            row = weeks[week].get(day)
            if row is None:
                raise InfoMentorError("A requested date is missing; no changes sent.")
            payload, before, requested = update.prepare(self.childcare, child_id, day, row)
            item = {"date": day.isoformat(), "before": before, "requested": requested,
                    "status": "not_sent"}
            result["items"].append(item)
            prepared.append((day, payload, item))
        return prepared

    def _save(self, child_id, prepared, update, result) -> None:
        result["status"] = "completed"
        for day, payload, item in prepared:
            item["status"] = "unknown"
            try:
                response = self.hub.transport.post(self.childcare.ROOT + update.endpoint, payload)
                saved = response.json()
                if not isinstance(saved, dict) or saved.get("success") is not True:
                    result["status"] = "incomplete"
                    return
                verified = update.verify(self.childcare, child_id, day)
            except (InfoMentorError, httpx.HTTPError, ValueError, TypeError, KeyError):
                result["status"] = "incomplete"
                return
            item["status"] = "verified" if verified else "verification_mismatch"
            if not verified:
                result["status"] = "incomplete"
                return
