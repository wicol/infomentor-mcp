import asyncio
import time
from io import BytesIO
from zipfile import ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.attachments import MAX_FILE_BYTES, extract_text, parse_attachments
from infomentor_mcp.client.http import InfoMentorError
from infomentor_mcp.client.infomentor import InfoMentorClient
from infomentor_mcp.client.models import Attachment, Child
from infomentor_mcp.server import create_server

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
    attachment = parse_attachments("7", [{"title": "weekly.docx", "url": url}])[0]
    assert (attachment.status == "available") == valid
    assert attachment.id == ("123" if valid else None)


def test_docx_text_includes_tables_and_pdf_without_text_requires_ocr(docx, monkeypatch):
    result = extract_text(Attachment(id="123", name="weekly.docx"), docx)
    assert result.status == "extracted"
    assert result.text == "Synthetic newsletter\nTable text"
    monkeypatch.setattr("infomentor_mcp.client.attachments.MAX_TEXT_CHARS", 9)
    result = extract_text(Attachment(id="123", name="weekly.docx"), docx)
    assert result.truncated and result.text == "Synthetic"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = BytesIO()
    writer.write(pdf)
    result = extract_text(Attachment(id="1", name="scan.pdf"), pdf.getvalue())
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
    result = extract_text(Attachment(id="1", name="weekly.pdf"), pdf.getvalue())
    assert result.status == "extracted" and result.text == "Synthetic"
    assert extract_text(Attachment(id="1", name="bad.pdf"), b"not a pdf").status == "invalid_format"


@pytest.mark.parametrize("mode", ["metadata", "inline", "followup"])
def test_news_fetches_only_page_attachments_once_and_restores_selection(
    monkeypatch, httpx_mock, docx, mode,
):
    client = InfoMentorClient(AuthSession())
    children = [Child(id="first", name="Ada", selected=True), Child(id="second", name="Bo")]
    switches = []
    def discover():
        client._expected_names = {child.id: child.name for child in children}
        client._original_child_id = "first"
        return children

    monkeypatch.setattr(client, "_list_children", discover)
    monkeypatch.setattr(client, "_select_child", lambda id_, _: switches.append(id_))
    monkeypatch.setattr("infomentor_mcp.client.infomentor.MAX_TOTAL_TEXT_CHARS", 9)

    def news(child_id):
        return [client._parse_news_item(child_id, {
            "id": id_, "publishedDate": date,
            "attachments": [{"title": "weekly.docx", "url": URL.replace("Id=7", f"Id={id_}")}],
        }) for id_, date in [(7, "2026-10-22"), (8, "2026-10-01")]]

    monkeypatch.setattr(client, "_get_selected_child_news", news)
    if mode != "metadata":
        httpx_mock.add_response(url="https://hub.infomentor.se" + URL, content=docx)
    try:
        kwargs = {} if mode == "inline" else {"include_attachments": False}
        items = client.get_news(limit=1, **kwargs)
        assert len(items) == 1
        assert items[0].child_ids == ["first", "second"]
        expected_status = "extracted" if mode == "inline" else "available"
        assert items[0].attachments[0].status == expected_status
        if mode == "inline":
            assert items[0].attachments[0].text == "Synthetic"
            assert items[0].attachments[0].truncated
        if mode == "followup":
            assert not httpx_mock.get_requests()
            assert client.get_attachments("second", "7", "123").status == "extracted"
        assert len(httpx_mock.get_requests()) == int(mode != "metadata")
        assert switches[-1] == "first"
    finally:
        client.close()


@pytest.mark.parametrize("headers,content,status", [
    ({"content-length": str(MAX_FILE_BYTES + 1)}, b"", "too_large"),
    ({}, b"broken zip", "invalid_format"),
])
def test_attachment_failures_are_per_file(httpx_mock, headers, content, status):
    client = InfoMentorClient(AuthSession())
    httpx_mock.add_response(url="https://hub.infomentor.se" + URL, headers=headers, content=content)
    try:
        result = client._read_attachment("7", Attachment(id="123", name="weekly.docx"))
        assert result.status == status
        assert result.text is None
    finally:
        client.close()


def test_unknown_attachment_never_triggers_a_download(monkeypatch, httpx_mock):
    client = InfoMentorClient(AuthSession())
    monkeypatch.setattr(client, "_get_news", lambda *_: [])
    try:
        with pytest.raises(InfoMentorError, match="not found"):
            client.get_attachments("first", "7", "123")
        assert not httpx_mock.get_requests()
    finally:
        client.close()


@pytest.mark.parametrize("tool,args", [
    ("get_news", {}),
    ("get_attachments", {"child_id": "first", "news_id": "7", "attachment_id": "123"}),
])
def test_download_mode_is_rejected_without_auth_or_network(tool, args, httpx_mock):
    result = asyncio.run(create_server().call_tool(tool, {**args, "attachment_mode": "download"}))
    assert result.structured_content["error"]["code"] == "unsupported_mode"
    assert not httpx_mock.get_requests()


def test_parallel_attachment_tools_queue_instead_of_failing_busy():
    from infomentor_mcp.auth.session import SessionStore

    class FakeClient:
        def __init__(self, session):
            pass

        def get_attachments(self, child_id, news_id, attachment_id):
            time.sleep(0.02)
            return Attachment(id=attachment_id, status="extracted", text="Synthetic")

        def close(self):
            pass

    async def scenario():
        store = SessionStore()
        store.save(AuthSession())
        server = create_server(store, FakeClient)
        results = await asyncio.gather(*[
            server.call_tool("get_attachments", {
                "child_id": "first", "news_id": "7", "attachment_id": id_,
            }) for id_ in ("123", "124")
        ])
        assert [r.structured_content["attachment"]["id"] for r in results] == ["123", "124"]

    asyncio.run(scenario())
