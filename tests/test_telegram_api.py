#!/usr/bin/env python3
"""Tests for lib/telegram_api.py — Telegram Bot API client."""

import io
import json
import os
import sys
import urllib.error
import urllib.request
from unittest.mock import MagicMock, call, patch


# Ensure project root is on sys.path so `from lib.util import ...` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.telegram_api import TelegramClient, _MAX_FILE_SIZE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(body, code=200):
    """Build a MagicMock that behaves like an HTTPResponse from urlopen."""
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
        url="https://api.telegram.org/botTOKEN/test",
        code=code,
        msg=f"HTTP {code}",
        hdrs={},
        fp=fp,
    )


# ---------------------------------------------------------------------------
# TelegramClient.api_call
# ---------------------------------------------------------------------------

class TestApiCall:
    """Tests for the core api_call retry and response handling."""

    def _client(self):
        return TelegramClient("TEST_TOKEN", bot_id="test")

    @patch("urllib.request.urlopen")
    def test_success_returns_parsed_json(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"ok": True, "result": {}})
        result = self._client().api_call("getMe")
        assert result == {"ok": True, "result": {}}

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_429_retries_with_retry_after(self, mock_urlopen, mock_sleep):
        """429 with retry_after in body should sleep for that duration."""
        err_body = json.dumps({"parameters": {"retry_after": 7}})
        mock_urlopen.side_effect = [
            _mock_http_error(429, err_body),
            _mock_response({"ok": True}),
        ]
        result = self._client().api_call("sendMessage")
        assert result["ok"] is True
        mock_sleep.assert_called_once_with(7)

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_5xx_retries_with_backoff(self, mock_urlopen, mock_sleep):
        """5xx errors use exponential backoff: 1, 2, 4, 8."""
        mock_urlopen.side_effect = [
            _mock_http_error(500, "server error"),
            _mock_http_error(502, "bad gateway"),
            _mock_response({"ok": True}),
        ]
        result = self._client().api_call("sendMessage")
        assert result["ok"] is True
        # attempt 0 -> 2^0=1, attempt 1 -> 2^1=2
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("urllib.request.urlopen")
    def test_4xx_does_not_retry(self, mock_urlopen):
        """4xx errors (except 429) should not retry."""
        err_body = json.dumps({"ok": False, "error_code": 400, "description": "bad request"})
        mock_urlopen.side_effect = _mock_http_error(400, err_body)
        result = self._client().api_call("sendMessage")
        assert result["ok"] is False
        assert result["error_code"] == 400
        assert mock_urlopen.call_count == 1

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_network_error_retries(self, mock_urlopen, mock_sleep):
        """URLError (network failure) should retry with exponential backoff."""
        mock_urlopen.side_effect = [
            urllib.error.URLError("connection refused"),
            _mock_response({"ok": True}),
        ]
        result = self._client().api_call("sendMessage")
        assert result["ok"] is True
        mock_sleep.assert_called_once_with(1)

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_all_retries_exhausted(self, mock_urlopen, mock_sleep):
        """After max_retries+1 attempts, returns failure."""
        mock_urlopen.side_effect = _mock_http_error(500, '{"ok": false}')
        result = self._client().api_call("sendMessage")
        assert result["ok"] is False
        # 5 attempts total (initial + 4 retries)
        assert mock_urlopen.call_count == 5
        assert mock_sleep.call_count == 4

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_network_error_all_retries_exhausted(self, mock_urlopen, mock_sleep):
        """Network errors exhaust all retries and return {"ok": False}."""
        mock_urlopen.side_effect = urllib.error.URLError("timeout")
        result = self._client().api_call("sendMessage")
        assert result == {"ok": False}
        assert mock_urlopen.call_count == 5


# ---------------------------------------------------------------------------
# TelegramClient.send_message
# ---------------------------------------------------------------------------

