"""Speechmatics TTS and STT integration for Claudio.

Alternative speech provider to ElevenLabs. Stdlib only — no external dependencies.

TTS: https://preview.tts.speechmatics.com/generate/{voice_id}
STT: https://asr.api.speechmatics.com/v2/jobs/ (batch async)
"""

import json
import os
import re
import time
import urllib.request
import urllib.error

from lib.util import MultipartEncoder, log, log_error, strip_markdown

# -- Constants --

TTS_MAX_CHARS = 5000  # Conservative limit
STT_MAX_SIZE = 20 * 1024 * 1024  # 20 MB

TTS_API = "https://preview.tts.speechmatics.com/generate"
STT_API_TEMPLATE = "https://{region}.asr.api.speechmatics.com/v2"

_VOICE_ID_RE = re.compile(r'^[a-z]{1,32}$')

# WAV magic bytes: RIFF....WAVE
_WAV_MAGIC = b'RIFF'
_WAV_FORMAT = b'WAVE'

# Polling config for batch STT
STT_POLL_INTERVAL = 2  # seconds between polls
STT_POLL_MAX_WAIT = 120  # max seconds to wait for job completion


def _validate_wav_magic(path):
    """Validate that a file starts with WAV (RIFF/WAVE) magic bytes."""
    try:
        with open(path, 'rb') as f:
            header = f.read(12)
    except OSError:
        return False

    if len(header) < 12:
        return False

    return header[:4] == _WAV_MAGIC and header[8:12] == _WAV_FORMAT


def tts_convert(text, output_path, api_key, voice_id='sarah'):
    """Convert text to speech using Speechmatics TTS API.

    Args:
        text: Text to convert to speech.
        output_path: Path to write the WAV output file.
        api_key: Speechmatics API key.
        voice_id: Speechmatics voice ID (sarah, theo, megan, jack).

    Returns:
        True on success, False on failure.
    """
    if not api_key:
        log_error("tts", "api_key not provided")
        return False

    if not voice_id:
        log_error("tts", "voice_id not provided")
        return False

    if not _VOICE_ID_RE.match(voice_id):
        log_error("tts", "Invalid voice_id format")
        return False

    # Strip markdown formatting for cleaner speech
    text = strip_markdown(text)

    if not text or not text.strip():
        log_error("tts", "No text to convert after stripping markdown")
        return False

    # Truncate if over limit
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]
        log("tts", f"Text truncated to {TTS_MAX_CHARS} characters")

    url = f"{TTS_API}/{voice_id}?output_format=wav_16000"
    payload = json.dumps({"text": text}).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=payload,
        method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        error_detail = f"HTTP {e.code}"
        try:
            raw = e.read(500).decode('utf-8', errors='replace')
            parsed = json.loads(raw)
            msg = parsed.get('detail', parsed.get('message', ''))
            if isinstance(msg, str) and msg:
                error_detail = f"HTTP {e.code}: {msg[:100]}"
        except Exception:
            pass
        log_error("tts", f"Speechmatics TTS API error: {error_detail}")
        _safe_delete(output_path)
        return False
    except (urllib.error.URLError, OSError) as e:
        log_error("tts", f"Speechmatics TTS request failed: {type(e).__name__}")
        _safe_delete(output_path)
        return False

    # Write output file
    try:
        with open(output_path, 'wb') as f:
            f.write(data)
    except OSError as e:
        log_error("tts", f"Failed to write output file: {e}")
        return False

    # Validate output is actually WAV audio
    if not _validate_wav_magic(output_path):
        log_error("tts", "Speechmatics returned non-audio content")
        _safe_delete(output_path)
        return False

    file_size = os.path.getsize(output_path)
    log("tts", f"Generated voice audio: {file_size} bytes")
    return True


