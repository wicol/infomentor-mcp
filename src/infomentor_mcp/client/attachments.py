"""Bounded in-memory PDF/DOCX text extraction; no file writes or external resources."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import PurePath
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from defusedxml import ElementTree
from pypdf import PdfReader

from infomentor_mcp.client.models import Attachment

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 10
MAX_TEXT_CHARS = 20_000
MAX_TOTAL_TEXT_CHARS = 60_000

# Parser diagnostics can contain document fragments; never log school content.
logging.getLogger("pypdf").setLevel(logging.CRITICAL)


def parse_attachments(news_id: str, raw: object) -> list[Attachment]:
    attachments = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("title") if isinstance(item.get("title"), str) else None
        attachment = Attachment(id=None, name=name, status="invalid_reference")
        try:
            url = urlsplit(item.get("url", ""))
            query = parse_qs(url.query, strict_parsing=True)
            prefix = "/Resources/Resource/Download/"
            resource_id = url.path.removeprefix(prefix)
            if (
                url.scheme not in {"", "https"}
                or url.netloc not in {"", "hub.infomentor.se"}
                or url.fragment or not url.path.startswith(prefix)
                or not resource_id.isascii() or not resource_id.isdecimal()
                or query != {"api": ["IM2"], "ModuleType": ["NewsItem"],
                             "ConnectionId": [news_id]}
            ):
                raise ValueError("Invalid attachment reference")
            attachment.id, attachment.status = resource_id, "available"
        except (TypeError, ValueError, AttributeError):
            pass
        attachments.append(attachment)
    return attachments


def extract_text(attachment: Attachment, content: bytes) -> Attachment:
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
                if length > MAX_TEXT_CHARS:
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
                if xml.file_size > MAX_FILE_BYTES:
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
        result.truncated |= len(text) > MAX_TEXT_CHARS
        result.text = text[:MAX_TEXT_CHARS] or None
    except Exception:
        # Parser errors may include document text or internal archive names.
        result.status = "extraction_failed"
    return result
