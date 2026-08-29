"""Tests for Drive revision history and historical downloads."""

import base64
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gdrive.drive_helpers import (
    download_drive_revision_to_temp,
    list_drive_file_revisions_data,
)
from gdrive.drive_tools import (
    get_drive_file_content,
    get_drive_file_download_url,
    list_drive_file_revisions,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _mock_revision_service(list_response=None, get_response=None):
    service = Mock()
    revisions = Mock()
    service.revisions.return_value = revisions
    if list_response is not None:
        list_request = Mock()
        list_request.execute.return_value = list_response
        revisions.list.return_value = list_request
    if get_response is not None:
        get_request = Mock()
        get_request.execute.return_value = get_response
        revisions.get.return_value = get_request
    return service


@pytest.mark.asyncio
async def test_missing_revision_explains_compaction_without_reading_current_content():
    service = _mock_revision_service(get_response={})
    service.revisions().get().execute.side_effect = HttpError(
        Mock(status=404, reason="Not Found"),
        b'{"error":{"message":"Revision not found"}}',
    )
    with (
        patch(
            "gdrive.drive_helpers.resolve_drive_item",
            return_value=(
                "doc123",
                {"name": "Policy", "mimeType": "application/vnd.google-apps.document"},
            ),
        ),
        patch("gdrive.drive_helpers._download_http_request_to_temp") as download,
        pytest.raises(UserInputError, match="may have merged or removed"),
    ):
        await download_drive_revision_to_temp(service, "doc123", "old")
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_revisions_returns_paginated_drive_visible_metadata():
    service = _mock_revision_service(
        list_response={
            "nextPageToken": "next-token",
            "revisions": [
                {
                    "id": "10",
                    "modifiedTime": "2026-08-28T12:00:00Z",
                    "lastModifyingUser": {
                        "displayName": "Coworker",
                        "emailAddress": "coworker@example.com",
                    },
                    "mimeType": "application/vnd.google-apps.document",
                }
            ],
        }
    )

    with patch(
        "gdrive.drive_helpers.resolve_drive_item",
        return_value=(
            "doc123",
            {
                "name": "Policy",
                "mimeType": "application/vnd.google-apps.document",
            },
        ),
    ):
        result = await list_drive_file_revisions_data(
            service, "shortcut-or-id", page_size=25, page_token="page-1"
        )

    assert result["file"]["id"] == "doc123"
    assert result["next_page_token"] == "next-token"
    assert result["revisions"][0]["id"] == "10"
    assert result["revisions"][0]["last_modifying_user"]["display_name"] == "Coworker"
    assert result["revisions"][0]["download_available"] is True
    assert "not guaranteed" in result["history_scope"]
    service.revisions().list.assert_called_once_with(
        fileId="doc123",
        pageSize=25,
        pageToken="page-1",
        fields=(
            "nextPageToken, revisions(id,modifiedTime,"
            "lastModifyingUser(displayName,emailAddress),mimeType,size,"
            "keepForever,originalFilename)"
        ),
    )


@pytest.mark.asyncio
async def test_list_revisions_marks_only_head_or_pinned_blob_downloadable():
    service = _mock_revision_service(
        list_response={
            "revisions": [
                {"id": "1", "keepForever": False},
                {"id": "2", "keepForever": True},
                {"id": "3", "keepForever": False},
            ]
        }
    )

    with patch(
        "gdrive.drive_helpers.resolve_drive_item",
        return_value=(
            "file123",
            {
                "name": "report.pdf",
                "mimeType": "application/pdf",
                "headRevisionId": "3",
            },
        ),
    ):
        result = await list_drive_file_revisions_data(service, "file123")

    assert [r["download_available"] for r in result["revisions"]] == [False, True, True]
    assert [r["is_head"] for r in result["revisions"]] == [False, False, True]


@pytest.mark.asyncio
async def test_list_revisions_rejects_page_size_outside_range():
    with pytest.raises(UserInputError, match="page_size must be between 1 and 1000"):
        await list_drive_file_revisions_data(Mock(), "file123", page_size=0)


@pytest.mark.asyncio
async def test_list_revisions_tool_is_thin_wrapper():
    expected = {"file": {"id": "x"}, "revisions": []}
    service = Mock()
    with patch(
        "gdrive.drive_tools.list_drive_file_revisions_data",
        return_value=expected,
    ) as helper:
        result = await _unwrap(list_drive_file_revisions)(
            service=service,
            user_google_email="user@example.com",
            file_id="x",
            page_size=10,
            page_token="p",
        )

    assert result == expected
    helper.assert_awaited_once_with(service, "x", page_size=10, page_token="p")


@pytest.mark.asyncio
async def test_native_revision_download_uses_revision_export_link(tmp_path):
    service = _mock_revision_service(
        get_response={
            "id": "12",
            "modifiedTime": "2026-08-28T13:00:00Z",
            "mimeType": "application/vnd.google-apps.document",
            "exportLinks": {"application/pdf": "https://example.invalid/revision.pdf"},
        }
    )
    service._http = Mock()
    temp = tmp_path / "revision.pdf"
    temp.write_bytes(b"pdf")

    with (
        patch(
            "gdrive.drive_helpers.resolve_drive_item",
            return_value=(
                "doc123",
                {
                    "name": "Policy",
                    "mimeType": "application/vnd.google-apps.document",
                },
            ),
        ),
        patch(
            "gdrive.drive_helpers._download_http_request_to_temp",
            return_value=temp,
        ) as downloader,
    ):
        result = await download_drive_revision_to_temp(
            service, "doc123", "12", export_format="pdf"
        )

    request = downloader.call_args.args[0]
    assert request.uri == "https://example.invalid/revision.pdf"
    assert result["path"] == temp
    assert result["output_filename"] == "Policy_rev12.pdf"
    assert result["output_mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_native_revision_download_rejects_bad_export_format():
    service = _mock_revision_service(
        get_response={
            "id": "12",
            "mimeType": "application/vnd.google-apps.document",
            "exportLinks": {"application/pdf": "https://example.invalid/revision.pdf"},
        }
    )

    with (
        patch(
            "gdrive.drive_helpers.resolve_drive_item",
            return_value=(
                "doc123",
                {
                    "name": "Policy",
                    "mimeType": "application/vnd.google-apps.document",
                },
            ),
        ),
        pytest.raises(UserInputError, match="Unsupported export_format"),
    ):
        await download_drive_revision_to_temp(
            service, "doc123", "12", export_format="pdfx"
        )


@pytest.mark.asyncio
async def test_native_revision_download_rejects_missing_export_link():
    service = _mock_revision_service(
        get_response={
            "id": "12",
            "modifiedTime": "2026-08-28T13:00:00Z",
            "mimeType": "application/vnd.google-apps.document",
            "exportLinks": {"application/pdf": "https://example.invalid/revision.pdf"},
        }
    )

    with (
        patch(
            "gdrive.drive_helpers.resolve_drive_item",
            return_value=(
                "doc123",
                {
                    "name": "Policy",
                    "mimeType": "application/vnd.google-apps.document",
                },
            ),
        ),
        patch("gdrive.drive_helpers._download_http_request_to_temp") as downloader,
        pytest.raises(UserInputError, match="cannot be exported as docx"),
    ):
        await download_drive_revision_to_temp(
            service, "doc123", "12", export_format="docx"
        )

    downloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_blob_revision_download_rejects_unpinned_old_revision():
    service = _mock_revision_service(
        get_response={
            "id": "old",
            "mimeType": "application/pdf",
            "keepForever": False,
            "originalFilename": "old-name.pdf",
        }
    )

    with (
        patch(
            "gdrive.drive_helpers.resolve_drive_item",
            return_value=(
                "file123",
                {
                    "name": "new-name.pdf",
                    "mimeType": "application/pdf",
                    "headRevisionId": "head",
                },
            ),
        ),
        pytest.raises(UserInputError, match="not downloadable"),
    ):
        await download_drive_revision_to_temp(service, "file123", "old")

    service.revisions().get_media.assert_not_called()


@pytest.mark.asyncio
async def test_blob_revision_uses_revision_filename_and_mime(tmp_path):
    service = _mock_revision_service(
        get_response={
            "id": "old",
            "modifiedTime": "2026-08-28T14:00:00Z",
            "mimeType": "application/octet-stream",
            "keepForever": True,
            "originalFilename": "historic.bin",
        }
    )
    temp = tmp_path / "historic"
    temp.write_bytes(b"old")

    with (
        patch(
            "gdrive.drive_helpers.resolve_drive_item",
            return_value=(
                "file123",
                {
                    "name": "current.dat",
                    "mimeType": "application/octet-stream",
                    "headRevisionId": "head",
                },
            ),
        ),
        patch(
            "gdrive.drive_helpers._download_http_request_to_temp",
            return_value=temp,
        ),
    ):
        result = await download_drive_revision_to_temp(service, "file123", "old")

    service.revisions().get_media.assert_called_once_with(
        fileId="file123", revisionId="old"
    )
    assert result["output_filename"] == "historic_revold.bin"
    assert result["output_mime_type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_existing_download_tool_accepts_revision_id(tmp_path):
    temp = tmp_path / "revision.pdf"
    temp.write_bytes(b"history")
    download_result = {
        "path": temp,
        "file_id": "doc123",
        "file_name": "Policy",
        "revision_id": "12",
        "revision_modified_time": "2026-08-28T13:00:00Z",
        "output_filename": "Policy_rev12.pdf",
        "output_mime_type": "application/pdf",
    }

    class _Storage:
        def save_attachment_from_path(self, src_path, filename, mime_type):
            result = Mock()
            result.path = Path(src_path)
            result.file_id = "attachment"
            return result

    with (
        patch(
            "gdrive.drive_tools.download_drive_revision_to_temp",
            return_value=download_result,
        ) as helper,
        patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "doc123",
                {
                    "name": "Policy",
                    "mimeType": "application/vnd.google-apps.document",
                    "size": "100",
                },
            ),
        ),
        patch("gdrive.drive_tools.is_stateless_mode", return_value=False),
        patch("gdrive.drive_tools.get_transport_mode", return_value="stdio"),
        patch("gdrive.drive_tools.get_attachment_storage", return_value=_Storage()),
    ):
        result = await _unwrap(get_drive_file_download_url)(
            service=Mock(),
            user_google_email="user@example.com",
            file_id="doc123",
            export_format="pdf",
            revision_id="12",
        )

    helper.assert_awaited_once()
    assert "Revision: 12 (2026-08-28T13:00:00Z)" in result
    assert "Policy_rev12.pdf" not in result  # path remains transport-specific


@pytest.mark.asyncio
async def test_historical_content_passes_configured_cap_and_checks_before_read(
    tmp_path,
):
    temp = tmp_path / "revision.pdf"
    temp.write_bytes(b"larger-than-cap")
    service = Mock()
    download_result = {
        "path": temp,
        "file_id": "file123",
        "file_name": "report.pdf",
        "revision_id": "7",
        "revision_modified_time": "2026-08-28T13:00:00Z",
        "output_filename": "report_rev7.pdf",
        "output_mime_type": "application/pdf",
    }

    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "file123",
                {"name": "report.pdf", "mimeType": "application/pdf"},
            ),
        ),
        patch(
            "gdrive.drive_tools.download_drive_revision_to_temp",
            return_value=download_result,
        ) as helper,
        patch("gdrive.drive_tools.get_max_file_bytes", return_value=4),
        patch("pathlib.Path.read_bytes", side_effect=AssertionError("must not read")),
    ):
        result = await _unwrap(get_drive_file_content)(
            service=service,
            user_google_email="user@example.com",
            file_id="file123",
            revision_id="7",
        )

    helper.assert_awaited_once_with(
        service, "file123", "7", export_format=None, max_bytes=4
    )
    assert "too large" in result.lower()
    assert not temp.exists()


