"""Webhook orchestrator for Claudio.

Routes webhooks to the appropriate platform handler and runs the unified
message processing pipeline. Replaces the telegram.sh and whatsapp.sh
Bash webhook handlers.

Entry point: process_webhook(body, bot_id, platform, bot_config_dict)
"""

import json
import os
import sqlite3
import tempfile
import threading

from lib.config import BotConfig, parse_env_file
from lib.util import (
    log, log_error,
    sanitize_for_prompt, summarize,
    safe_filename_ext, sanitize_doc_name,
    make_tmp_dir,
)
from lib.telegram_api import TelegramClient
from lib.whatsapp_api import WhatsAppClient
from lib.elevenlabs import tts_convert, stt_transcribe
from lib.claude_runner import run_claude

# -- Constants --

CLAUDIO_PATH = os.path.join(os.path.expanduser('~'), '.claudio')
_MODULE = 'handler'

# Cached service env (loaded once, thread-safe)
_service_env = None
_service_env_lock = threading.Lock()


def _load_service_env():
    """Load and cache ~/.claudio/service.env."""
    global _service_env
    if _service_env is not None:
        return _service_env
    with _service_env_lock:
        if _service_env is not None:
            return _service_env
        _service_env = parse_env_file(os.path.join(CLAUDIO_PATH, 'service.env'))
        return _service_env


# -- Parsed message --

class ParsedMessage:
    """Parsed webhook message, platform-independent."""
    __slots__ = (
        'chat_id', 'message_id', 'text', 'caption',
        'image_file_id', 'image_ext', 'extra_photos',
        'doc_file_id', 'doc_mime', 'doc_filename',
        'voice_file_id',
        'reply_to_text', 'reply_to_from', 'context_id',
        'message_type',
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot, ''))
        # Ensure extra_photos is always a list
        if not self.extra_photos:
            self.extra_photos = []

    @property
    def has_image(self):
        return bool(self.image_file_id)

    @property
    def has_document(self):
        return bool(self.doc_file_id)

    @property
    def has_voice(self):
        return bool(self.voice_file_id)


# -- Parse functions --

def _parse_telegram(body):
    """Parse a Telegram webhook body into a ParsedMessage."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None

    msg = data.get('message', {})
    if not msg:
        return None

    chat_id = str(msg.get('chat', {}).get('id', ''))
    if not chat_id:
        return None

    message_id = str(msg.get('message_id', ''))
    text = msg.get('text', '')
    caption = msg.get('caption', '')

    # Photo — last element has highest resolution
    photo = msg.get('photo', [])
    photo_file_id = photo[-1]['file_id'] if photo else ''

    # Document
    doc = msg.get('document', {}) or {}
    doc_file_id = doc.get('file_id', '')
    doc_mime = doc.get('mime_type', '')
    doc_filename = doc.get('file_name', '')

    # Voice
    voice = msg.get('voice', {}) or {}
    voice_file_id = voice.get('file_id', '')

    # Reply context
    reply_to = msg.get('reply_to_message') or {}
    reply_to_text = reply_to.get('text', '')
    reply_to_from = (reply_to.get('from') or {}).get('first_name', '')

    # Extra photos from media group merge (injected by server.py _merge_media_group)
    extra_photos = msg.get('_extra_photos', [])

    # Determine image info: compressed photo takes priority, then image document
    image_file_id = ''
    image_ext = 'jpg'
    if photo_file_id:
        image_file_id = photo_file_id
    elif doc_file_id and doc_mime.startswith('image/'):
        image_file_id = doc_file_id
        image_ext = {
            'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp',
        }.get(doc_mime, 'jpg')
        # Treated as image, not document
        doc_file_id = ''
        doc_mime = ''
        doc_filename = ''

    return ParsedMessage(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        caption=caption,
        image_file_id=image_file_id,
        image_ext=image_ext,
        extra_photos=extra_photos,
        doc_file_id=doc_file_id,
        doc_mime=doc_mime,
        doc_filename=doc_filename,
        voice_file_id=voice_file_id,
        reply_to_text=reply_to_text,
        reply_to_from=reply_to_from,
        context_id='',
        message_type='',
    )


def _parse_whatsapp(body):
    """Parse a WhatsApp webhook body into a ParsedMessage."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None

    entry = data.get('entry', [])
    if not entry:
        return None
    changes = entry[0].get('changes', [])
    if not changes:
        return None
    value = changes[0].get('value', {})
    messages = value.get('messages', [])
    if not messages:
        return None

    msg = messages[0]
    from_number = msg.get('from', '')
    if not from_number:
        return None

    message_id = msg.get('id', '')
    message_type = msg.get('type', '')

    # Text body (only present for text messages)
    text_obj = msg.get('text')
    text = text_obj.get('body', '') if isinstance(text_obj, dict) else ''

    # Image
    image = msg.get('image') or {}
    image_id = image.get('id', '')
    image_caption = image.get('caption', '')

    # Document
    document = msg.get('document') or {}
    doc_id = document.get('id', '')
    doc_filename = document.get('filename', '')
    doc_mime = document.get('mime_type', '')

    # Audio / Voice (combined — both transcribed the same way)
    audio_id = (msg.get('audio') or {}).get('id', '') or \
               (msg.get('voice') or {}).get('id', '')

    # Reply context
    context_id = (msg.get('context') or {}).get('id', '')

    return ParsedMessage(
        chat_id=from_number,
        message_id=message_id,
        text=text,
        caption=image_caption,
        image_file_id=image_id,
        image_ext='jpg',
        extra_photos=[],
        doc_file_id=doc_id,
        doc_mime=doc_mime,
        doc_filename=doc_filename,
        voice_file_id=audio_id,
        reply_to_text='',
        reply_to_from='',
        context_id=context_id,
        message_type=message_type,
    )


