"""Explicit-date childcare writes, matching Hub's web forms; never replay a write."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from infomentor_mcp.client.http import InfoMentorError

ROOT = "/TimeRegistration/TimeRegistration/"


def update_childcare(
    client, child_id: str, dates: list[date], *, start_time: str | None = None,
    end_time: str | None = None, on_leave: bool = False, comment: str | None = None,
    confirm: bool = False,
) -> dict:
    """Preflight all dates, preview by default, then save/verify each explicit day."""
    dates = sorted(set(dates))
    today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
    if not child_id or not dates or len(dates) > 31 or dates[0] < today:
        return {"error": {"code": "invalid_input", "message":
                "Provide one child and 1-31 explicit dates, today or later."}}
    if comment is None:
        valid_times = all(
            isinstance(t, str) and len(t) == 5 and t[2] == ":"
            and t[:2].isascii() and t[:2].isdigit() and t[3:].isascii() and t[3:].isdigit()
            and 0 <= int(t[:2]) < 24 and 0 <= int(t[3:]) < 60
            for t in (start_time, end_time)
        )
        if (on_leave and (start_time is not None or end_time is not None)) or (
            not on_leave and (not valid_times or start_time >= end_time)
        ):
            return {"error": {"code": "invalid_input", "message":
                    "Use HH:MM start_time < end_time, or on_leave=true without times."}}
    elif not comment.strip() or len(comment) > 10000:
        return {"error": {"code": "invalid_input", "message":
                "Provide non-empty comment text, at most 10000 characters. Deletion unsupported."}}

    def operation():
        children = client._list_children()
        client._find_child(children, child_id)
        original = next((ch.id for ch in children if ch.selected), None)
        if original is None:
            raise InfoMentorError("Cannot safely restore child selection.")
        linked = {ch.id for ch in children}
        attempted = False
        result = {"status": "preview", "child_id": child_id, "items": [],
                  "comments_marked_read": comment is not None}
        try:
            client._select_child(child_id, linked)
            weeks = {}
            prepared = []
            for day in dates:
                week = day - timedelta(days=day.weekday())
                if week not in weeks:
                    weeks[week] = read_week(client, week)
                row = weeks[week].get(day)
                if row is None:
                    raise InfoMentorError("A requested date is missing; no changes sent.")
                if comment is None:
                    if (
                        row.get("canEdit") is not True or row.get("isLocked") is not False
                        or row.get("isSchoolClosed") is not False
                    ):
                        raise InfoMentorError("A requested day is locked/closed; no changes sent.")
                    current = client._parse_childcare_day(child_id, row)
                    if not on_leave and (
                        current.opens_at and start_time < current.opens_at.strftime("%H:%M")
                        or current.closes_at and end_time > current.closes_at.strftime("%H:%M")
                    ):
                        raise InfoMentorError("Times exceed care opening hours; no changes sent.")
                    before = current.model_dump(mode="json")
                    after = {"on_leave": on_leave, "start_time": start_time, "end_time": end_time}
                    updated = dict(row)
                    updated.update({
                        "onLeave": on_leave,
                        "registrationType": "OnLeave" if on_leave else "TimeReg",
                        "commentText": "", "isCommentUpdated": False, "commentId": 0,
                    })
                    for key, value in (("startDateTime", start_time), ("endDateTime", end_time)):
                        updated.pop(key, None)
                        if not on_leave:
                            updated[key] = f"{day.isoformat()}T{value}:00"
                    payload = {"days": [updated], "series": None}
                    endpoint = "SaveTimeRegistrations/"
                else:
                    if row.get("canEditComment") is False:
                        raise InfoMentorError("Comment is not editable; no changes sent.")
                    details = client._http.hub_post(ROOT + "GetComments/", {
                        "date": row["date"],
                    }).json()
                    if not isinstance(details, dict) or details.get("canEdit") is not True:
                        raise InfoMentorError("Comment is not editable; no changes sent.")
                    registration_id = row.get("timeRegistrationId")
                    comment_id = details.get("parentCommentId")
                    if type(registration_id) is not int or registration_id <= 0 or (
                        comment_id is not None and (type(comment_id) is not int or comment_id < 0)
                    ) or not isinstance(details.get("userComment"), (str, type(None))):
                        raise InfoMentorError("Invalid comment identifiers; no changes sent.")
                    before = {"comment": details.get("userComment")}
                    after = {"comment": comment}
                    payload = {"commentId": comment_id or 0, "commentText": comment,
                               "timeRegistrationId": registration_id}
                    endpoint = "SaveComment/"
                item = {"date": day.isoformat(), "before": before, "requested": after,
                        "status": "not_sent"}
                result["items"].append(item)
                prepared.append((day, endpoint, payload, item))
            if confirm:
                result["status"] = "completed"
                for day, endpoint, payload, item in prepared:
                    attempted = True
                    item["status"] = "unknown"
                    try:
                        saved = client._http.hub_post(ROOT + endpoint, payload).json()
                        if not isinstance(saved, dict) or saved.get("success") is not True:
                            result["status"] = "incomplete"
                            break
                        if comment is not None:
                            actual = client._http.hub_post(ROOT + "GetComments/", {
                                "date": day.isoformat() + "T00:00:00",
                            }).json()
                            matches = (
                                isinstance(actual, dict) and actual.get("userComment") == comment
                            )
                        else:
                            actual = read_week(client, day - timedelta(days=day.weekday())).get(day)
                            parsed = client._parse_childcare_day(child_id, actual)
                            matches = parsed.on_leave == on_leave and (
                                parsed.starts_at is None and parsed.ends_at is None if on_leave else
                                parsed.starts_at is not None and parsed.ends_at is not None
                                and parsed.starts_at.strftime("%H:%M:%S") == start_time + ":00"
                                and parsed.ends_at.strftime("%H:%M:%S") == end_time + ":00"
                            )
                        item["status"] = "verified" if matches else "verification_mismatch"
                        if not matches:
                            result["status"] = "incomplete"
                            break
                    except (InfoMentorError, httpx.HTTPError, ValueError, TypeError, KeyError):
                        result["status"] = "incomplete"
                        break
            return result
        finally:
            try:
                client._select_child(original, linked)
            except (InfoMentorError, httpx.HTTPError):
                if not attempted:
                    raise
                result["selection_restored"] = False
                # Never let an auth error here replay writes through the renewal wrapper.

    return client._with_session_renewal(operation, preserve_result_on_storage_error=True)


def read_week(client, week: date) -> dict:
    payload = client._http.hub_post(ROOT + "GetTimeRegistrations/", {
        "date": week.isoformat() + "T00:00:00.000Z", "showNextWeekIfNoMoreSchoolDays": False,
    }).json()
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
            raise ValueError()
        if date.fromisoformat(payload["startDate"][:10]) != week:
            raise ValueError()
        end = date.fromisoformat(payload["endDate"][:10])
        if not week <= end <= week + timedelta(days=6):
            raise ValueError()
        rows = {}
        for row in payload["days"]:
            parsed = client._parse_childcare_day("", row)
            if not week <= parsed.date <= end or parsed.date in rows:
                raise ValueError()
            rows[parsed.date] = row
        return rows
    except (ValueError, TypeError, KeyError):
        raise InfoMentorError("Invalid childcare week; details withheld.") from None