class TestSendMessage:

    def _client(self):
        return TelegramClient("TEST_TOKEN", bot_id="test")

    @patch.object(TelegramClient, "api_call")
    def test_normal_send(self, mock_api):
        mock_api.return_value = {"ok": True}
        self._client().send_message("123", "Hello")
        mock_api.assert_called_once_with("sendMessage", data={
            "chat_id": "123",
            "text": "Hello",
            "parse_mode": "Markdown",
        })

    @patch.object(TelegramClient, "api_call")
    def test_chunking_splits_long_messages(self, mock_api):
        """Messages longer than 4096 chars should be split into multiple chunks."""
        mock_api.return_value = {"ok": True}
        text = "A" * 10000
        self._client().send_message("123", text)

        assert mock_api.call_count == 3  # ceil(10000/4096) = 3
        chunks = [c.kwargs["data"]["text"] for c in mock_api.call_args_list]
        assert chunks[0] == "A" * 4096
        assert chunks[1] == "A" * 4096
        assert chunks[2] == "A" * 1808
        # Reconstructed text matches original
        assert "".join(chunks) == text

    @patch.object(TelegramClient, "api_call")
    def test_markdown_fallback_to_plaintext(self, mock_api):
        """On Markdown failure, retries without parse_mode."""
        # First call (Markdown) fails, second (plaintext) succeeds
        mock_api.side_effect = [{"ok": False}, {"ok": True}]
        self._client().send_message("123", "Hello")

        assert mock_api.call_count == 2
        # First attempt includes parse_mode
        assert mock_api.call_args_list[0].kwargs["data"]["parse_mode"] == "Markdown"
        # Second attempt omits parse_mode
        assert "parse_mode" not in mock_api.call_args_list[1].kwargs["data"]

    @patch.object(TelegramClient, "api_call")
    def test_reply_to_on_first_chunk_only(self, mock_api):
        """reply_to should only appear on the first chunk."""
        mock_api.return_value = {"ok": True}
        text = "B" * 5000  # 2 chunks
        self._client().send_message("123", text, reply_to=42)

        assert mock_api.call_count == 2
        first_data = mock_api.call_args_list[0].kwargs["data"]
        second_data = mock_api.call_args_list[1].kwargs["data"]
        assert first_data["reply_to_message_id"] == 42
        assert "reply_to_message_id" not in second_data

    @patch.object(TelegramClient, "api_call")
    def test_fallback_drops_reply_to_on_third_attempt(self, mock_api):
        """Third fallback attempt drops reply_to entirely."""
        mock_api.side_effect = [
            {"ok": False},  # Markdown with reply_to
            {"ok": False},  # Plaintext with reply_to
            {"ok": True},   # Plaintext without reply_to
        ]
        self._client().send_message("123", "Hello", reply_to=42)

        assert mock_api.call_count == 3
        # Third attempt: no reply_to, no parse_mode
        third_data = mock_api.call_args_list[2].kwargs["data"]
        assert "reply_to_message_id" not in third_data
        assert "parse_mode" not in third_data


# ---------------------------------------------------------------------------
# TelegramClient.send_voice
# ---------------------------------------------------------------------------

class TestSendVoice:

    def _client(self):
        return TelegramClient("TEST_TOKEN", bot_id="test")

    @patch.object(TelegramClient, "api_call")
    def test_success_returns_true(self, mock_api):
        mock_api.return_value = {"ok": True}
        assert self._client().send_voice("123", "/tmp/voice.ogg") is True
        mock_api.assert_called_once()
        files_arg = mock_api.call_args.kwargs.get("files") or mock_api.call_args[1].get("files")
        assert files_arg["voice"] == "/tmp/voice.ogg"
        assert files_arg["chat_id"] == "123"

    @patch.object(TelegramClient, "api_call")
    def test_failure_returns_false(self, mock_api):
        mock_api.return_value = {"ok": False}
        assert self._client().send_voice("123", "/tmp/voice.ogg") is False


# ---------------------------------------------------------------------------
# TelegramClient.send_typing
# ---------------------------------------------------------------------------

class TestSendTyping:

    def _client(self):
        return TelegramClient("TEST_TOKEN", bot_id="test")

    @patch.object(TelegramClient, "api_call")
    def test_fire_and_forget_never_raises(self, mock_api):
        """send_typing must swallow all exceptions."""
        mock_api.side_effect = RuntimeError("boom")
        # Should not raise
        self._client().send_typing("123")

    @patch.object(TelegramClient, "api_call")
    def test_sends_correct_action(self, mock_api):
        mock_api.return_value = {"ok": True}
        self._client().send_typing("123", action="upload_photo")
        data = mock_api.call_args.kwargs["data"]
        assert data["chat_id"] == "123"
        assert data["action"] == "upload_photo"


