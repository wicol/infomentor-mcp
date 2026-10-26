import asyncio
import time
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from infomentor_mcp.app.mcp import McpApp
from infomentor_mcp.app.service import InfoMentorService
from infomentor_mcp.features.attachments import AttachmentReader
from infomentor_mcp.models import Attachment

URL = "/Resources/Resource/Download/123?api=IM2&ModuleType=NewsItem&ConnectionId=7"


@pytest.fixture
def docx():
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("word/document.xml", '''<w:document
          xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>Synthetic newsletter</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table text</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
          </w:body></w:document>''')
    return content.getvalue()


@pytest.mark.parametrize("url,valid", [
    (URL, True), ("https://evil.test" + URL, False),
    (URL.replace("ConnectionId=7", "ConnectionId=8"), False),
])
def test_metadata_restricts_download_references(url, valid):
    attachment = Attachment.list_from_hub("7", [{"title": "weekly.docx", "url": url}])[0]
    assert (attachment.status == "available") == valid
    assert attachment.id == ("123" if valid else None)


def test_docx_text_includes_tables_and_pdf_without_text_requires_ocr(docx, monkeypatch):
    result = AttachmentReader.extract(Attachment(id="123", name="weekly.docx"), docx)
    assert result.status == "extracted"
    assert result.text == "Synthetic newsletter\nTable text"
    monkeypatch.setattr(AttachmentReader, "MAX_TEXT_CHARS", 9)
    result = AttachmentReader.extract(Attachment(id="123", name="weekly.docx"), docx)
    assert result.truncated and result.text == "Synthetic"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = BytesIO()
    writer.write(pdf)
    result = AttachmentReader.extract(Attachment(id="1", name="scan.pdf"), pdf.getvalue())
    assert result.status == "ocr_required"
    page = writer.pages[0]
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
    })})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 50 Td (Synthetic PDF) Tj ET")
    page[NameObject("/Contents")] = stream
    pdf = BytesIO()
    writer.write(pdf)
    result = AttachmentReader.extract(Attachment(id="1", name="weekly.pdf"), pdf.getvalue())
    assert result.status == "extracted" and result.text == "Synthetic"
    invalid = AttachmentReader.extract(Attachment(id="1", name="bad.pdf"), b"not a pdf")
    assert invalid.status == "invalid_format"


@pytest.mark.parametrize("tool,args", [
    ("get_news", {}),
    ("get_attachments", {"child_id": "first", "news_id": "7", "attachment_id": "123"}),
])
def test_download_mode_is_rejected_without_auth_or_network(tool, args, httpx_mock):
    result = asyncio.run(McpApp().build().call_tool(tool, {**args, "attachment_mode": "download"}))
    assert result.structured_content["error"]["code"] == "unsupported_mode"
    assert not httpx_mock.get_requests()


def test_parallel_attachment_tools_queue_instead_of_failing_busy(fake_hub, store, docx):
    def news(request, child):
        return {"items": [{"id": 7, "attachments": [
            {"title": f"{id_}.docx", "url": URL.replace("123", id_)} for id_ in ("123", "124")
        ]}]}

    def download(request, child):
        time.sleep(0.02)
        return httpx.Response(200, content=docx)

    fake_hub.route("/Communication/News/GetNewsList", news)
    for id_ in ("123", "124"):
        fake_hub.route(f"/Resources/Resource/Download/{id_}", download, method="GET")

    async def scenario():
        server = McpApp(InfoMentorService(store)).build()
        results = await asyncio.gather(*[
            server.call_tool("get_attachments", {
                "child_id": "first", "news_id": "7", "attachment_id": id_,
            }) for id_ in ("123", "124")
        ])
        attachments = [r.structured_content["attachment"] for r in results]
        assert [a["id"] for a in attachments] == ["123", "124"]
        assert all(a["status"] == "extracted" for a in attachments)

    asyncio.run(scenario())
