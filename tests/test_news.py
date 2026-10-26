import hashlib
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from infomentor_mcp.errors import InfoMentorError
from infomentor_mcp.features.attachments import AttachmentReader
from infomentor_mcp.features.news import News

NEWS = "/Communication/News/GetNewsList"
DOWNLOAD = "/Resources/Resource/Download/123"
URL = DOWNLOAD + "?api=IM2&ModuleType=NewsItem&ConnectionId=7"


@pytest.fixture
def docx():
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("word/document.xml", '''<w:document
          xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>Synthetic newsletter</w:t></w:r></w:p></w:body></w:document>''')
    return content.getvalue()


def news_with_attachments(request, child):
    return {"items": [{
        "id": id_, "publishedDate": date,
        "attachments": [{"title": "weekly.docx", "url": URL.replace("Id=7", f"Id={id_}")}],
    } for id_, date in [(7, "2026-10-22"), (8, "2026-10-01")]]}


def test_maps_news_to_the_requested_child(fake_hub, hub):
    fake_hub.route(NEWS, lambda request, child: {"items": [{
        "id": 7, "title": "Synthetic title", "content": "<p>Synthetic content</p>",
        "publishedDate": "2026-10-21T10:00:00",
    }]})
    news = News(hub).items(["first"])
    assert news[0].child_ids == ["first"]
    assert news[0].title == "Synthetic title"
    assert fake_hub.read_by(NEWS) == ["first"]


@pytest.mark.parametrize("requested, expected", [
    (None, ["second", "shared", "first", "undated"]),
    (["first", "second", "first"], ["second", "shared", "first", "undated"]),
    (["first"], ["shared", "first", "undated"]),
    ([], []),
])
def test_news_merges_child_ids_and_sorts(fake_hub, hub, requested, expected):
    def news(request, child):
        dates = {"first": "2026-10-01", "second": "2026-10-03", "shared": "2026-10-02"}
        ids = [child, "shared", "shared"] + (["undated"] if child == "first" else [])
        return {"items": [{"id": id_, "publishedDate": dates.get(id_)} for id_ in ids]}

    fake_hub.route(NEWS, news)
    items = News(hub).items(requested)
    assert [item.id for item in items] == expected
    reads = list(dict.fromkeys(requested)) if requested is not None else ["first", "second"]
    assert fake_hub.read_by(NEWS) == reads
    assert fake_hub.selected == "second"
    shared = next((item for item in items if item.id == "shared"), None)
    if shared:
        assert shared.child_ids == reads


@pytest.mark.parametrize("mode", ["metadata", "inline", "followup"])
def test_news_fetches_only_page_attachments_once_and_restores_selection(
    fake_hub, hub, monkeypatch, docx, mode,
):
    monkeypatch.setattr(AttachmentReader, "MAX_TOTAL_TEXT_CHARS", 9)
    fake_hub.route(NEWS, news_with_attachments)
    fake_hub.route(DOWNLOAD, lambda request, child: httpx.Response(200, content=docx),
                   method="GET")
    kwargs = {} if mode == "inline" else {"include_attachments": False}
    items = News(hub).items(limit=1, **kwargs)
    assert len(items) == 1
    assert items[0].child_ids == ["first", "second"]
    attachment = items[0].attachments[0]
    assert attachment.status == ("extracted" if mode == "inline" else "available")
    if mode == "inline":
        assert attachment.text == "Synthetic" and attachment.truncated
        assert attachment.content_sha256 == hashlib.sha256(docx).hexdigest()
    # Downloaded once, for the newest item only, while its first child was selected.
    assert fake_hub.read_by(DOWNLOAD) == (["first"] if mode == "inline" else [])
    if mode == "followup":
        assert News(hub).attachment("second", "7", "123").status == "extracted"
        assert fake_hub.read_by(DOWNLOAD) == ["second"]
        assert fake_hub.requests(DOWNLOAD)[0].url.params["ConnectionId"] == "7"
    assert fake_hub.selected == "second"


@pytest.mark.parametrize("headers,content,status", [
    ({"content-length": str(AttachmentReader.MAX_FILE_BYTES + 1)}, b"", "too_large"),
    ({}, b"broken zip", "invalid_format"),
])
def test_attachment_failures_are_per_file(fake_hub, hub, headers, content, status):
    fake_hub.route(NEWS, news_with_attachments)
    fake_hub.route(DOWNLOAD, lambda request, child: httpx.Response(
        200, headers=headers, content=content,
    ), method="GET")
    items = News(hub).items(["first"], limit=1)
    assert items[0].attachments[0].status == status
    assert items[0].attachments[0].text is None


def test_unknown_attachment_never_triggers_a_download(fake_hub, hub):
    fake_hub.route(NEWS, lambda request, child: {"items": []})
    with pytest.raises(InfoMentorError, match="not found"):
        News(hub).attachment("first", "7", "123")
    assert not fake_hub.requests(DOWNLOAD)
    assert fake_hub.selected == "second"
