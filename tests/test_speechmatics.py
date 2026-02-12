#!/usr/bin/env python3
"""Tests for lib/speechmatics.py — Speechmatics TTS and STT integration."""

import io
import json
import os
import sys
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.speechmatics import (
    TTS_MAX_CHARS,
    STT_MAX_SIZE,
    STT_POLL_INTERVAL,
    STT_POLL_MAX_WAIT,
    _validate_wav_magic,
    tts_convert,
    stt_transcribe,
    _submit_job,
    _wait_for_job,
    _get_transcript,
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
    return urllib.error.HTTPError(
        url="https://preview.tts.speechmatics.com/test",
        code=code,
        msg=f"HTTP {code}",
        hdrs={},
        fp=fp,
    )


# Valid WAV data: RIFF....WAVE header
_VALID_WAV = b'RIFF' + b'\x00\x00\x00\x00' + b'WAVE' + b'\x00' * 200


# ---------------------------------------------------------------------------
# tts_convert
# ---------------------------------------------------------------------------

class TestTtsConvert:

    @patch("urllib.request.urlopen")
    def test_success_writes_wav(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.wav")
        mock_urlopen.return_value = _mock_response(_VALID_WAV)

        result = tts_convert("Hello world", out, api_key="key123", voice_id="sarah")
        assert result is True
        assert os.path.isfile(out)
        with open(out, "rb") as f:
            assert f.read() == _VALID_WAV

    @patch("urllib.request.urlopen")
    def test_api_error_returns_false(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.wav")
        mock_urlopen.side_effect = _mock_http_error(500, "server error")

        result = tts_convert("Hello", out, api_key="key123", voice_id="sarah")
        assert result is False
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_url_error_returns_false(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.wav")
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        result = tts_convert("Hello", out, api_key="key123", voice_id="sarah")
        assert result is False

    def test_empty_text_after_markdown_stripping(self, tmp_path):
        out = str(tmp_path / "output.wav")
        result = tts_convert("```\ncode only\n```", out, api_key="key123", voice_id="sarah")
        assert result is False

    @patch("urllib.request.urlopen")
    def test_truncation_at_max_chars(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.wav")
        mock_urlopen.return_value = _mock_response(_VALID_WAV)
        long_text = "A" * 8000

        result = tts_convert(long_text, out, api_key="key123", voice_id="sarah")
        assert result is True

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert len(payload["text"]) == TTS_MAX_CHARS

    def test_invalid_voice_id_rejected(self, tmp_path):
        out = str(tmp_path / "output.wav")
        assert tts_convert("Hello", out, api_key="key", voice_id="Bad/ID!") is False
        assert tts_convert("Hello", out, api_key="key", voice_id="") is False
        assert tts_convert("Hello", out, api_key="key", voice_id="UPPER") is False

    def test_missing_api_key_rejected(self, tmp_path):
        out = str(tmp_path / "output.wav")
        assert tts_convert("Hello", out, api_key="", voice_id="sarah") is False

    @patch("urllib.request.urlopen")
    def test_non_audio_response_rejected(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.wav")
        mock_urlopen.return_value = _mock_response(b"<html>Error</html>")

        result = tts_convert("Hello", out, api_key="key123", voice_id="sarah")
        assert result is False
        assert not os.path.exists(out)

    @patch("urllib.request.urlopen")
    def test_correct_api_url_and_headers(self, mock_urlopen, tmp_path):
        out = str(tmp_path / "output.wav")
        mock_urlopen.return_value = _mock_response(_VALID_WAV)

        tts_convert("Hello", out, api_key="mykey", voice_id="theo")

        req = mock_urlopen.call_args[0][0]
        assert "theo" in req.full_url
        assert "output_format=wav_16000" in req.full_url
        assert req.get_header("Authorization") == "Bearer mykey"
        assert req.get_header("Content-type") == "application/json"

        payload = json.loads(req.data.decode("utf-8"))
        assert payload["text"] == "Hello"

    @patch("urllib.request.urlopen")
    def test_valid_voice_ids(self, mock_urlopen, tmp_path):
        """All documented voice IDs should be accepted."""
        out = str(tmp_path / "output.wav")
        mock_urlopen.return_value = _mock_response(_VALID_WAV)

        for voice in ("sarah", "theo", "megan", "jack"):
            result = tts_convert("Hello", out, api_key="key", voice_id=voice)
            assert result is True


# ---------------------------------------------------------------------------
# stt_transcribe
# ---------------------------------------------------------------------------

class TestSttTranscribe:

    @patch("lib.speechmatics.time.sleep")
    @patch("urllib.request.urlopen")
    def test_success_returns_text(self, mock_urlopen, mock_sleep, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        # Sequence: submit job -> poll (done) -> get transcript
        mock_urlopen.side_effect = [
            _mock_response({"id": "job123"}),
            _mock_response({"job": {"status": "done"}}),
            _mock_response("Hello world"),
        ]

        result = stt_transcribe(str(audio), api_key="key123")
        assert result == "Hello world"

    @patch("urllib.request.urlopen")
    def test_submit_error_returns_none(self, mock_urlopen, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        mock_urlopen.side_effect = _mock_http_error(500, "server error")

        result = stt_transcribe(str(audio), api_key="key123")
        assert result is None

    def test_empty_file_returns_none(self, tmp_path):
        audio = tmp_path / "empty.ogg"
        audio.write_bytes(b"")
        assert stt_transcribe(str(audio), api_key="key123") is None

    def test_file_too_large_returns_none(self, tmp_path):
        audio = tmp_path / "huge.ogg"
        audio.write_bytes(b"\x00" * (STT_MAX_SIZE + 1))
        assert stt_transcribe(str(audio), api_key="key123") is None

    def test_missing_file_returns_none(self):
        assert stt_transcribe("/nonexistent/audio.ogg", api_key="key123") is None

    def test_missing_api_key_returns_none(self, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)
        assert stt_transcribe(str(audio), api_key="") is None

    def test_invalid_region_returns_none(self, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)
        assert stt_transcribe(str(audio), api_key="key123", region="bad") is None
        assert stt_transcribe(str(audio), api_key="key123", region="EU1") is None

    @patch("lib.speechmatics.time.sleep")
    @patch("urllib.request.urlopen")
    def test_empty_transcription_returns_none(self, mock_urlopen, mock_sleep, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        mock_urlopen.side_effect = [
            _mock_response({"id": "job123"}),
            _mock_response({"job": {"status": "done"}}),
            _mock_response(""),
        ]

        result = stt_transcribe(str(audio), api_key="key123")
        assert result is None

    @patch("lib.speechmatics.time.sleep")
    @patch("urllib.request.urlopen")
    def test_polling_waits_for_done(self, mock_urlopen, mock_sleep, tmp_path):
        """Job that is 'running' then 'done' should succeed."""
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        mock_urlopen.side_effect = [
            _mock_response({"id": "job123"}),          # submit
            _mock_response({"job": {"status": "running"}}),  # poll 1
            _mock_response({"job": {"status": "running"}}),  # poll 2
            _mock_response({"job": {"status": "done"}}),     # poll 3
            _mock_response("Transcribed text"),               # transcript
        ]

        result = stt_transcribe(str(audio), api_key="key123")
        assert result == "Transcribed text"
        assert mock_sleep.call_count == 2  # slept during 'running' polls

    @patch("lib.speechmatics.time.sleep")
    @patch("urllib.request.urlopen")
    def test_rejected_job_returns_none(self, mock_urlopen, mock_sleep, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)

        mock_urlopen.side_effect = [
            _mock_response({"id": "job123"}),
            _mock_response({"job": {"status": "rejected"}}),
        ]

        result = stt_transcribe(str(audio), api_key="key123")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_correct_multipart_request(self, mock_urlopen, tmp_path):
        """Verify the multipart submit request carries the file and config."""
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 50)

        # Just test the submit step — make it fail after so we don't need all steps
        mock_urlopen.return_value = _mock_response({"id": "job123"})

        # Manually call _submit_job to inspect the request
        job_id = _submit_job(str(audio), "key123",
                             "https://eu1.asr.api.speechmatics.com/v2", "en")
        assert job_id == "job123"

        req = mock_urlopen.call_args[0][0]
        assert "/jobs/" in req.full_url
        assert req.get_header("Authorization") == "Bearer key123"
        assert "multipart/form-data" in req.get_header("Content-type")
        body = req.data
        assert b"OggS" in body
        assert b"transcription" in body

    @patch("urllib.request.urlopen")
    def test_url_error_returns_none(self, mock_urlopen, tmp_path):
        audio = tmp_path / "audio.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 100)
        mock_urlopen.side_effect = urllib.error.URLError("network error")
        assert stt_transcribe(str(audio), api_key="key123") is None


# ---------------------------------------------------------------------------
# _validate_wav_magic
# ---------------------------------------------------------------------------

class TestValidateWavMagic:

    def test_valid_wav(self, tmp_path):
        f = tmp_path / "test.wav"
        f.write_bytes(_VALID_WAV)
        assert _validate_wav_magic(str(f)) is True

    def test_mp3_rejected(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"ID3\x04\x00" + b"\x00" * 100)
        assert _validate_wav_magic(str(f)) is False

    def test_empty_file_rejected(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert _validate_wav_magic(str(f)) is False

    def test_too_short_file_rejected(self, tmp_path):
        f = tmp_path / "tiny.bin"
        f.write_bytes(b"RIFF\x00\x00")
        assert _validate_wav_magic(str(f)) is False

    def test_nonexistent_file_rejected(self):
        assert _validate_wav_magic("/nonexistent/file.wav") is False

    def test_riff_without_wave_rejected(self, tmp_path):
        """RIFF file that is not WAVE (e.g. AVI) should be rejected."""
        f = tmp_path / "test.avi"
        f.write_bytes(b'RIFF' + b'\x00\x00\x00\x00' + b'AVI ' + b'\x00' * 100)
        assert _validate_wav_magic(str(f)) is False

    def test_plain_text_rejected(self, tmp_path):
        f = tmp_path / "text.txt"
        f.write_bytes(b"This is just plain text, not audio.")
        assert _validate_wav_magic(str(f)) is False


# ---------------------------------------------------------------------------
# _wait_for_job
# ---------------------------------------------------------------------------

class TestWaitForJob:

    @patch("lib.speechmatics.time.sleep")
    @patch("lib.speechmatics.time.monotonic")
    @patch("urllib.request.urlopen")
    def test_timeout(self, mock_urlopen, mock_mono, mock_sleep):
        """Job that never completes should time out."""
        # Simulate time progressing past deadline
        mock_mono.side_effect = [0, 0, STT_POLL_MAX_WAIT + 1]
        mock_urlopen.return_value = _mock_response({"job": {"status": "running"}})

        result = _wait_for_job("job123", "key",
                               "https://eu1.asr.api.speechmatics.com/v2")
        assert result is False

    @patch("lib.speechmatics.time.sleep")
    @patch("urllib.request.urlopen")
    def test_poll_error_returns_false(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = urllib.error.URLError("network error")

        result = _wait_for_job("job123", "key",
                               "https://eu1.asr.api.speechmatics.com/v2")
        assert result is False


# ---------------------------------------------------------------------------
# _get_transcript
# ---------------------------------------------------------------------------

class TestGetTranscript:

    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response("Hello world")
        result = _get_transcript("job123", "key",
                                 "https://eu1.asr.api.speechmatics.com/v2")
        assert result == "Hello world"

        req = mock_urlopen.call_args[0][0]
        assert "format=txt" in req.full_url

    @patch("urllib.request.urlopen")
    def test_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = _mock_http_error(404, "not found")
        result = _get_transcript("job123", "key",
                                 "https://eu1.asr.api.speechmatics.com/v2")
        assert result is None