# ---------------------------------------------------------------------------
# TelegramClient.set_reaction
# ---------------------------------------------------------------------------

class TestSetReaction:

    def _client(self):
        return TelegramClient("TEST_TOKEN", bot_id="test")

    @patch("urllib.request.urlopen")
    def test_fire_and_forget_never_raises(self, mock_urlopen):
        mock_urlopen.side_effect = RuntimeError("network down")
        # Should not raise
        self._client().set_reaction("123", 456)

    @patch("urllib.request.urlopen")
    def test_correct_json_payload(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"ok": True})
        self._client().set_reaction("123", 456, emoji="\U0001f44d")

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["chat_id"] == "123"
        assert payload["message_id"] == 456
        assert payload["reaction"] == [{"type": "emoji", "emoji": "\U0001f44d"}]
        assert req.get_header("Content-type") == "application/json"


# ---------------------------------------------------------------------------
# TelegramClient.download_file
# ---------------------------------------------------------------------------

class TestDownloadFile:

    def _client(self):
        return TelegramClient("TEST_TOKEN", bot_id="test")

    @patch("urllib.request.urlopen")
    def test_success_with_valid_file(self, mock_urlopen, tmp_path):
        """Downloads a file successfully with no validation."""
        out = str(tmp_path / "image.jpg")
        file_data = b"\xff\xd8\xff" + b"\x00" * 100

        # First call: getFile API
        get_file_resp = _mock_response({"ok": True, "result": {"file_path": "photos/file_1.jpg"}})
        # Second call: download the actual file
        download_resp = _mock_response(file_data)
        mock_urlopen.side_effect = [get_file_resp, download_resp]

        assert self._client().download_file("file123", out) is True
        assert os.path.isfile(out)
        with open(out, "rb") as f:
            assert f.read() == file_data

    @patch("urllib.request.urlopen")
    def test_magic_byte_validation_failure_deletes_file(self, mock_urlopen, tmp_path):
        """Failed magic byte validation should delete the written file."""
        out = str(tmp_path / "bad.jpg")
        file_data = b"this is not an image"

        get_file_resp = _mock_response({"ok": True, "result": {"file_path": "photos/file_1.jpg"}})
        download_resp = _mock_response(file_data)
        mock_urlopen.side_effect = [get_file_resp, download_resp]

        def fake_validate(path):
            return False

        assert self._client().download_file("file123", out, validate_fn=fake_validate) is False
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_invalid_file_path_rejected(self, mock_urlopen):
        """File paths with traversal or dangerous characters should be rejected."""
        get_file_resp = _mock_response({
            "ok": True,
            "result": {"file_path": "../../../etc/passwd"}
        })
        mock_urlopen.side_effect = [get_file_resp]

        assert self._client().download_file("file123", "/tmp/out") is False
        # Only one call (getFile), download should not have been attempted
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_file_path_with_special_chars_rejected(self, mock_urlopen):
        """File paths with shell metacharacters should be rejected."""
        get_file_resp = _mock_response({
            "ok": True,
            "result": {"file_path": "photos/file;rm -rf /.jpg"}
        })
        mock_urlopen.side_effect = [get_file_resp]

        assert self._client().download_file("file123", "/tmp/out") is False

    @patch("urllib.request.urlopen")
    def test_size_validation_rejects_oversized(self, mock_urlopen, tmp_path):
        """Files exceeding _MAX_FILE_SIZE should be rejected."""
        out = str(tmp_path / "big.bin")
        oversized_data = b"\x00" * (_MAX_FILE_SIZE + 1)

        get_file_resp = _mock_response({"ok": True, "result": {"file_path": "docs/big.bin"}})
        download_resp = _mock_response(oversized_data)
        mock_urlopen.side_effect = [get_file_resp, download_resp]

        assert self._client().download_file("file123", out) is False

    @patch("urllib.request.urlopen")
    def test_empty_file_rejected(self, mock_urlopen, tmp_path):
        """Zero-byte downloads should be rejected."""
        out = str(tmp_path / "empty.bin")

        get_file_resp = _mock_response({"ok": True, "result": {"file_path": "docs/empty.bin"}})
        download_resp = _mock_response(b"")
        mock_urlopen.side_effect = [get_file_resp, download_resp]

        assert self._client().download_file("file123", out) is False

    @patch("urllib.request.urlopen")
    def test_getfile_failure_returns_false(self, mock_urlopen):
        """If getFile returns no file_path, download should fail."""
        mock_urlopen.return_value = _mock_response({"ok": False})
        assert self._client().download_file("file123", "/tmp/out") is False


