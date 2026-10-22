"""Reusable read-only Swedish InfoMentor Hub client."""

from __future__ import annotations

import html
import re
import threading
from datetime import date, datetime
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

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
from infomentor_mcp.client.models import Attachment, CalendarEntry, Child, NewsItem, Notification

_PUPIL_SWITCH_PATH = re.compile(r"/Account/PupilSwitcher/SwitchPupil/([^/?#'\"\\]+)", re.I)
_SELECTED_PUPIL_NAME = re.compile(r"selectedPupilName\s*:\s*'((?:\\.|[^'])*)'", re.I)
MAX_CALENDAR_DAYS = 366


class UnexpectedInfoMentorResponse(InfoMentorError):
    """Hub responded successfully but not in the documented shape."""


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
    """A session-bound client that exposes only documented read operations."""

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
        include_attachments: bool = False,
    ) -> list[NewsItem]:
        """Get unique news, newest first, for selected children or all linked children."""
        return self._with_session_renewal(
            lambda: self._get_news(child_ids, offset, limit, include_attachments)
        )

    def _get_news(
        self, child_ids: list[str] | None, offset: int = 0, limit: int | None = None,
        include_attachments: bool = False,
    ) -> list[NewsItem]:
        children = self._list_children()
        selected = next((child for child in children if child.selected), None)
        targets = (
            [self._find_child(children, child_id) for child_id in dict.fromkeys(child_ids)]
            if child_ids is not None else children
        )
        child_id_set = {child.id for child in children}
        items: dict[str, NewsItem] = {}
        try:
            for child in targets:
                self._select_child(child.id, child_id_set)
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
                    self._select_child(item.child_ids[0], child_id_set)
                    for index, attachment in enumerate(item.attachments):
                        if attachment.status != "available":
                            continue
                        if remaining <= 0:
                            result = attachment.model_copy(update={"status": "limit_reached"})
                        elif attachment.id in cache:
                            result = cache[attachment.id].model_copy(
                                deep=True, update={"name": attachment.name}
                            )
                        elif len(cache) >= MAX_ATTACHMENTS:
                            result = attachment.model_copy(update={"status": "limit_reached"})
                        else:
                            result = self._read_attachment(item.id, attachment)
                            cache[attachment.id] = result.model_copy(deep=True)
                        if result.text:
                            result.truncated |= len(result.text) > remaining
                            result.text = result.text[:remaining]
                            remaining -= len(result.text)
                        item.attachments[index] = result
            return page
        finally:
            if selected:
                self._select_child(selected.id, child_id_set)

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
        children = self._list_children()
        selected = next((child for child in children if child.selected), None)
        targets = (
            [self._find_child(children, id_) for id_ in dict.fromkeys(child_ids)]
            if child_ids is not None else children
        )
        linked_ids = {child.id for child in children}
        items: dict[str, CalendarEntry] = {}
        try:
            for child in targets:
                self._select_child(child.id, linked_ids)
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
                for row in rows:
                    entry = self._parse_calendar_entry(child.id, row)
                    if entry.starts_at.date() > end_date or entry.ends_at.date() < start_date:
                        continue
                    if entry.id not in items:
                        items[entry.id] = entry
                    elif child.id not in items[entry.id].child_ids:
                        items[entry.id].child_ids.append(child.id)
            return sorted(items.values(), key=lambda item: (item.starts_at, item.id))
        finally:
            if selected:
                self._select_child(selected.id, linked_ids)

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
            timezone = ZoneInfo("Europe/Stockholm")
            start = datetime.fromisoformat(row["startDateFull"])
            end = datetime.fromisoformat(row["endDateFull"])
            start = (
                start.replace(tzinfo=timezone)
                if start.tzinfo is None else start.astimezone(timezone)
            )
            end = end.replace(tzinfo=timezone) if end.tzinfo is None else end.astimezone(timezone)
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
            return extract_text(result, bytes(content))
        except (httpx.HTTPError, ValueError):
            result.status = "fetch_failed"
            return result

    def _with_session_renewal(self, operation):
        process_lock = self._lock_store.locked()
        with self._operation_lock, process_lock:
            self._original_child_id = None
            if self._session_store:
                latest = self._session_store.load()
                if latest is None:
                    raise AuthenticationExpiredError("No saved session; run auth login again.")
                saved_cookies = sorted(
                    (cookie.domain, cookie.path, cookie.name, cookie.value)
                    for cookie in latest.cookies
                )
                active_cookies = sorted(
                    (cookie.domain, cookie.path, cookie.name, cookie.value)
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
            finally:
                if self._session_store and persist_current:
                    self._session_store.save(self._http.session())
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
        return [
            self._parse_news_item(child_id, item)
            for item in source_items
            if isinstance(item, dict)
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
