"""School news for linked children, with bounded inline attachment text."""

from __future__ import annotations

from infomentor_mcp.errors import InfoMentorError, UnexpectedInfoMentorResponse
from infomentor_mcp.features.attachments import AttachmentReader
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.models import Attachment, NewsItem


class News:
    PATH = "/Communication/News/GetNewsList"

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def items(
        self, child_ids: list[str] | None = None, *, offset: int = 0, limit: int | None = None,
        include_attachments: bool = True,
    ) -> list[NewsItem]:
        """Unique news, newest first. Attachment text is only fetched for the returned page."""
        return self.hub.run(lambda: self._items(child_ids, offset, limit, include_attachments))

    def attachment(self, child_id: str, news_id: str, attachment_id: str) -> Attachment:
        """Read one attachment rediscovered in a linked child's current news feed."""
        return self.hub.run(lambda: self._attachment(child_id, news_id, attachment_id))

    def read_selected(self, child_id: str) -> list[NewsItem]:
        """The news feed of the pupil currently selected in Hub."""
        response = self.hub.transport.post(self.PATH, {})
        try:
            payload = response.json()
            source_items = payload["items"]
        except (KeyError, TypeError, ValueError) as error:
            raise UnexpectedInfoMentorResponse(
                "News response did not contain an items list."
            ) from error
        if not isinstance(source_items, list):
            raise UnexpectedInfoMentorResponse("News response items was not a list.")
        if any(not isinstance(item, dict) for item in source_items):
            raise UnexpectedInfoMentorResponse("News response contained invalid items.")
        return [NewsItem.from_hub(child_id, item) for item in source_items]

    def _items(self, child_ids, offset, limit, include_attachments) -> list[NewsItem]:
        items: dict[str, NewsItem] = {}
        with self.hub.pupils.visiting(child_ids) as children:
            for child in children:
                self.hub.pupils.select(child.id)
                for item in self.read_selected(child.id):
                    if item.id not in items:
                        items[item.id] = item
                    elif child.id not in items[item.id].child_ids:
                        items[item.id].child_ids.append(child.id)
            page = sorted(items.values(), key=lambda item: item.published_at or "", reverse=True)
            page = page[offset : offset + limit if limit is not None else None]
            if include_attachments:
                reader = AttachmentReader(self.hub.transport)
                for item in page:
                    if item.attachments:
                        self.hub.pupils.select(item.child_ids[0])
                        reader.enrich([item])
            return page

    def _attachment(self, child_id, news_id, attachment_id) -> Attachment:
        with self.hub.pupils.visiting([child_id]) as (child,):
            self.hub.pupils.select(child.id)
            item = next((item for item in self.read_selected(child.id) if item.id == news_id), None)
            attachment = next(
                (a for a in item.attachments if a.id == attachment_id), None,
            ) if item else None
            if attachment is None or attachment.status != "available":
                raise InfoMentorError("Attachment was not found in the requested child's news.")
            return AttachmentReader(self.hub.transport).read(news_id, attachment)