@pytest.mark.asyncio
async def test_historical_content_cleans_temp_file_when_parsing_fails(tmp_path):
    temp = tmp_path / "revision.pdf"
    temp.write_bytes(b"pdf")
    download_result = {
        "path": temp,
        "file_id": "file123",
        "file_name": "report.pdf",
        "revision_id": "7",
        "revision_modified_time": "2026-08-28T13:00:00Z",
        "output_filename": "report_rev7.pdf",
        "output_mime_type": "application/pdf",
    }

    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "file123",
                {"name": "report.pdf", "mimeType": "application/pdf"},
            ),
        ),
        patch(
            "gdrive.drive_tools.download_drive_revision_to_temp",
            return_value=download_result,
        ),
        patch(
            "gdrive.drive_tools.extract_pdf_text",
            side_effect=RuntimeError("parse failed"),
        ),
        pytest.raises(RuntimeError, match="parse failed"),
    ):
        await _unwrap(get_drive_file_content)(
            service=Mock(),
            user_google_email="user@example.com",
            file_id="file123",
            revision_id="7",
        )

    assert not temp.exists()


@pytest.mark.asyncio
async def test_historical_stateless_download_embeds_full_revision_and_cleans_temp(
    tmp_path,
):
    temp = tmp_path / "revision.pdf"
    content = b"historical complete payload"
    temp.write_bytes(content)
    download_result = {
        "path": temp,
        "file_id": "file123",
        "file_name": "report.pdf",
        "revision_id": "7",
        "revision_modified_time": "2026-08-28T13:00:00Z",
        "output_filename": "report_rev7.pdf",
        "output_mime_type": "application/pdf",
    }

    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "file123",
                {"name": "report.pdf", "mimeType": "application/pdf", "size": "1"},
            ),
        ),
        patch(
            "gdrive.drive_tools.download_drive_revision_to_temp",
            return_value=download_result,
        ) as helper,
        patch("gdrive.drive_tools.is_stateless_mode", return_value=True),
        patch("gdrive.drive_tools.gcs_files_enabled", return_value=False),
        patch("gdrive.drive_tools.get_stateless_inline_max_bytes", return_value=100),
    ):
        result = await _unwrap(get_drive_file_download_url)(
            service=Mock(),
            user_google_email="user@example.com",
            file_id="file123",
            revision_id="7",
        )

    helper.assert_awaited_once()
    assert "Revision: 7 (2026-08-28T13:00:00Z)" in result.content[0].text
    embedded = result.content[1].resource
    assert embedded.blob == base64.b64encode(content).decode("ascii")
    assert embedded.mime_type == "application/pdf"
    assert not temp.exists()


