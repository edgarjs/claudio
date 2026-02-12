"""ElevenLabs TTS and STT integration for Claudio.

Ported from tts.sh and stt.sh. Stdlib only — no external dependencies.
All config is passed via function parameters for testability.
"""

import json
import os
import re
import urllib.request
import urllib.error

from lib.util import MultipartEncoder, log, log_error, strip_markdown

# -- Constants --

TTS_MAX_CHARS = 5000  # Conservative limit (API supports up to 10000)
STT_MAX_SIZE = 20 * 1024 * 1024  # 20 MB

ELEVENLABS_API = "https://api.elevenlabs.io/v1"

_VOICE_ID_RE = re.compile(r'^[a-zA-Z0-9]{1,64}$')
_MODEL_RE = re.compile(r'^[a-zA-Z0-9_]{1,64}$')

# MP3 magic bytes: ID3 tag, MPEG frame sync variants (MPEG1/2 Layer 2/3),
# and ADTS frame sync variants (AAC).
_MP3_MAGIC = (
    b'ID3',       # ID3v2 tag header
    b'\xff\xfb',  # MPEG1 Layer 3
    b'\xff\xf3',  # MPEG2 Layer 3
    b'\xff\xf2',  # MPEG2.5 Layer 3
    b'\xff\xf1',  # ADTS AAC (MPEG-4)
    b'\xff\xf9',  # ADTS AAC (MPEG-2)
)


def _validate_mp3_magic(path):
    """Validate that a file starts with MP3/ADTS magic bytes.

    Returns True if the file header matches any known MP3 or AAC/ADTS
    frame sync pattern.
    """
    try:
        with open(path, 'rb') as f:
            header = f.read(3)
    except OSError:
        return False

    if len(header) < 2:
        return False

    for magic in _MP3_MAGIC:
        if header[:len(magic)] == magic:
            return True

    return False


def tts_convert(text, output_path, api_key, voice_id,
                model='eleven_multilingual_v2'):
    """Convert text to speech using ElevenLabs API.

    Args:
        text: Text to convert to speech.
        output_path: Path to write the MP3 output file.
        api_key: ElevenLabs API key.
        voice_id: ElevenLabs voice ID.
        model: ElevenLabs TTS model ID.

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

    if not _MODEL_RE.match(model):
        log_error("tts", "Invalid model format")
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

    url = (f"{ELEVENLABS_API}/text-to-speech/{voice_id}"
           f"?output_format=mp3_44100_128")
    payload = json.dumps({"text": text, "model_id": model}).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=payload,
        method='POST',
        headers={
            'xi-api-key': api_key,
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
            detail = json.loads(raw).get('detail', {})
            if isinstance(detail, dict):
                error_detail = f"HTTP {e.code}: {detail.get('message', 'API error')[:100]}"
            elif isinstance(detail, str):
                error_detail = f"HTTP {e.code}: {detail[:100]}"
        except Exception:
            pass
        log_error("tts", f"ElevenLabs TTS API error: {error_detail}")
        _safe_delete(output_path)
        return False
    except (urllib.error.URLError, OSError) as e:
        log_error("tts", f"ElevenLabs TTS request failed: {type(e).__name__}")
        _safe_delete(output_path)
        return False

    # Write output file
    try:
        with open(output_path, 'wb') as f:
            f.write(data)
    except OSError as e:
        log_error("tts", f"Failed to write output file: {e}")
        return False

    # Validate output is actually audio
    if not _validate_mp3_magic(output_path):
        log_error("tts", "ElevenLabs returned non-audio content")
        _safe_delete(output_path)
        return False

    file_size = os.path.getsize(output_path)
    log("tts", f"Generated voice audio: {file_size} bytes")
    return True


def stt_transcribe(audio_path, api_key, model='scribe_v1'):
    """Transcribe audio using ElevenLabs Speech-to-Text API.

    Args:
        audio_path: Path to the audio file to transcribe.
        api_key: ElevenLabs API key.
        model: ElevenLabs STT model ID.

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

    if not _MODEL_RE.match(model):
        log_error("stt", "Invalid model format")
        return None

    # Build multipart request
    enc = MultipartEncoder()
    enc.add_file('file', audio_path)
    enc.add_field('model_id', model)
    body = enc.finish()

    url = f"{ELEVENLABS_API}/speech-to-text"
    req = urllib.request.Request(
        url,
        data=body,
        method='POST',
        headers={
            'xi-api-key': api_key,
            'Content-Type': enc.content_type,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_data = resp.read()
    except urllib.error.HTTPError as e:
        error_detail = f"HTTP {e.code}"
        try:
            raw = e.read(500).decode('utf-8', errors='replace')
            detail = json.loads(raw).get('detail', {})
            if isinstance(detail, dict):
                error_detail = f"HTTP {e.code}: {detail.get('message', 'API error')[:100]}"
            elif isinstance(detail, str):
                error_detail = f"HTTP {e.code}: {detail[:100]}"
        except Exception:
            pass
        log_error("stt", f"ElevenLabs STT API error: {error_detail}")
        return None
    except (urllib.error.URLError, OSError) as e:
        log_error("stt", f"ElevenLabs STT request failed: {type(e).__name__}")
        return None

    try:
        result = json.loads(resp_data)
    except (json.JSONDecodeError, ValueError) as e:
        log_error("stt", f"Failed to parse STT response: {e}")
        return None

    text = result.get('text') or ''
    if not text:
        log_error("stt", "ElevenLabs STT returned empty transcription")
        return None

    language = result.get('language_code', 'unknown')
    log("stt",
        f"Transcribed {file_size} bytes of audio "
        f"(language: {language}, {len(text)} chars)")

    return text


def _safe_delete(path):
    """Delete a file, ignoring errors if it does not exist."""
    try:
        os.unlink(path)
    except OSError:
        pass