# -- Commands --

def _handle_command(text, config, client, target, message_id):
    """Handle slash commands. Returns True if handled."""
    text = (text or '').strip()

    if text in ('/opus', '/sonnet', '/haiku'):
        model = text[1:]
        try:
            config.save_model(model)
        except ValueError:
            return False
        client.send_message(target, f"_Switched to {model.capitalize()} model._",
                            reply_to=message_id)
        return True

    if text == '/start':
        client.send_message(
            target,
            "_Hola!_ Send me a message and I'll forward it to Claude Code.",
            reply_to=message_id,
        )
        return True

    return False


# -- Direct database access (replaces subprocess to db.py) --

def _db_init(db_file):
    """Ensure messages and token_usage tables exist."""
    conn = sqlite3.connect(db_file, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _history_add(db_file, role, content):
    """Insert a message into conversation history."""
    conn = sqlite3.connect(db_file, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    try:
        conn.execute(
            "INSERT INTO messages (role, content) VALUES (?, ?)",
            (role, content),
        )
        conn.commit()
    finally:
        conn.close()


def _history_get_context(db_file, limit):
    """Get conversation history as a formatted context string."""
    conn = sqlite3.connect(db_file, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    try:
        rows = conn.execute(
            "SELECT role, content FROM "
            "(SELECT role, content, id FROM messages ORDER BY id DESC LIMIT ?) "
            "ORDER BY id ASC",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return ''

    lines = []
    for role, content in rows:
        prefix = 'H' if role == 'user' else 'A'
        lines.append(f"{prefix}: {content}")

    return (
        "Here is the recent conversation history for context:\n\n"
        + "\n\n".join(lines)
        + "\n\n"
    )


# -- Memory integration (via daemon socket) --

def _memory_retrieve(query):
    """Retrieve relevant memories via the memory daemon."""
    from lib.memory import _try_daemon
    resp = _try_daemon({"command": "retrieve", "query": query, "top_k": 5})
    if resp and "result" in resp:
        return resp["result"]
    return ''


def _memory_consolidate():
    """Trigger memory consolidation via daemon (best-effort, background)."""
    from lib.memory import _try_daemon
    try:
        _try_daemon({"command": "consolidate", "_timeout": 150})
    except Exception:
        pass


# -- Main entry point --

def process_webhook(body, bot_id, platform, bot_config_dict):
    """Process a webhook message through the full pipeline.

    Args:
        body: Raw webhook body string.
        bot_id: Bot identifier string.
        platform: "telegram" or "whatsapp".
        bot_config_dict: Dict from server.py's bots or whatsapp_bots registry.
    """
    service_env = _load_service_env()
    config = BotConfig.from_bot_config(bot_id, bot_config_dict, service_env)

    # Parse webhook body
    if platform == 'telegram':
        msg = _parse_telegram(body)
    elif platform == 'whatsapp':
        msg = _parse_whatsapp(body)
    else:
        log_error(_MODULE, f"Unknown platform: {platform}", bot_id=bot_id)
        return

    if msg is None:
        return

    # Authorization — fail closed if not configured
    if platform == 'telegram':
        if not config.telegram_chat_id:
            log_error(_MODULE, "TELEGRAM_CHAT_ID not configured — rejecting all messages",
                      bot_id=bot_id)
            return
        if msg.chat_id != config.telegram_chat_id:
            log(_MODULE, f"Rejected message from unauthorized chat_id: {msg.chat_id}",
                bot_id=bot_id)
            return
    elif platform == 'whatsapp':
        if not config.whatsapp_phone_number:
            log_error(_MODULE, "WHATSAPP_PHONE_NUMBER not configured — rejecting all messages",
                      bot_id=bot_id)
            return
        if msg.chat_id != config.whatsapp_phone_number:
            log(_MODULE, f"Rejected message from unauthorized number: {msg.chat_id}",
                bot_id=bot_id)
            return

    # Build platform client
    if platform == 'telegram':
        client = TelegramClient(config.telegram_token, bot_id=bot_id)
    else:
        client = WhatsAppClient(
            config.whatsapp_phone_number_id,
            config.whatsapp_access_token,
            bot_id=bot_id,
        )

    # WhatsApp: reject unsupported message types early
    if platform == 'whatsapp' and msg.message_type not in (
        'text', 'image', 'document', 'audio', 'voice',
    ):
        log(_MODULE, f"Unsupported WhatsApp message type: {msg.message_type}",
            bot_id=bot_id)
        client.send_message(
            msg.chat_id,
            "Sorry, I don't support that message type yet.",
            reply_to=msg.message_id,
        )
        return

    # Effective text: message text, falling back to caption
    text = msg.text or msg.caption

    # Must have text, image, document, or voice
    if not text and not msg.has_image and not msg.has_document and not msg.has_voice:
        return

    # Command check — before reply context injection so commands work as replies
    if _handle_command(text, config, client, msg.chat_id, msg.message_id):
        return

    # Reply context injection
    if text:
        if platform == 'telegram' and msg.reply_to_text:
            reply_from = sanitize_for_prompt(msg.reply_to_from or 'someone')
            sanitized_reply = sanitize_for_prompt(msg.reply_to_text)
            text = f'[Replying to {reply_from}: "{sanitized_reply}"]\n\n{text}'
        elif platform == 'whatsapp' and msg.context_id:
            text = f'[Replying to a previous message]\n\n{text}'

    log(_MODULE,
        f"Received message from "
        f"{'chat_id' if platform == 'telegram' else 'number'}={msg.chat_id}",
        bot_id=bot_id)

    # Acknowledge receipt
    if platform == 'telegram':
        client.set_reaction(msg.chat_id, msg.message_id)
    else:
        client.mark_read(msg.message_id)

    # Run the processing pipeline with temp file cleanup
    _process_message(msg, text, config, client, platform, bot_id)


def _process_message(msg, text, config, client, platform, bot_id):
    """Run the message processing pipeline: download, transcribe, invoke Claude, respond."""
    tmp_files = []
    typing_stop = threading.Event()
    typing_thread = None
    has_voice = False
    transcription = ''
    voice_label = 'voice' if platform == 'telegram' else 'audio'

    try:
        tmp_dir = make_tmp_dir(CLAUDIO_PATH)

        # -- Media downloads --

        image_file = ''
        extra_image_files = []
        if msg.has_image:
            fd, image_file = tempfile.mkstemp(
                prefix='claudio-img-', suffix=f'.{msg.image_ext}', dir=tmp_dir,
            )
            os.close(fd)
            os.chmod(image_file, 0o600)
            tmp_files.append(image_file)

            ok = client.download_image(msg.image_file_id, image_file)
            if not ok:
                client.send_message(
                    msg.chat_id,
                    "Sorry, I couldn't download your image. Please try again.",
                    reply_to=msg.message_id,
                )
                return

            # Extra photos from media group (Telegram only)
            for fid in msg.extra_photos:
                fd, efile = tempfile.mkstemp(
                    prefix='claudio-img-', suffix='.jpg', dir=tmp_dir,
                )
                os.close(fd)
                os.chmod(efile, 0o600)
                tmp_files.append(efile)
                if client.download_image(fid, efile):
                    extra_image_files.append(efile)
                else:
                    log_error(_MODULE, "Failed to download extra photo from media group",
                              bot_id=bot_id)

            if extra_image_files:
                log(_MODULE,
                    f"Downloaded {1 + len(extra_image_files)} photos from media group",
                    bot_id=bot_id)

        doc_file = ''
        if msg.has_document:
            ext = safe_filename_ext(msg.doc_filename)
            fd, doc_file = tempfile.mkstemp(
                prefix='claudio-doc-', suffix=f'.{ext}', dir=tmp_dir,
            )
            os.close(fd)
            os.chmod(doc_file, 0o600)
            tmp_files.append(doc_file)

            ok = client.download_document(msg.doc_file_id, doc_file)
            if not ok:
                client.send_message(
                    msg.chat_id,
                    "Sorry, I couldn't download your file. Please try again.",
                    reply_to=msg.message_id,
                )
                return

        # -- Voice transcription --

        if msg.has_voice:
            if not config.elevenlabs_api_key:
                client.send_message(
                    msg.chat_id,
                    f"_{voice_label.capitalize()} messages require ELEVENLABS_API_KEY "
                    f"to be configured._",
                    reply_to=msg.message_id,
                )
                return

            voice_ext = 'oga' if platform == 'telegram' else 'ogg'
            fd, voice_file = tempfile.mkstemp(
                prefix='claudio-voice-', suffix=f'.{voice_ext}', dir=tmp_dir,
            )
            os.close(fd)
            os.chmod(voice_file, 0o600)
            tmp_files.append(voice_file)

            if platform == 'telegram':
                ok = client.download_voice(msg.voice_file_id, voice_file)
            else:
                ok = client.download_audio(msg.voice_file_id, voice_file)

            if not ok:
                client.send_message(
                    msg.chat_id,
                    f"Sorry, I couldn't download your {voice_label} message. "
                    f"Please try again.",
                    reply_to=msg.message_id,
                )
                return

            transcription = stt_transcribe(
                voice_file,
                config.elevenlabs_api_key,
                model=config.elevenlabs_stt_model,
            )
            if not transcription:
                client.send_message(
                    msg.chat_id,
                    f"Sorry, I couldn't transcribe your {voice_label} message. "
                    f"Please try again.",
                    reply_to=msg.message_id,
                )
                return

            has_voice = True

            # Voice file no longer needed after transcription
            try:
                os.unlink(voice_file)
                tmp_files.remove(voice_file)
            except (OSError, ValueError):
                pass

            # Prepend transcription to text
            if text:
                text = f"{transcription}\n\n{text}"
            else:
                text = transcription
            log(_MODULE, f"{voice_label.capitalize()} message transcribed: "
                f"{len(transcription)} chars", bot_id=bot_id)

        # -- Build prompt with media references --

        if image_file:
            image_count = 1 + len(extra_image_files)
            if image_count == 1:
                prefix = f"[The user sent an image at {image_file}]"
                text = f"{prefix}\n\n{text}" if text else \
                    f"{prefix}\n\nDescribe this image."
            else:
                all_images = [image_file] + extra_image_files
                refs = ', '.join(all_images)
                prefix = f"[The user sent {image_count} images at: {refs}]"
                text = f"{prefix}\n\n{text}" if text else \
                    f"{prefix}\n\nDescribe these images."

        if doc_file:
            doc_name = sanitize_doc_name(msg.doc_filename)
            prefix = f'[The user sent a file "{doc_name}" at {doc_file}]'
            text = f"{prefix}\n\n{text}" if text else \
                f"{prefix}\n\nRead this file and summarize its contents."

        # -- Build history text (descriptive, without temp file paths) --

        history_text = text
        if has_voice:
            history_text = f"[Sent a {voice_label} message: {transcription}]"
        elif image_file:
            user_caption = msg.caption or msg.text
            if extra_image_files:
                img_total = 1 + len(extra_image_files)
                if user_caption:
                    history_text = f"[Sent {img_total} images with caption: {user_caption}]"
                else:
                    history_text = f"[Sent {img_total} images]"
            elif user_caption:
                history_text = f"[Sent an image with caption: {user_caption}]"
            else:
                history_text = "[Sent an image]"
        elif doc_file:
            doc_name = sanitize_doc_name(msg.doc_filename)
            user_caption = msg.caption or msg.text
            if user_caption:
                history_text = f'[Sent a file "{doc_name}" with caption: {user_caption}]'
            else:
                history_text = f'[Sent a file "{doc_name}"]'

        # -- Typing indicator (Telegram only — WhatsApp removed) --

        if platform == 'telegram':
            typing_action = 'record_voice' if has_voice else 'typing'

            def _typing_loop():
                while not typing_stop.is_set():
                    client.send_typing(msg.chat_id, typing_action)
                    typing_stop.wait(4)

            typing_thread = threading.Thread(target=_typing_loop, daemon=True)
            typing_thread.start()

        # -- Initialize DB --

        if config.db_file:
            _db_init(config.db_file)

        # -- History retrieval --

        history_context = ''
        if config.db_file and config.max_history_lines > 0:
            try:
                history_context = _history_get_context(
                    config.db_file, config.max_history_lines,
                )
            except Exception as e:
                log_error(_MODULE, f"Failed to get history: {e}", bot_id=bot_id)

        # -- Memory retrieval --

        memories = ''
        if config.memory_enabled:
            try:
                memories = _memory_retrieve(text)
            except Exception as e:
                log_error(_MODULE, f"Failed to retrieve memories: {e}", bot_id=bot_id)

        # -- Claude invocation --

        result = run_claude(text, config, history_context=history_context, memories=memories)
        response = result.response

        # -- Enrich document history with summary from response --

        if response and not (msg.caption or msg.text) and doc_file:
            doc_name = sanitize_doc_name(msg.doc_filename)
            history_text = f'[Sent a file "{doc_name}": {summarize(response)}]'

        # -- Record history --

        if config.db_file:
            try:
                _history_add(config.db_file, 'user', history_text)

                if response:
                    history_response = response
                    if result.notifier_messages:
                        history_response = (
                            f"{result.notifier_messages}\n\n{history_response}"
                        )
                    if result.tool_summary:
                        history_response = (
                            f"{result.tool_summary}\n\n{history_response}"
                        )
                    history_response = sanitize_for_prompt(history_response)
                    _history_add(config.db_file, 'assistant', history_response)
            except Exception as e:
                log_error(_MODULE, f"Failed to record history: {e}", bot_id=bot_id)

        # -- Memory consolidation (background) --

        if config.memory_enabled and response:
            t = threading.Thread(target=_memory_consolidate, daemon=True)
            t.start()

        # -- Response delivery --

        if response:
            if has_voice and config.elevenlabs_api_key:
                _deliver_voice_response(
                    response, config, client, msg, platform,
                    tmp_dir, tmp_files, bot_id,
                )
            else:
                client.send_message(msg.chat_id, response, reply_to=msg.message_id)
        else:
            client.send_message(
                msg.chat_id,
                "Sorry, I couldn't get a response. Please try again.",
                reply_to=msg.message_id,
            )

    except Exception as e:
        log_error(_MODULE, f"Error processing message: {e}", bot_id=bot_id)
        try:
            client.send_message(
                msg.chat_id,
                "Sorry, an error occurred while processing your message. "
                "Please try again.",
                reply_to=msg.message_id,
            )
        except Exception:
            pass

    finally:
        # Stop typing indicator
        typing_stop.set()
        if typing_thread:
            typing_thread.join(timeout=5)

        # Clean up temp files
        for path in tmp_files:
            try:
                os.unlink(path)
            except OSError:
                pass


def _deliver_voice_response(response, config, client, msg, platform,
                            tmp_dir, tmp_files, bot_id):
    """Convert response to voice/audio and send, falling back to text."""
    fd, tts_file = tempfile.mkstemp(
        prefix='claudio-tts-', suffix='.mp3', dir=tmp_dir,
    )
    os.close(fd)
    os.chmod(tts_file, 0o600)
    tmp_files.append(tts_file)

    if tts_convert(response, tts_file, config.elevenlabs_api_key,
                   config.elevenlabs_voice_id, config.elevenlabs_model):
        if platform == 'telegram':
            ok = client.send_voice(msg.chat_id, tts_file, reply_to=msg.message_id)
        else:
            ok = client.send_audio(msg.chat_id, tts_file, reply_to=msg.message_id)

        if not ok:
            label = 'voice' if platform == 'telegram' else 'audio'
            log_error(_MODULE, f"Failed to send {label}, falling back to text",
                      bot_id=bot_id)
            client.send_message(msg.chat_id, response, reply_to=msg.message_id)
    else:
        log_error(_MODULE, "TTS conversion failed, sending text only", bot_id=bot_id)
        client.send_message(msg.chat_id, response, reply_to=msg.message_id)
