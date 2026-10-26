"""Pupil-linked Hub notifications, read without marking them seen or following links."""

from __future__ import annotations

from infomentor_mcp.errors import UnexpectedInfoMentorResponse
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.models import Notification


class Notifications:
    PATH = "/NotificationApp/NotificationApp/GetNotifications"

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def items(self, child_ids: list[str] | None = None) -> list[Notification]:
        """Shared server IDs merged with independent per-child states, newest first."""
        return self.hub.run(lambda: self._items(child_ids))

    def _items(self, child_ids) -> list[Notification]:
        items: dict[str, Notification] = {}
        # Hub's upstream pupil identities differ from switcher IDs; remember which
        # verified selection each one appeared under.
        owners: dict[tuple[str, str], str] = {}
        with self.hub.pupils.visiting(child_ids) as children:
            for child in children:
                self.hub.pupils.select(child.id)
                for notification in self._read_selected(child.id, owners):
                    existing = items.get(notification.id)
                    if existing is None:
                        items[notification.id] = notification
                        continue
                    if child.id not in existing.child_ids:
                        existing.child_ids.append(child.id)
                    existing.states[child.id] = notification.states[child.id]
        return sorted(
            items.values(), key=lambda item: item.order_date or item.sent_at or "", reverse=True
        )

    def _read_selected(self, child_id, owners) -> list[Notification]:
        response = self.hub.transport.post(self.PATH, {})
        try:
            rows = response.json()["notifications"]
            if not isinstance(rows, list):
                raise ValueError("Not a list")
        except (ValueError, KeyError, TypeError):
            raise UnexpectedInfoMentorResponse(
                "Notification response did not contain a notifications list."
            ) from None
        notifications = []
        selected_identity = None
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("currentlySelectedPupil"), bool):
                raise UnexpectedInfoMentorResponse(
                    "Notification response did not identify selected-pupil records."
                )
            if not row["currentlySelectedPupil"]:
                continue
            # Do not guess a mapping between upstream pupil IDs and switcher IDs; bind
            # selected records to the verified selection instead.
            identity = (row.get("pupilIM2Id"), row.get("pupilSourceId"))
            if any(isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).strip()
                   for v in (*identity, row.get("id"))):
                raise UnexpectedInfoMentorResponse("Notification identity was missing.")
            identity = tuple(str(v) for v in identity)
            if (
                selected_identity is not None and selected_identity != identity
                or identity in owners and owners[identity] != child_id
            ):
                raise UnexpectedInfoMentorResponse(
                    "Notification pupil selection was ambiguous; no notifications returned."
                )
            owners[identity] = child_id
            selected_identity = identity
            notifications.append(Notification.from_hub(child_id, row))
        return notifications