# ---------------------------------------------------------------------------
# Convenience download wrappers
# ---------------------------------------------------------------------------

class TestDownloadConvenience:

    def _client(self):
        return TelegramClient("TEST_TOKEN", bot_id="test")

    @patch.object(TelegramClient, "download_file")
    def test_download_image_delegates_with_validate_fn(self, mock_dl):
        mock_dl.return_value = True
        self._client().download_image("fid", "/tmp/img.jpg")
        mock_dl.assert_called_once()
        args, kwargs = mock_dl.call_args
        assert args == ("fid", "/tmp/img.jpg")
        # validate_fn should be validate_image_magic from util
        from lib.util import validate_image_magic
        assert kwargs["validate_fn"] is validate_image_magic

    @patch.object(TelegramClient, "download_file")
    def test_download_voice_delegates_with_ogg_validate(self, mock_dl):
        mock_dl.return_value = True
        self._client().download_voice("fid", "/tmp/voice.ogg")
        from lib.util import validate_ogg_magic
        assert mock_dl.call_args.kwargs["validate_fn"] is validate_ogg_magic

    @patch.object(TelegramClient, "download_file")
    def test_download_document_has_no_validate_fn(self, mock_dl):
        mock_dl.return_value = True
        self._client().download_document("fid", "/tmp/doc.pdf")
        # No validate_fn keyword should be passed (defaults to None)
        args, kwargs = mock_dl.call_args
        assert kwargs.get("validate_fn") is None


# ---------------------------------------------------------------------------
# TelegramClient._build_request
# ---------------------------------------------------------------------------

class TestBuildRequest:

    def _client(self):
        return TelegramClient("TEST_TOKEN")

    def test_get_without_body(self):
        req = self._client()._build_request("https://example.com/api")
        assert req.get_method() == "GET"
        assert req.data is None

    def test_url_encoded_with_data(self):
        req = self._client()._build_request(
            "https://example.com/api",
            data={"chat_id": "123", "text": "hi"},
        )
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        body = req.data.decode("utf-8")
        assert "chat_id=123" in body
        assert "text=hi" in body

    def test_multipart_with_files(self, tmp_path):
        """When files dict contains actual file paths, builds multipart."""
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        req = self._client()._build_request(
            "https://example.com/api",
            files={"voice": str(audio), "chat_id": "123"},
        )
        assert req.get_method() == "POST"
        assert "multipart/form-data" in req.get_header("Content-type")
        # Body should contain both the file data and the chat_id field
        body = req.data
        assert b"OggS" in body
        assert b"123" in body


# ---------------------------------------------------------------------------
# TelegramClient._retry_delay
# ---------------------------------------------------------------------------

class TestRetryDelay:

    def test_429_with_retry_after(self):
        body = json.dumps({"parameters": {"retry_after": 10}})
        assert TelegramClient._retry_delay(429, body, 0) == 10

    def test_429_without_retry_after_falls_back(self):
        """If 429 body has no parseable retry_after, use exponential backoff."""
        assert TelegramClient._retry_delay(429, "{}", 2) == 4  # 2^2

    def test_429_with_invalid_json(self):
        assert TelegramClient._retry_delay(429, "not json", 1) == 2  # 2^1

    def test_exponential_backoff_attempt_0(self):
        assert TelegramClient._retry_delay(500, "", 0) == 1  # 2^0

    def test_exponential_backoff_attempt_3(self):
        assert TelegramClient._retry_delay(503, "", 3) == 8  # 2^3

    def test_429_retry_after_zero_uses_backoff(self):
        """retry_after < 1 should fall back to exponential backoff."""
        body = json.dumps({"parameters": {"retry_after": 0}})
        assert TelegramClient._retry_delay(429, body, 2) == 4