@pytest.mark.asyncio
async def test_historical_stateless_download_rejects_oversized_revision_and_cleans_temp(
    tmp_path,
):
    temp = tmp_path / "revision.pdf"
    temp.write_bytes(b"historical payload over inline limit")
    download_result = {
        "path": temp,
        "file_id": "file123",
        "file_name": "report.pdf",
        "revision_id": "7",
        "revision_modified_time": "2026-08-28T13:00:00Z",
        "output_filename": "report_rev7.pdf",
        "output_mime_type": "application/pdf",
    }

    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "file123",
                {"name": "report.pdf", "mimeType": "application/pdf", "size": "1"},
            ),
        ),
        patch(
            "gdrive.drive_tools.download_drive_revision_to_temp",
            return_value=download_result,
        ),
        patch("gdrive.drive_tools.is_stateless_mode", return_value=True),
        patch("gdrive.drive_tools.gcs_files_enabled", return_value=False),
        patch("gdrive.drive_tools.get_stateless_inline_max_bytes", return_value=4),
    ):
        result = await _unwrap(get_drive_file_download_url)(
            service=Mock(),
            user_google_email="user@example.com",
            file_id="file123",
            revision_id="7",
        )

    assert "exceeds the inline limit of 4 bytes" in result
    assert "Revision: 7 (2026-08-28T13:00:00Z)" in result
    assert not temp.exists()


