"""Bounded in-memory PDF/DOCX text extraction; no file writes or external resources."""

from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from pathlib import PurePath
from zipfile import ZipFile

import httpx
from defusedxml import ElementTree
from pypdf import PdfReader

from infomentor_mcp.errors import AuthenticationExpiredError
from infomentor_mcp.hub.transport import HUB_ORIGIN, HubTransport
from infomentor_mcp.models import Attachment, NewsItem

# Parser diagnostics can contain document fragments; never log school content.
logging.getLogger("pypdf").setLevel(logging.CRITICAL)


class AttachmentReader:
    """Download and extract news attachments within per-file and per-batch limits.

    One reader is one batch: identical files are downloaded once and the total amount of
    returned text is bounded.
    """

    MAX_FILE_BYTES = 10 * 1024 * 1024
    MAX_ATTACHMENTS = 10
    MAX_TEXT_CHARS = 20_000
    MAX_TOTAL_TEXT_CHARS = 60_000

    def __init__(self, transport: HubTransport) -> None:
        self.transport = transport
        self.cache: dict[str, Attachment] = {}
        self.remaining = self.MAX_TOTAL_TEXT_CHARS

    def enrich(self, items: list[NewsItem]) -> None:
        """Replace available attachments of the currently selected pupil's items with text."""
        for item in items:
            for index, attachment in enumerate(item.attachments):
                if attachment.status != "available":
                    continue
                if self.remaining <= 0 or (
                    attachment.id not in self.cache and len(self.cache) >= self.MAX_ATTACHMENTS
                ):
                    result = attachment.model_copy(update={"status": "limit_reached"})
                elif attachment.id in self.cache:
                    result = self.cache[attachment.id].model_copy(
                        deep=True, update={"name": attachment.name},
                    )
                else:
                    result = self.read(item.id, attachment)
                    self.cache[attachment.id] = result.model_copy(deep=True)
                if result.text:
                    result.truncated |= len(result.text) > self.remaining
                    result.text = result.text[:self.remaining]
                    self.remaining -= len(result.text)
                item.attachments[index] = result

    def read(self, news_id: str, attachment: Attachment) -> Attachment:
        result = attachment.model_copy(deep=True)
        if not (result.name or "").lower().endswith((".pdf", ".docx")):
            result.status = "unsupported_type"
            return result
        # Construct the URL from rediscovered IDs, never accept a caller-supplied URL.
        path = f"/Resources/Resource/Download/{result.id}"
        try:
            with self.transport.client.stream(
                "GET", HUB_ORIGIN + path,
                params={"api": "IM2", "ModuleType": "NewsItem", "ConnectionId": news_id},
            ) as response:
                if response.status_code == 401 or response.is_redirect:
                    raise AuthenticationExpiredError("Attachment request requires authentication.")
                response.raise_for_status()
                if int(response.headers.get("content-length", "0")) > self.MAX_FILE_BYTES:
                    result.status = "too_large"
                    return result
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > self.MAX_FILE_BYTES:
                        result.status = "too_large"
                        return result
            result.content_sha256 = hashlib.sha256(content).hexdigest()
            return self.extract(result, bytes(content))
        except (httpx.HTTPError, ValueError):
            result.status = "fetch_failed"
            return result

    @classmethod
    def extract(cls, attachment: Attachment, content: bytes) -> Attachment:
        result = attachment.model_copy(deep=True)
        try:
            extension = PurePath(result.name or "").suffix.lower()
            if extension == ".pdf" and content.startswith(b"%PDF-"):
                pdf = PdfReader(BytesIO(content), strict=True)
                if pdf.is_encrypted:
                    result.status = "encrypted"
                    return result
                parts = []
                length = 0
                for page in pdf.pages[:50]:
                    text = page.extract_text() or ""
                    parts.append(text)
                    length += len(text)
                    if length > cls.MAX_TEXT_CHARS:
                        break
                text = "\n\n".join(parts).strip()
                result.truncated = len(pdf.pages) > len(parts)
                result.status = "extracted" if text else "ocr_required"
            elif extension == ".docx" and content.startswith(b"PK"):
                with ZipFile(BytesIO(content)) as archive:
                    entries = archive.infolist()
                    if len(entries) > 500 or sum(e.file_size for e in entries) > 40 * 1024 * 1024:
                        result.status = "too_large"
                        return result
                    xml = archive.getinfo("word/document.xml")
                    if xml.file_size > cls.MAX_FILE_BYTES:
                        result.status = "too_large"
                        return result
                    root = ElementTree.fromstring(archive.read(xml))
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                text = "\n".join(
                    "".join(node.text or "" for node in paragraph.iter(ns + "t"))
                    for paragraph in root.iter(ns + "p")
                ).strip()
                result.status = "extracted" if text else "no_text"
            else:
                result.status = "invalid_format"
                return result
            result.truncated |= len(text) > cls.MAX_TEXT_CHARS
            result.text = text[:cls.MAX_TEXT_CHARS] or None
        except Exception:
            # Parser errors may include document text or internal archive names.
            result.status = "extraction_failed"
        return result
