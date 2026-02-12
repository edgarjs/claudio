#!/usr/bin/env python3
"""Tests for lib/whatsapp_api.py — WhatsApp Business API client."""

import io
import json
import os
import sys
import urllib.error
import urllib.request
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure project root is on sys.path so `from lib.util import ...` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.whatsapp_api import WhatsAppClient, _MAX_MEDIA_SIZE, _MAX_MESSAGE_LEN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(body, code=200):
    """Build a MagicMock that behaves like an HTTPResponse (context manager)."""
    if isinstance(body, dict):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.code = code
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _mock_http_error(code, body=""):
    """Build a urllib.error.HTTPError with a readable body."""
    if isinstance(body, dict):
        body = json.dumps(body)
    fp = io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body)
    return urllib.error.HTTPError(
        url="https://graph.facebook.com/v21.0/test",
        code=code,
        msg=f"HTTP {code}",
        hdrs={},
        fp=fp,
    )


def _client():
    return WhatsAppClient(
        phone_number_id="1234567890",
        access_token="TEST_ACCESS_TOKEN",
        bot_id="test",
    )


# ---------------------------------------------------------------------------
# WhatsAppClient.api_call
# ---------------------------------------------------------------------------

class TestApiCall:

    @patch("urllib.request.urlopen")
    def test_success_returns_parsed_json(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"messages": [{"id": "wamid.123"}]})
        result = _client().api_call("messages", data={"to": "123"})
        assert result == {"messages": [{"id": "wamid.123"}]}

    @patch("urllib.request.urlopen")
    def test_json_body_sent_correctly(self, mock_urlopen):
        """api_call with data dict should send JSON body with correct content-type."""
        mock_urlopen.return_value = _mock_response({"ok": True})
        _client().api_call("messages", data={"to": "123", "type": "text"})

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Content-type") == "application/json"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["to"] == "123"

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_429_retries(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [
            _mock_http_error(429, "rate limited"),
            _mock_response({"messages": [{"id": "wamid.456"}]}),
        ]
        result = _client().api_call("messages", data={"to": "123"})
        assert result == {"messages": [{"id": "wamid.456"}]}
        mock_sleep.assert_called_once_with(1)  # 2^0 = 1

    @patch("urllib.request.urlopen")
    def test_4xx_no_retry(self, mock_urlopen):
        err_body = json.dumps({"error": {"message": "bad request"}})
        mock_urlopen.side_effect = _mock_http_error(400, err_body)
        result = _client().api_call("messages", data={"to": "123"})
        assert result["error"]["message"] == "bad request"
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_auth_header_present(self, mock_urlopen):
        """Every request should carry the Bearer token."""
        mock_urlopen.return_value = _mock_response({"ok": True})
        _client().api_call("messages", data={"to": "123"})

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer TEST_ACCESS_TOKEN"

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_5xx_retries_with_backoff(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [
            _mock_http_error(500, "server error"),
            _mock_http_error(503, "unavailable"),
            _mock_response({"ok": True}),
        ]
        result = _client().api_call("messages", data={})
        assert result == {"ok": True}
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_all_retries_exhausted_returns_empty_dict(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = _mock_http_error(500, "fail")
        result = _client().api_call("messages", data={})
        # 5 attempts (initial + 4 retries)
        assert mock_urlopen.call_count == 5
        # Returns parsed body or empty dict
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# WhatsAppClient.send_message
# ---------------------------------------------------------------------------

class TestSendMessage:

    @patch.object(WhatsAppClient, "api_call")
    def test_normal_send(self, mock_api):
        mock_api.return_value = {"messages": [{"id": "wamid.123"}]}
        _client().send_message("1234567890", "Hello WhatsApp")
        mock_api.assert_called_once()
        payload = mock_api.call_args.kwargs["data"]
        assert payload["messaging_product"] == "whatsapp"
        assert payload["to"] == "1234567890"
        assert payload["text"]["body"] == "Hello WhatsApp"

    @patch.object(WhatsAppClient, "api_call")
    def test_4096_char_chunking(self, mock_api):
        """Messages longer than 4096 chars are split into chunks."""
        mock_api.return_value = {"messages": [{"id": "wamid.123"}]}
        text = "X" * 9000
        _client().send_message("1234567890", text)

        assert mock_api.call_count == 3  # ceil(9000/4096) = 3
        chunks = [c.kwargs["data"]["text"]["body"] for c in mock_api.call_args_list]
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 4096
        assert len(chunks[2]) == 808
        assert "".join(chunks) == text

    @patch.object(WhatsAppClient, "api_call")
    def test_reply_to_on_first_chunk_only(self, mock_api):
        mock_api.return_value = {"messages": [{"id": "wamid.123"}]}
        text = "Y" * 5000  # 2 chunks
        _client().send_message("1234567890", text, reply_to="wamid.orig")

        assert mock_api.call_count == 2
        first_payload = mock_api.call_args_list[0].kwargs["data"]
        second_payload = mock_api.call_args_list[1].kwargs["data"]
        assert first_payload["context"]["message_id"] == "wamid.orig"
        assert "context" not in second_payload

    @patch.object(WhatsAppClient, "api_call")
    def test_no_reply_to_when_none(self, mock_api):
        mock_api.return_value = {"messages": [{"id": "wamid.123"}]}
        _client().send_message("1234567890", "Hello")
        payload = mock_api.call_args.kwargs["data"]
        assert "context" not in payload


# ---------------------------------------------------------------------------
# WhatsAppClient.send_audio
# ---------------------------------------------------------------------------

class TestSendAudio:

    @patch.object(WhatsAppClient, "api_call")
    def test_upload_then_send_flow(self, mock_api, tmp_path):
        """send_audio should first upload media, then send a message referencing it."""
        audio_file = tmp_path / "voice.mp3"
        audio_file.write_bytes(b"ID3" + b"\x00" * 100)

        # First call: upload returns media_id
        # Second call: send message returns message_id
        mock_api.side_effect = [
            {"id": "media_999"},
            {"messages": [{"id": "wamid.456"}]},
        ]

        result = _client().send_audio("1234567890", str(audio_file), reply_to="wamid.orig")
        assert result is True
        assert mock_api.call_count == 2

        # First call is the media upload
        first_call = mock_api.call_args_list[0]
        assert first_call.args[0] == "media"
        assert first_call.kwargs.get("files") is not None

        # Second call is the message send
        second_call = mock_api.call_args_list[1]
        assert second_call.args[0] == "messages"
        payload = second_call.kwargs["data"]
        assert payload["audio"]["id"] == "media_999"
        assert payload["context"]["message_id"] == "wamid.orig"

    @patch.object(WhatsAppClient, "api_call")
    def test_upload_failure_returns_false(self, mock_api, tmp_path):
        audio_file = tmp_path / "voice.mp3"
        audio_file.write_bytes(b"ID3" + b"\x00" * 100)

        mock_api.return_value = {}  # No "id" key
        result = _client().send_audio("1234567890", str(audio_file))
        assert result is False
        assert mock_api.call_count == 1  # Only upload attempted

    @patch.object(WhatsAppClient, "api_call")
    def test_send_failure_returns_false(self, mock_api, tmp_path):
        audio_file = tmp_path / "voice.mp3"
        audio_file.write_bytes(b"ID3" + b"\x00" * 100)

        mock_api.side_effect = [
            {"id": "media_999"},  # Upload succeeds
            {},                   # Send fails (no messages key)
        ]
        result = _client().send_audio("1234567890", str(audio_file))
        assert result is False


# ---------------------------------------------------------------------------
# WhatsAppClient.mark_read
# ---------------------------------------------------------------------------

class TestMarkRead:

    @patch("urllib.request.urlopen")
    def test_fire_and_forget_never_raises(self, mock_urlopen):
        mock_urlopen.side_effect = RuntimeError("network down")
        # Should not raise
        _client().mark_read("wamid.123")

    @patch("urllib.request.urlopen")
    def test_correct_payload(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"success": True})
        _client().mark_read("wamid.123")

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload == {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": "wamid.123",
        }
        assert req.get_header("Authorization") == "Bearer TEST_ACCESS_TOKEN"
        assert req.get_header("Content-type") == "application/json"


# ---------------------------------------------------------------------------
# WhatsAppClient.download_media
# ---------------------------------------------------------------------------

class TestDownloadMedia:

    @patch("urllib.request.urlopen")
    def test_two_step_get_url_then_download(self, mock_urlopen, tmp_path):
        """download_media first resolves media_id to URL, then downloads."""
        out = str(tmp_path / "image.jpg")
        file_data = b"\xff\xd8\xff" + b"\x00" * 100

        # First call: get media URL
        meta_resp = _mock_response({"url": "https://cdn.whatsapp.net/file.jpg"})
        # Second call: download the file
        dl_resp = _mock_response(file_data)
        mock_urlopen.side_effect = [meta_resp, dl_resp]

        assert _client().download_media("media_123", out) is True
        assert mock_urlopen.call_count == 2
        with open(out, "rb") as f:
            assert f.read() == file_data

    @patch("urllib.request.urlopen")
    def test_size_validation_rejects_oversized(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "huge.bin")
        oversized = b"\x00" * (_MAX_MEDIA_SIZE + 1)

        mock_urlopen.side_effect = [
            _mock_response({"url": "https://cdn.whatsapp.net/big.bin"}),
            _mock_response(oversized),
        ]

        assert _client().download_media("media_123", out) is False
        # File should be cleaned up
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_empty_file_rejected(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "empty.bin")

        mock_urlopen.side_effect = [
            _mock_response({"url": "https://cdn.whatsapp.net/empty.bin"}),
            _mock_response(b""),
        ]

        assert _client().download_media("media_123", out) is False
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_magic_byte_validation_failure(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "bad.jpg")
        file_data = b"not an image at all"

        mock_urlopen.side_effect = [
            _mock_response({"url": "https://cdn.whatsapp.net/file.jpg"}),
            _mock_response(file_data),
        ]

        def fake_validate(path):
            return False

        assert _client().download_media("media_123", out, validate_fn=fake_validate) is False
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_https_url_validation(self, mock_urlopen, tmp_path):
        """Non-HTTPS URLs should be rejected."""
        out = str(tmp_path / "file.bin")

        mock_urlopen.side_effect = [
            _mock_response({"url": "http://insecure.example.com/file.bin"}),
        ]

        assert _client().download_media("media_123", out) is False
        # Only one call made (metadata), download should not be attempted
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_missing_url_in_metadata(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "file.bin")
        mock_urlopen.side_effect = [
            _mock_response({"id": "media_123"}),  # No "url" key
        ]
        assert _client().download_media("media_123", out) is False

    @patch("urllib.request.urlopen")
    def test_auth_header_on_both_requests(self, mock_urlopen, tmp_path):
        """Both the metadata request and download request carry auth."""
        out = str(tmp_path / "file.bin")
        file_data = b"\x00" * 10

        mock_urlopen.side_effect = [
            _mock_response({"url": "https://cdn.whatsapp.net/file.bin"}),
            _mock_response(file_data),
        ]
        _client().download_media("media_123", out)

        # Both requests should have the Authorization header
        for i in range(2):
            req = mock_urlopen.call_args_list[i][0][0]
            assert req.get_header("Authorization") == "Bearer TEST_ACCESS_TOKEN"


# ---------------------------------------------------------------------------
# Convenience download wrappers
# ---------------------------------------------------------------------------

class TestDownloadConvenience:

    @patch.object(WhatsAppClient, "download_media")
    def test_download_image_delegates_with_validate_fn(self, mock_dl):
        mock_dl.return_value = True
        _client().download_image("media_123", "/tmp/img.jpg")
        mock_dl.assert_called_once()
        args, kwargs = mock_dl.call_args
        assert args == ("media_123", "/tmp/img.jpg")
        from lib.util import validate_image_magic
        assert kwargs["validate_fn"] is validate_image_magic

    @patch.object(WhatsAppClient, "download_media")
    def test_download_document_has_no_validate_fn(self, mock_dl):
        mock_dl.return_value = True
        _client().download_document("media_123", "/tmp/doc.pdf")
        args, kwargs = mock_dl.call_args
        assert kwargs.get("validate_fn") is None

    @patch.object(WhatsAppClient, "download_media")
    def test_download_audio_delegates_with_validate_fn(self, mock_dl):
        mock_dl.return_value = True
        _client().download_audio("media_123", "/tmp/audio.ogg")
        from lib.util import validate_audio_magic
        assert mock_dl.call_args.kwargs["validate_fn"] is validate_audio_magic
