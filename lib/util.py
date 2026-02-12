"""Shared utilities for Claudio webhook handlers.

Functions ported from the duplicated code in telegram.sh and whatsapp.sh.
Stdlib only — no external dependencies.
"""

import io
import os
import re
import sys
import uuid

# -- Prompt sanitization --

# Matches XML-like tags (opening, closing, self-closing)
_TAG_RE = re.compile(r'</?[a-zA-Z_][a-zA-Z0-9_-]*[^>]*>')


def sanitize_for_prompt(text):
    """Strip XML-like tags that could be used for prompt injection.

    Mirrors _sanitize_for_prompt() in telegram.sh / whatsapp.sh.
    """
    return _TAG_RE.sub('[quoted text]', text)


def summarize(text, max_len=200):
    """Sanitize, collapse to single line, and truncate.

    Mirrors _summarize() in telegram.sh / whatsapp.sh.
    """
    s = sanitize_for_prompt(text)
    s = s.replace('\n', ' ')
    s = s.lstrip()
    s = re.sub(r'\s+', ' ', s)
    if len(s) > max_len:
        s = s[:max_len] + '...'
    return s


# -- Filename utilities --

# Only allow safe extension characters
_EXT_RE = re.compile(r'^[a-zA-Z0-9]+$')


def safe_filename_ext(filename):
    """Extract and validate a file extension from a filename.

    Returns the extension (without dot) or 'bin' if invalid/missing.
    """
    if not filename:
        return 'bin'
    _, _, ext = filename.rpartition('.')
    if not ext or ext == filename:
        return 'bin'
    if not _EXT_RE.match(ext) or len(ext) > 10:
        return 'bin'
    return ext


# Only allow safe characters in document names
_DOC_NAME_RE = re.compile(r'[^a-zA-Z0-9._ -]')


def sanitize_doc_name(name):
    """Clean a filename for safe inclusion in prompts.

    Strips characters that could break prompt framing or enable injection.
    Truncates to 255 characters.
    """
    if not name:
        return 'document'
    cleaned = _DOC_NAME_RE.sub('', name)[:255]
    return cleaned or 'document'


# -- Magic byte validation --

def validate_image_magic(path):
    """Validate that a file has image magic bytes (JPEG, PNG, GIF, WebP).

    Returns True if valid, False otherwise. Does NOT delete the file on failure.
    """
    try:
        with open(path, 'rb') as f:
            header = f.read(12)
    except OSError:
        return False

    if len(header) < 4:
        return False

    # JPEG
    if header[:3] == b'\xff\xd8\xff':
        return True
    # PNG
    if header[:4] == b'\x89PNG':
        return True
    # GIF
    if header[:4] == b'GIF8':
        return True
    # WebP: RIFF + 4 size bytes + WEBP
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return True

    return False


def validate_audio_magic(path):
    """Validate magic bytes for audio formats (OGG, MP3 with various headers).

    Returns True if valid, False otherwise.
    """
    try:
        with open(path, 'rb') as f:
            header = f.read(12)
    except OSError:
        return False

    if len(header) < 2:
        return False

    # OGG
    if header[:4] == b'OggS':
        return True
    # MP3 with ID3 tag
    if header[:3] == b'ID3':
        return True
    # MP3 frame sync variants
    if header[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
        return True

    return False


def validate_ogg_magic(path):
    """Validate that a file has OGG magic bytes (Telegram voice = OGG Opus).

    Returns True if valid, False otherwise.
    """
    try:
        with open(path, 'rb') as f:
            header = f.read(4)
    except OSError:
        return False

    return header == b'OggS'


# -- Logging --

def log_msg(module, msg, bot_id=None):
    """Format a log message with module and optional bot_id.

    Matches the log_msg() function in server.py.
    """
    if bot_id:
        return f"[{module}] [{bot_id}] {msg}\n"
    return f"[{module}] {msg}\n"


def log(module, msg, bot_id=None):
    """Write a log message to stderr."""
    sys.stderr.write(log_msg(module, msg, bot_id))


def log_error(module, msg, bot_id=None):
    """Write an error log message to stderr."""
    log(module, f"ERROR: {msg}", bot_id)


# -- Multipart form-data encoder --

class MultipartEncoder:
    """Encode multipart/form-data requests using stdlib only.

    Python's urllib has no built-in multipart support. This encoder handles
    both regular fields and file uploads, producing the body and content-type
    header needed for urllib.request.Request.

    Usage:
        enc = MultipartEncoder()
        enc.add_field('chat_id', '12345')
        enc.add_file('voice', '/path/to/audio.ogg', 'audio/ogg')
        body = enc.finish()
        content_type = enc.content_type
    """

    def __init__(self):
        self._boundary = uuid.uuid4().hex
        self._parts = []

    @property
    def content_type(self):
        return f'multipart/form-data; boundary={self._boundary}'

    def add_field(self, name, value):
        """Add a simple form field."""
        part = (
            f'--{self._boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f'\r\n'
            f'{value}\r\n'
        )
        self._parts.append(part.encode('utf-8'))

    def add_file(self, name, filepath, content_type='application/octet-stream', filename=None):
        """Add a file upload field."""
        if filename is None:
            filename = os.path.basename(filepath)

        header = (
            f'--{self._boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n'
            f'\r\n'
        )
        with open(filepath, 'rb') as f:
            file_data = f.read()

        self._parts.append(header.encode('utf-8') + file_data + b'\r\n')

    def add_file_data(self, name, data, content_type='application/octet-stream', filename='file'):
        """Add file data (bytes) as an upload field."""
        header = (
            f'--{self._boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n'
            f'\r\n'
        )
        self._parts.append(header.encode('utf-8') + data + b'\r\n')

    def finish(self):
        """Return the complete multipart body as bytes."""
        closing = f'--{self._boundary}--\r\n'.encode('utf-8')
        return b''.join(self._parts) + closing


# -- Temp file management --

def make_tmp_dir(claudio_path):
    """Create and return the tmp directory path under CLAUDIO_PATH."""
    tmp_dir = os.path.join(claudio_path, 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


# -- Markdown stripping for TTS --

def strip_markdown(text):
    """Strip markdown formatting for cleaner TTS output.

    Mirrors tts_strip_markdown() in tts.sh.
    """
    # Remove code blocks (``` ... ```)
    lines = text.split('\n')
    filtered = []
    in_code = False
    for line in lines:
        if line.strip().startswith('```'):
            in_code = not in_code
            continue
        if not in_code:
            filtered.append(line)
    text = '\n'.join(filtered)

    # Remove inline code
    text = re.sub(r'`[^`]*`', '', text)
    # Remove bold/italic (*** ... ***)
    text = re.sub(r'\*\*\*([^*]*)\*\*\*', r'\1', text)
    # Remove bold (** ... **)
    text = re.sub(r'\*\*([^*]*)\*\*', r'\1', text)
    # Remove italic (* ... *)
    text = re.sub(r'\*([^*]*)\*', r'\1', text)
    # Remove bold/italic (___ ... ___)
    text = re.sub(r'___([^_]*)___', r'\1', text)
    # Remove bold (__ ... __)
    text = re.sub(r'__([^_]*)__', r'\1', text)
    # Remove italic (_ ... _)
    text = re.sub(r'\b_([^_]*)_\b', r'\1', text)
    # Remove markdown links [text](url) -> text
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    # Remove list markers
    text = re.sub(r'^[ \t]*[-*+][ \t]', '  ', text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text