@pytest.mark.asyncio
async def test_stateless_inline_zero_skips_historical_download():
    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "file123",
                {"name": "report.pdf", "mimeType": "application/pdf"},
            ),
        ),
        patch("gdrive.drive_tools.download_drive_revision_to_temp") as helper,
        patch("gdrive.drive_tools.is_stateless_mode", return_value=True),
        patch("gdrive.drive_tools.gcs_files_enabled", return_value=False),
        patch("gdrive.drive_tools.get_stateless_inline_max_bytes", return_value=0),
    ):
        result = await _unwrap(get_drive_file_download_url)(
            service=Mock(),
            user_google_email="user@example.com",
            file_id="file123",
            revision_id="7",
        )

    helper.assert_not_awaited()
    assert "Revision: 7 (modified time unavailable)" in result
    assert "inline returns are disabled" in result


@pytest.mark.asyncio
async def test_existing_content_tool_reads_historical_revision(tmp_path):
    temp = tmp_path / "revision.docx"
    temp.write_bytes(b"office-archive")
    service = Mock()
    download_result = {
        "path": temp,
        "file_id": "doc123",
        "file_name": "Policy",
        "revision_id": "12",
        "revision_modified_time": "2026-08-28T13:00:00Z",
        "output_filename": "Policy_rev12.docx",
        "output_mime_type": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    }

    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "doc123",
                {
                    "name": "Policy",
                    "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": "https://docs.google.com/document/d/doc123/edit",
                },
            ),
        ),
        patch(
            "gdrive.drive_tools.download_drive_revision_to_temp",
            return_value=download_result,
        ) as helper,
        patch(
            "gdrive.drive_tools.extract_office_xml_text",
            return_value="Historical text",
        ),
        patch("gdrive.drive_tools._download_file_bytes") as current_download,
    ):
        result = await _unwrap(get_drive_file_content)(
            service=service,
            user_google_email="user@example.com",
            file_id="doc123",
            revision_id="12",
        )

    helper.assert_awaited_once_with(
        service,
        "doc123",
        "12",
        export_format="docx",
        max_bytes=None,
    )
    current_download.assert_not_awaited()
    assert not temp.exists()
    assert "Revision: 12 (2026-08-28T13:00:00Z)" in result
    assert "Historical text" in result