def stt_transcribe(audio_path, api_key, region='eu1', language='en'):
    """Transcribe audio using Speechmatics batch STT API.

    Submits a transcription job, polls for completion, and retrieves
    the plain-text transcript.

    Args:
        audio_path: Path to the audio file to transcribe.
        api_key: Speechmatics API key.
        region: API region (eu1, us1, au1).
        language: ISO language code for transcription.

    Returns:
        Transcription text on success, None on failure.
    """
    if not api_key:
        log_error("stt", "api_key not provided")
        return None

    if not os.path.isfile(audio_path):
        log_error("stt", f"Audio file not found: {audio_path}")
        return None

    try:
        file_size = os.path.getsize(audio_path)
    except OSError as e:
        log_error("stt", f"Cannot stat audio file: {e}")
        return None

    if file_size == 0:
        log_error("stt", f"Audio file is empty: {audio_path}")
        return None

    if file_size > STT_MAX_SIZE:
        log_error("stt",
                  f"Audio file too large: {file_size} bytes "
                  f"(max {STT_MAX_SIZE})")
        return None

    if not re.match(r'^[a-z]{2}[0-9]$', region):
        log_error("stt", f"Invalid region format: {region}")
        return None

    base_url = STT_API_TEMPLATE.format(region=region)

    # Step 1: Submit transcription job
    job_id = _submit_job(audio_path, api_key, base_url, language)
    if not job_id:
        return None

    # Step 2: Poll for completion
    if not _wait_for_job(job_id, api_key, base_url):
        return None

    # Step 3: Get transcript
    text = _get_transcript(job_id, api_key, base_url)
    if not text:
        log_error("stt", "Speechmatics STT returned empty transcription")
        return None

    log("stt",
        f"Transcribed {file_size} bytes of audio ({len(text)} chars)")

    return text


def _submit_job(audio_path, api_key, base_url, language):
    """Submit a batch transcription job. Returns job_id or None."""
    config_json = json.dumps({
        "type": "transcription",
        "transcription_config": {"language": language},
    })

    enc = MultipartEncoder()
    enc.add_file('data_file', audio_path)
    enc.add_field('config', config_json)
    body = enc.finish()

    url = f"{base_url}/jobs/"
    req = urllib.request.Request(
        url,
        data=body,
        method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': enc.content_type,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_data = resp.read()
    except urllib.error.HTTPError as e:
        error_detail = f"HTTP {e.code}"
        try:
            raw = e.read(500).decode('utf-8', errors='replace')
            parsed = json.loads(raw)
            msg = parsed.get('detail', parsed.get('message', ''))
            if isinstance(msg, str) and msg:
                error_detail = f"HTTP {e.code}: {msg[:100]}"
        except Exception:
            pass
        log_error("stt", f"Speechmatics job submit error: {error_detail}")
        return None
    except (urllib.error.URLError, OSError) as e:
        log_error("stt", f"Speechmatics job submit failed: {type(e).__name__}")
        return None

    try:
        result = json.loads(resp_data)
    except (json.JSONDecodeError, ValueError):
        log_error("stt", "Failed to parse job submit response")
        return None

    job_id = result.get('id', '')
    if not job_id:
        log_error("stt", "No job ID in submit response")
        return None

    log("stt", f"Submitted transcription job: {job_id}")
    return job_id


def _wait_for_job(job_id, api_key, base_url):
    """Poll job status until done. Returns True on success, False on failure."""
    url = f"{base_url}/jobs/{job_id}"
    deadline = time.monotonic() + STT_POLL_MAX_WAIT

    while time.monotonic() < deadline:
        req = urllib.request.Request(
            url,
            method='GET',
            headers={'Authorization': f'Bearer {api_key}'},
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, ValueError) as e:
            log_error("stt", f"Error polling job {job_id}: {e}")
            return False

        status = data.get('job', {}).get('status', '')

        if status == 'done':
            return True
        if status in ('rejected', 'deleted', 'expired'):
            log_error("stt", f"Job {job_id} failed with status: {status}")
            return False

        time.sleep(STT_POLL_INTERVAL)

    log_error("stt", f"Job {job_id} timed out after {STT_POLL_MAX_WAIT}s")
    return False


def _get_transcript(job_id, api_key, base_url):
    """Retrieve plain-text transcript for a completed job."""
    url = f"{base_url}/jobs/{job_id}/transcript?format=txt"
    req = urllib.request.Request(
        url,
        method='GET',
        headers={'Authorization': f'Bearer {api_key}'},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode('utf-8', errors='replace').strip()
    except urllib.error.HTTPError as e:
        log_error("stt", f"Speechmatics transcript fetch error: HTTP {e.code}")
        return None
    except (urllib.error.URLError, OSError) as e:
        log_error("stt", f"Speechmatics transcript fetch failed: {type(e).__name__}")
        return None

    return text


def _safe_delete(path):
    """Delete a file, ignoring errors if it does not exist."""
    try:
        os.unlink(path)
    except OSError:
        pass
