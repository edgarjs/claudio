#!/usr/bin/env python3
"""Tests for lib/elevenlabs.py — ElevenLabs TTS and STT integration."""

import io
import json
import os
import sys
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch


# Ensure project root is on sys.path so `from lib.util import ...` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.elevenlabs import (
    TTS_MAX_CHARS,
    STT_MAX_SIZE,
    _validate_mp3_magic,
    tts_convert,
    stt_transcribe,
)


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
    fp = io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body)
    exc = urllib.error.HTTPError(
        url="https://api.elevenlabs.io/v1/test",
        code=code,
        msg=f"HTTP {code}",
        hdrs={},
        fp=fp,
    )
    return exc


# Valid MP3 data starting with ID3 header
_VALID_MP3 = b"ID3" + b"\x00" * 200


# ---------------------------------------------------------------------------
# tts_convert
# ---------------------------------------------------------------------------

class TestTtsConvert:

    @patch("urllib.request.urlopen")
    def test_success_writes_mp3(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.mp3")
        mock_urlopen.return_value = _mock_response(_VALID_MP3)

        result = tts_convert("Hello world", out, api_key="key123", voice_id="abc123")
        assert result is True
        assert os.path.isfile(out)
        with open(out, "rb") as f:
            assert f.read() == _VALID_MP3

    @patch("urllib.request.urlopen")
    def test_api_error_returns_false(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.mp3")
        mock_urlopen.side_effect = _mock_http_error(500, "server error")

        result = tts_convert("Hello", out, api_key="key123", voice_id="abc123")
        assert result is False
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_url_error_returns_false(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.mp3")
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        result = tts_convert("Hello", out, api_key="key123", voice_id="abc123")
        assert result is False

    def test_empty_text_after_markdown_stripping(self, tmp_path):
        """Text that becomes empty after markdown stripping should fail."""
        out = str(tmp_path / "output.mp3")
        # A code block with no other content
        result = tts_convert("```\ncode only\n```", out, api_key="key123", voice_id="abc123")
        assert result is False

    @patch("urllib.request.urlopen")
    def test_truncation_at_5000_chars(self, mock_urlopen, tmp_path):
        """Text exceeding TTS_MAX_CHARS should be truncated to 5000 chars."""
        out = str(tmp_path / "output.mp3")
        mock_urlopen.return_value = _mock_response(_VALID_MP3)
        long_text = "A" * 8000

        result = tts_convert(long_text, out, api_key="key123", voice_id="abc123")
        assert result is True

        # Verify the sent payload was truncated
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert len(payload["text"]) == TTS_MAX_CHARS

    def test_invalid_voice_id_rejected(self, tmp_path):
        """voice_id with non-alphanumeric characters should be rejected."""
        out = str(tmp_path / "output.mp3")
        assert tts_convert("Hello", out, api_key="key", voice_id="bad/id!") is False
        assert tts_convert("Hello", out, api_key="key", voice_id="") is False

    def test_invalid_model_rejected(self, tmp_path):
        """model with invalid characters should be rejected."""
        out = str(tmp_path / "output.mp3")
        assert tts_convert("Hello", out, api_key="key", voice_id="abc123",
                           model="bad model!") is False

    def test_missing_api_key_rejected(self, tmp_path):
        out = str(tmp_path / "output.mp3")
        assert tts_convert("Hello", out, api_key="", voice_id="abc123") is False

    @patch("urllib.request.urlopen")
    def test_non_audio_response_rejected(self, mock_urlopen, tmp_path):
        """If the API returns something that is not valid MP3, it should fail."""
        out = str(tmp_path / "output.mp3")
        # Return HTML error page (not MP3 magic bytes)
        mock_urlopen.return_value = _mock_response(b"<html>Error</html>")

        result = tts_convert("Hello", out, api_key="key123", voice_id="abc123")
        assert result is False
        # File should be cleaned up
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_correct_api_url_and_headers(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.mp3")
        mock_urlopen.return_value = _mock_response(_VALID_MP3)

        tts_convert("Hello", out, api_key="mykey", voice_id="v1", model="eleven_turbo_v2")

        req = mock_urlopen.call_args[0][0]
        assert "v1" in req.full_url
        assert "output_format=mp3_44100_128" in req.full_url
        assert req.get_header("Xi-api-key") == "mykey"
        assert req.get_header("Content-type") == "application/json"

        payload = json.loads(req.data.decode("utf-8"))
        assert payload["model_id"] == "eleven_turbo_v2"


# ---------------------------------------------------------------------------
# stt_transcribe
# ---------------------------------------------------------------------------

class TestSttTranscribe:

    @patch("urllib.request.urlopen")
    def test_success_returns_text(self, mock_urlopen, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        mock_urlopen.return_value = _mock_response({
            "text": "Hello world",
            "language_code": "en",
        })

        result = stt_transcribe(str(audio), api_key="key123")
        assert result == "Hello world"

    @patch("urllib.request.urlopen")
    def test_api_error_returns_none(self, mock_urlopen, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        mock_urlopen.side_effect = _mock_http_error(500, "internal error")

        result = stt_transcribe(str(audio), api_key="key123")
        assert result is None

    def test_empty_file_returns_none(self, tmp_path):
        audio = tmp_path / "empty.ogg"
        audio.write_bytes(b"")

        result = stt_transcribe(str(audio), api_key="key123")
        assert result is None

    def test_file_too_large_returns_none(self, tmp_path):
        audio = tmp_path / "huge.ogg"
        audio.write_bytes(b"\x00" * (STT_MAX_SIZE + 1))

        result = stt_transcribe(str(audio), api_key="key123")
        assert result is None

    def test_missing_file_returns_none(self):
        result = stt_transcribe("/nonexistent/audio.ogg", api_key="key123")
        assert result is None

    def test_missing_api_key_returns_none(self, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)
        result = stt_transcribe(str(audio), api_key="")
        assert result is None

    def test_invalid_model_returns_none(self, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)
        result = stt_transcribe(str(audio), api_key="key123", model="bad model!")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_empty_transcription_returns_none(self, mock_urlopen, tmp_path):
        """If the API returns empty text, should return None."""
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        mock_urlopen.return_value = _mock_response({"text": "", "language_code": "en"})

        result = stt_transcribe(str(audio), api_key="key123")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_correct_multipart_request(self, mock_urlopen, tmp_path):
        """Verify the multipart request carries the file and model_id."""
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 50)

        mock_urlopen.return_value = _mock_response({"text": "hello", "language_code": "en"})

        stt_transcribe(str(audio), api_key="key123", model="scribe_v1")

        req = mock_urlopen.call_args[0][0]
        assert "speech-to-text" in req.full_url
        assert req.get_header("Xi-api-key") == "key123"
        assert "multipart/form-data" in req.get_header("Content-type")
        # Body should contain the model_id field and file data
        body = req.data
        assert b"scribe_v1" in body
        assert b"OggS" in body

    @patch("urllib.request.urlopen")
    def test_url_error_returns_none(self, mock_urlopen, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)
        mock_urlopen.side_effect = urllib.error.URLError("network error")
        assert stt_transcribe(str(audio), api_key="key123") is None


# ---------------------------------------------------------------------------
# _validate_mp3_magic
# ---------------------------------------------------------------------------

class TestValidateMp3Magic:

    def test_id3_header(self, tmp_path):
        f = tmp_path / "id3.mp3"
        f.write_bytes(b"ID3\x04\x00" + b"\x00" * 100)
        assert _validate_mp3_magic(str(f)) is True

    def test_mpeg1_layer3_sync(self, tmp_path):
        f = tmp_path / "mpeg1.mp3"
        f.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert _validate_mp3_magic(str(f)) is True

    def test_mpeg2_layer3_sync(self, tmp_path):
        f = tmp_path / "mpeg2.mp3"
        f.write_bytes(b"\xff\xf3\x90\x00" + b"\x00" * 100)
        assert _validate_mp3_magic(str(f)) is True

    def test_mpeg25_layer3_sync(self, tmp_path):
        f = tmp_path / "mpeg25.mp3"
        f.write_bytes(b"\xff\xf2\x90\x00" + b"\x00" * 100)
        assert _validate_mp3_magic(str(f)) is True

    def test_adts_mpeg4_aac(self, tmp_path):
        f = tmp_path / "adts4.aac"
        f.write_bytes(b"\xff\xf1\x50\x00" + b"\x00" * 100)
        assert _validate_mp3_magic(str(f)) is True

    def test_adts_mpeg2_aac(self, tmp_path):
        f = tmp_path / "adts2.aac"
        f.write_bytes(b"\xff\xf9\x50\x00" + b"\x00" * 100)
        assert _validate_mp3_magic(str(f)) is True

    def test_invalid_bytes_rejected(self, tmp_path):
        f = tmp_path / "bad.bin"
        f.write_bytes(b"RIFF" + b"\x00" * 100)
        assert _validate_mp3_magic(str(f)) is False

    def test_empty_file_rejected(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert _validate_mp3_magic(str(f)) is False

    def test_too_short_file_rejected(self, tmp_path):
        f = tmp_path / "tiny.bin"
        f.write_bytes(b"\xff")
        assert _validate_mp3_magic(str(f)) is False

    def test_nonexistent_file_rejected(self):
        assert _validate_mp3_magic("/nonexistent/file.mp3") is False

    def test_plain_text_rejected(self, tmp_path):
        f = tmp_path / "text.txt"
        f.write_bytes(b"This is just plain text, not audio.")
        assert _validate_mp3_magic(str(f)) is False
