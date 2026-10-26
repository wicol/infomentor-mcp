"""Hub's server-side pupil selection: discover linked children and switch deliberately."""

from __future__ import annotations

import html
import re
from contextlib import contextmanager
from urllib.parse import quote, urljoin, urlparse

from infomentor_mcp.errors import (
    AuthenticationExpiredError,
    InfoMentorError,
    UnexpectedInfoMentorResponse,
    UnknownChild,
)
from infomentor_mcp.hub.transport import HUB_ORIGIN, HubTransport
from infomentor_mcp.models import Child

_PUPIL_SWITCH_PATH = re.compile(r"/Account/PupilSwitcher/SwitchPupil/([^/?#'\"\\]+)", re.I)
_SELECTED_PUPIL_NAME = re.compile(r"selectedPupilName\s*:\s*'((?:\\.|[^'])*)'", re.I)


def parse_child_ids(page: str) -> list[str]:
    """Extract linked pupil IDs from Hub's generated switcher routes."""
    return list(dict.fromkeys(match.group(1) for match in _PUPIL_SWITCH_PATH.finditer(page)))


class PupilSwitcher:
    """Hub shows one selected pupil at a time, for the whole session.

    Every switch is confirmed by name before data is read, and operations restore the
    pupil that was selected before they started.
    """

    def __init__(self, transport: HubTransport) -> None:
        self.transport = transport
        self.linked_ids: set[str] = set()
        self.expected_names: dict[str, str] = {}
        self.original_id: str | None = None

    @staticmethod
    def selected_name(page: str) -> str | None:
        match = _SELECTED_PUPIL_NAME.search(page)
        if match is None:
            return None
        return html.unescape(match.group(1).replace("\\'", "'").replace("\\\\", "\\"))

    @staticmethod
    def same_name(first: str, second: str | None) -> bool:
        if second is None:
            return False
        normalized_first = " ".join(first.lower().replace(",", " ").split())
        normalized_second = " ".join(second.lower().replace(",", " ").split())
        return normalized_first == normalized_second

    @staticmethod
    def find(children: list[Child], child_id: str | None) -> Child:
        if child_id is None:
            raise UnknownChild("A child ID is required for this operation.")
        child = next((item for item in children if item.id == child_id), None)
        if child is None:
            raise UnknownChild("The requested child is not linked to this InfoMentor account.")
        return child

    def discover(self) -> list[Child]:
        """Switch to every linked pupil once to learn and verify its name."""
        self.expected_names = {}
        page = self.transport.get_page("/")
        child_ids = parse_child_ids(page)
        initial_name = self.selected_name(page)
        if not child_ids or initial_name is None:
            raise UnexpectedInfoMentorResponse(
                "Hub page did not contain linked pupil switchers and a selected pupil name."
            )
        self.linked_ids = set(child_ids)
        children: list[Child] = []
        original_id: str | None = None
        last_selected_id: str | None = None
        try:
            for child_id in child_ids:
                name = self.selected_name(self.select(child_id))
                if name is None:
                    raise UnexpectedInfoMentorResponse(
                        "Hub did not expose the selected pupil name after switching pupil."
                    )
                is_selected = self.same_name(name, initial_name)
                if any(self.same_name(name, child.name) for child in children):
                    raise UnexpectedInfoMentorResponse(
                        "Pupil selection is ambiguous or the switch was ignored; "
                        "no school data read."
                    )
                if is_selected:
                    original_id = self.original_id = child_id
                children.append(Child(id=child_id, name=name, selected=is_selected))
                self.expected_names[child_id] = name
                last_selected_id = child_id
            if original_id is None:
                raise UnexpectedInfoMentorResponse(
                    "Could not identify the original pupil selection; no school data read."
                )
        finally:
            if original_id and last_selected_id != original_id:
                self.select(original_id)
        return children

    @contextmanager
    def visiting(self, child_ids: list[str] | None = None):
        """Discover, validate the requested children, and restore the original selection
        afterwards. None selects all linked children; an empty list selects none."""
        children = self.discover()
        targets = children if child_ids is None else [
            self.find(children, id_) for id_ in dict.fromkeys(child_ids)
        ]
        original = next(child for child in children if child.selected)
        try:
            yield targets
        finally:
            self.select(original.id)

    def select(self, child_id: str) -> str:
        """Switch pupil, following only same-origin redirects; returns the confirmed page."""
        if child_id not in self.linked_ids:
            raise UnknownChild("The requested child is not linked to this InfoMentor account.")
        destination = f"{HUB_ORIGIN}/Account/PupilSwitcher/SwitchPupil/{quote(child_id, safe='')}"
        for _ in range(6):
            response = self.transport.client.get(destination, follow_redirects=False)
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
        expected_name = self.expected_names.get(child_id)
        if expected_name is not None and not self.same_name(
            self.selected_name(response.text) or "", expected_name
        ):
            raise UnexpectedInfoMentorResponse(
                "Hub did not confirm the requested pupil after switching; no school data returned."
            )
        return response.text
