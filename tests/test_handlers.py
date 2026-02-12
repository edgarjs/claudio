#!/usr/bin/env python3
"""Tests for lib/handlers.py — webhook orchestrator."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call

# Ensure project root is on sys.path so `lib.*` imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.handlers import (
    ParsedMessage,
    _db_init,
    _handle_command,
    _history_add,
    _history_get_context,
    _parse_telegram,
    _parse_whatsapp,
    _process_message,
    process_webhook,
)
from lib.claude_runner import ClaudeResult
from lib.config import BotConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tg_body(text="hello", chat_id=123, message_id=42, **extra_msg):
    """Build a minimal Telegram webhook body."""
    msg = {
        "chat": {"id": chat_id},
        "message_id": message_id,
        "text": text,
    }
    msg.update(extra_msg)
    return json.dumps({"message": msg})


def _wa_body(text="hello", from_number="5551234", msg_id="wamid.123",
             msg_type="text", **extra):
    """Build a minimal WhatsApp webhook body."""
    wa_msg = {
        "from": from_number,
        "id": msg_id,
        "type": msg_type,
    }
    if msg_type == "text":
        wa_msg["text"] = {"body": text}
    wa_msg.update(extra)
    return json.dumps({
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [wa_msg],
                }
            }]
        }]
    })


def _make_config(tmp_dir, **overrides):
    """Build a BotConfig with sensible test defaults."""
    bot_dir = os.path.join(tmp_dir, "bot")
    os.makedirs(bot_dir, exist_ok=True)
    defaults = dict(
        bot_id="test-bot",
        bot_dir=bot_dir,
        telegram_token="tok:123",
        telegram_chat_id="999",
        whatsapp_phone_number="5551234",
        whatsapp_phone_number_id="PH123",
        whatsapp_access_token="wa-token",
        model="sonnet",
        db_file=os.path.join(tmp_dir, "history.db"),
        elevenlabs_api_key="el-key",
        memory_enabled=False,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _make_bot_config_dict(tmp_dir, **overrides):
    """Build a server.py-style bot_config dict for process_webhook."""
    bot_dir = os.path.join(tmp_dir, "bot")
    os.makedirs(bot_dir, exist_ok=True)
    defaults = dict(
        bot_dir=bot_dir,
        token="tok:123",
        chat_id="999",
        secret="sec",
        phone_number_id="PH123",
        access_token="wa-token",
        app_secret="app-sec",
        verify_token="verify-tok",
        phone_number="5551234",
        model="sonnet",
        max_history_lines="100",
    )
    defaults.update(overrides)
    return defaults


def _mock_claude_result(response="Test response."):
    """Build a mock ClaudeResult."""
    return ClaudeResult(
        response=response,
        raw_json={"result": response},
        notifier_messages='',
        tool_summary='',
    )


# ---------------------------------------------------------------------------
# _parse_telegram
# ---------------------------------------------------------------------------

class TestParseTelegram(unittest.TestCase):
    """Tests for _parse_telegram()."""

    def test_text_message(self):
        msg = _parse_telegram(_tg_body(text="hello world"))
        self.assertIsNotNone(msg)
        self.assertEqual(msg.chat_id, "123")
        self.assertEqual(msg.message_id, "42")
        self.assertEqual(msg.text, "hello world")
        self.assertFalse(msg.has_image)
        self.assertFalse(msg.has_document)
        self.assertFalse(msg.has_voice)

    def test_image_with_caption(self):
        body = _tg_body(
            text="",
            caption="nice photo",
            photo=[
                {"file_id": "small_id", "width": 100},
                {"file_id": "large_id", "width": 800},
            ],
        )
        msg = _parse_telegram(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_image)
        self.assertEqual(msg.image_file_id, "large_id")
        self.assertEqual(msg.caption, "nice photo")
        self.assertEqual(msg.image_ext, "jpg")

    def test_voice_message(self):
        body = _tg_body(text="", voice={"file_id": "voice123", "duration": 5})
        msg = _parse_telegram(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_voice)
        self.assertEqual(msg.voice_file_id, "voice123")

    def test_document(self):
        body = _tg_body(
            text="",
            document={
                "file_id": "doc456",
                "mime_type": "application/pdf",
                "file_name": "report.pdf",
            },
        )
        msg = _parse_telegram(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_document)
        self.assertEqual(msg.doc_file_id, "doc456")
        self.assertEqual(msg.doc_mime, "application/pdf")
        self.assertEqual(msg.doc_filename, "report.pdf")
        self.assertFalse(msg.has_image)

    def test_reply_context(self):
        body = _tg_body(
            text="I agree!",
            reply_to_message={
                "text": "What do you think?",
                "from": {"first_name": "Alice"},
            },
        )
        msg = _parse_telegram(body)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.reply_to_text, "What do you think?")
        self.assertEqual(msg.reply_to_from, "Alice")

    def test_media_group_extra_photos(self):
        body = json.dumps({
            "message": {
                "chat": {"id": 123},
                "message_id": 42,
                "text": "",
                "caption": "group photos",
                "photo": [{"file_id": "main_id", "width": 800}],
                "_extra_photos": ["extra1", "extra2"],
            }
        })
        msg = _parse_telegram(body)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.image_file_id, "main_id")
        self.assertEqual(msg.extra_photos, ["extra1", "extra2"])

    def test_document_as_image(self):
        """A document with image/ mime type should be treated as an image."""
        body = _tg_body(
            text="",
            document={
                "file_id": "imgdoc789",
                "mime_type": "image/png",
                "file_name": "screenshot.png",
            },
        )
        msg = _parse_telegram(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_image)
        self.assertEqual(msg.image_file_id, "imgdoc789")
        self.assertEqual(msg.image_ext, "png")
        # Should NOT be treated as a document
        self.assertFalse(msg.has_document)
        self.assertEqual(msg.doc_file_id, '')

    def test_invalid_json(self):
        msg = _parse_telegram("not json at all")
        self.assertIsNone(msg)

    def test_missing_message_key(self):
        msg = _parse_telegram(json.dumps({"update_id": 1}))
        self.assertIsNone(msg)

    def test_missing_chat_id(self):
        body = json.dumps({"message": {"text": "hi"}})
        msg = _parse_telegram(body)
        self.assertIsNone(msg)


# ---------------------------------------------------------------------------
# _parse_whatsapp
# ---------------------------------------------------------------------------

class TestParseWhatsApp(unittest.TestCase):
    """Tests for _parse_whatsapp()."""

    def test_text_message(self):
        msg = _parse_whatsapp(_wa_body(text="hello"))
        self.assertIsNotNone(msg)
        self.assertEqual(msg.chat_id, "5551234")
        self.assertEqual(msg.message_id, "wamid.123")
        self.assertEqual(msg.text, "hello")
        self.assertEqual(msg.message_type, "text")

    def test_image_with_caption(self):
        body = _wa_body(
            msg_type="image",
            image={"id": "img999", "caption": "sunset"},
        )
        msg = _parse_whatsapp(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_image)
        self.assertEqual(msg.image_file_id, "img999")
        self.assertEqual(msg.caption, "sunset")
        self.assertEqual(msg.message_type, "image")

    def test_document(self):
        body = _wa_body(
            msg_type="document",
            document={
                "id": "doc111",
                "filename": "data.csv",
                "mime_type": "text/csv",
            },
        )
        msg = _parse_whatsapp(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_document)
        self.assertEqual(msg.doc_file_id, "doc111")
        self.assertEqual(msg.doc_filename, "data.csv")

    def test_audio(self):
        body = _wa_body(msg_type="audio", audio={"id": "aud222"})
        msg = _parse_whatsapp(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_voice)
        self.assertEqual(msg.voice_file_id, "aud222")

    def test_voice(self):
        body = _wa_body(msg_type="voice", voice={"id": "voi333"})
        msg = _parse_whatsapp(body)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.has_voice)
        self.assertEqual(msg.voice_file_id, "voi333")

    def test_reply_context(self):
        body = _wa_body(
            text="thanks",
            context={"id": "wamid.original"},
        )
        msg = _parse_whatsapp(body)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.context_id, "wamid.original")

    def test_invalid_json(self):
        msg = _parse_whatsapp("{{bad json")
        self.assertIsNone(msg)

    def test_empty_messages_array(self):
        body = json.dumps({
            "entry": [{"changes": [{"value": {"messages": []}}]}]
        })
        msg = _parse_whatsapp(body)
        self.assertIsNone(msg)

    def test_missing_entry(self):
        msg = _parse_whatsapp(json.dumps({}))
        self.assertIsNone(msg)

    def test_missing_from_number(self):
        body = json.dumps({
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{"id": "m1", "type": "text", "text": {"body": "hi"}}]
                    }
                }]
            }]
        })
        msg = _parse_whatsapp(body)
        self.assertIsNone(msg)


# ---------------------------------------------------------------------------
# ParsedMessage properties
# ---------------------------------------------------------------------------

class TestParsedMessageProperties(unittest.TestCase):
    """Tests for ParsedMessage.has_image, has_document, has_voice."""

    def test_has_image(self):
        msg = ParsedMessage(image_file_id="img1")
        self.assertTrue(msg.has_image)
        self.assertFalse(msg.has_document)
        self.assertFalse(msg.has_voice)

    def test_has_document(self):
        msg = ParsedMessage(doc_file_id="doc1")
        self.assertFalse(msg.has_image)
        self.assertTrue(msg.has_document)
        self.assertFalse(msg.has_voice)

    def test_has_voice(self):
        msg = ParsedMessage(voice_file_id="voi1")
        self.assertFalse(msg.has_image)
        self.assertFalse(msg.has_document)
        self.assertTrue(msg.has_voice)

    def test_none_of_the_above(self):
        msg = ParsedMessage(text="just text")
        self.assertFalse(msg.has_image)
        self.assertFalse(msg.has_document)
        self.assertFalse(msg.has_voice)


# ---------------------------------------------------------------------------
# _handle_command
# ---------------------------------------------------------------------------

class TestHandleCommand(unittest.TestCase):
    """Tests for _handle_command()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = _make_config(self.tmp)
        self.client = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_opus_command(self):
        result = _handle_command("/opus", self.config, self.client, "999", "42")
        self.assertTrue(result)
        self.assertEqual(self.config.model, "opus")
        self.client.send_message.assert_called_once()
        args = self.client.send_message.call_args
        self.assertIn("Opus", args[0][1])

    def test_sonnet_command(self):
        self.config.model = "haiku"
        result = _handle_command("/sonnet", self.config, self.client, "999", "42")
        self.assertTrue(result)
        self.assertEqual(self.config.model, "sonnet")

    def test_haiku_command(self):
        self.config.model = "opus"
        result = _handle_command("/haiku", self.config, self.client, "999", "42")
        self.assertTrue(result)
        self.assertEqual(self.config.model, "haiku")

    def test_start_command(self):
        result = _handle_command("/start", self.config, self.client, "999", "42")
        self.assertTrue(result)
        self.client.send_message.assert_called_once()
        args = self.client.send_message.call_args
        self.assertIn("Hola", args[0][1])

    def test_non_command_returns_false(self):
        result = _handle_command("just a message", self.config, self.client, "999", "42")
        self.assertFalse(result)
        self.client.send_message.assert_not_called()

    def test_empty_text_returns_false(self):
        result = _handle_command("", self.config, self.client, "999", "42")
        self.assertFalse(result)

    def test_none_text_returns_false(self):
        result = _handle_command(None, self.config, self.client, "999", "42")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _db_init, _history_add, _history_get_context
# ---------------------------------------------------------------------------

class TestDbHistory(unittest.TestCase):
    """Tests for _db_init, _history_add, _history_get_context."""

    def setUp(self):
        fd, self.db_file = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.db_file)
        except OSError:
            pass

    def test_db_init_creates_tables(self):
        _db_init(self.db_file)
        conn = sqlite3.connect(self.db_file)
        # Check messages table
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        # Check token_usage table
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        conn.close()

    def test_db_init_idempotent(self):
        """Calling _db_init twice should not error."""
        _db_init(self.db_file)
        _db_init(self.db_file)

    def test_history_add_and_retrieve(self):
        _db_init(self.db_file)
        _history_add(self.db_file, "user", "Hello!")
        _history_add(self.db_file, "assistant", "Hi there!")

        ctx = _history_get_context(self.db_file, 10)
        self.assertIn("H: Hello!", ctx)
        self.assertIn("A: Hi there!", ctx)

    def test_empty_db_returns_empty_string(self):
        _db_init(self.db_file)
        ctx = _history_get_context(self.db_file, 10)
        self.assertEqual(ctx, '')

    def test_history_limit(self):
        _db_init(self.db_file)
        for i in range(5):
            _history_add(self.db_file, "user", f"msg-{i}")

        ctx = _history_get_context(self.db_file, 2)
        # Should only have the last 2 messages
        self.assertNotIn("msg-0", ctx)
        self.assertNotIn("msg-1", ctx)
        self.assertNotIn("msg-2", ctx)
        self.assertIn("msg-3", ctx)
        self.assertIn("msg-4", ctx)

    def test_history_ordering(self):
        """Messages should appear in chronological order."""
        _db_init(self.db_file)
        _history_add(self.db_file, "user", "first")
        _history_add(self.db_file, "assistant", "second")
        _history_add(self.db_file, "user", "third")

        ctx = _history_get_context(self.db_file, 10)
        first_pos = ctx.index("first")
        second_pos = ctx.index("second")
        third_pos = ctx.index("third")
        self.assertLess(first_pos, second_pos)
        self.assertLess(second_pos, third_pos)


# ---------------------------------------------------------------------------
# process_webhook — integration-style tests (everything mocked)
# ---------------------------------------------------------------------------

_SERVICE_ENV = {
    "ELEVENLABS_API_KEY": "el-key",
    "MEMORY_ENABLED": "0",
}


class TestProcessWebhook(unittest.TestCase):
    """Tests for process_webhook() — full pipeline with mocked externals."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot_config_dict = _make_bot_config_dict(self.tmp)

        # Common patches
        self.patches = []
        self._add_patch("lib.handlers._load_service_env", return_value=_SERVICE_ENV)
        self._add_patch("lib.handlers.run_claude", return_value=_mock_claude_result())
        self._add_patch("lib.handlers._memory_retrieve", return_value='')
        self._add_patch("lib.handlers._memory_consolidate")
        self._add_patch("lib.handlers.stt_transcribe", return_value="transcribed text")
        self._add_patch("lib.handlers.tts_convert", return_value=True)

        # Start all patches
        for p in self.patches:
            p.start()

    def _add_patch(self, target, **kwargs):
        p = patch(target, **kwargs)
        self.patches.append(p)
        return p

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        # Reset cached service env between tests
        import lib.handlers
        lib.handlers._service_env = None

    @patch("lib.handlers.TelegramClient")
    def test_telegram_text_roundtrip(self, mock_tg_cls):
        """Full Telegram text message flow: parse, auth, invoke Claude, reply."""
        mock_client = MagicMock()
        mock_tg_cls.return_value = mock_client

        body = _tg_body(text="hello from test", chat_id=999)
        process_webhook(body, "test-bot", "telegram", self.bot_config_dict)

        # Client should have been created with the token
        mock_tg_cls.assert_called_once_with("tok:123", bot_id="test-bot")
        # Reaction should have been set (acknowledge receipt)
        mock_client.set_reaction.assert_called_once()
        # Claude should have been called
        from lib.handlers import run_claude
        run_claude.assert_called_once()
        # Response should have been sent
        mock_client.send_message.assert_called()
        sent_args = mock_client.send_message.call_args
        self.assertEqual(sent_args[0][0], "999")  # chat_id
        self.assertEqual(sent_args[0][1], "Test response.")

    @patch("lib.handlers.WhatsAppClient")
    def test_whatsapp_text_roundtrip(self, mock_wa_cls):
        """Full WhatsApp text message flow."""
        mock_client = MagicMock()
        mock_wa_cls.return_value = mock_client

        body = _wa_body(text="hello wa", from_number="5551234")
        process_webhook(body, "test-bot", "whatsapp", self.bot_config_dict)

        mock_wa_cls.assert_called_once_with("PH123", "wa-token", bot_id="test-bot")
        mock_client.mark_read.assert_called_once()
        mock_client.send_message.assert_called()

    @patch("lib.handlers.TelegramClient")
    def test_telegram_auth_rejection(self, mock_tg_cls):
        """Messages from wrong chat_id should be rejected silently."""
        mock_client = MagicMock()
        mock_tg_cls.return_value = mock_client

        body = _tg_body(text="intruder!", chat_id=666)
        process_webhook(body, "test-bot", "telegram", self.bot_config_dict)

        # Client should NOT have been instantiated (auth check happens first)
        # Actually auth check happens after parsing but before client use for messages
        from lib.handlers import run_claude
        run_claude.assert_not_called()

    @patch("lib.handlers.WhatsAppClient")
    def test_whatsapp_unsupported_type(self, mock_wa_cls):
        """Unsupported WhatsApp message types get a polite rejection."""
        mock_client = MagicMock()
        mock_wa_cls.return_value = mock_client

        body = _wa_body(msg_type="sticker", from_number="5551234")
        process_webhook(body, "test-bot", "whatsapp", self.bot_config_dict)

        mock_client.send_message.assert_called_once()
        args = mock_client.send_message.call_args
        self.assertIn("don't support", args[0][1])

    @patch("lib.handlers.TelegramClient")
    def test_empty_message_skipped(self, mock_tg_cls):
        """A message with no text, image, doc, or voice should be skipped."""
        mock_client = MagicMock()
        mock_tg_cls.return_value = mock_client

        body = _tg_body(text="", chat_id=999)
        process_webhook(body, "test-bot", "telegram", self.bot_config_dict)

        from lib.handlers import run_claude
        run_claude.assert_not_called()
        # send_message should not be called (silently skipped)
        mock_client.send_message.assert_not_called()

    @patch("lib.handlers.TelegramClient")
    def test_command_handling(self, mock_tg_cls):
        """Slash commands should be handled without invoking Claude."""
        mock_client = MagicMock()
        mock_tg_cls.return_value = mock_client

        body = _tg_body(text="/haiku", chat_id=999)
        process_webhook(body, "test-bot", "telegram", self.bot_config_dict)

        from lib.handlers import run_claude
        run_claude.assert_not_called()
        # Should send a confirmation message
        mock_client.send_message.assert_called_once()
        args = mock_client.send_message.call_args
        self.assertIn("Haiku", args[0][1])


# ---------------------------------------------------------------------------
# _process_message — media flow tests
# ---------------------------------------------------------------------------

class TestProcessMessage(unittest.TestCase):
    """Tests for _process_message() with various media types."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = _make_config(self.tmp)
        self.client = MagicMock()

        # Initialize the DB so history operations work
        _db_init(self.config.db_file)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        # Reset cached service env
        import lib.handlers
        lib.handlers._service_env = None

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.stt_transcribe", return_value="Hello from voice")
    @patch("lib.handlers.tts_convert", return_value=True)
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("Voice reply"))
    def test_voice_message_flow(self, mock_claude, mock_tts, mock_stt,
                                mock_mem_r, mock_mem_c):
        """Voice messages should be downloaded, transcribed, sent to Claude,
        and the response delivered as voice audio."""
        msg = ParsedMessage(
            chat_id="999",
            message_id="42",
            voice_file_id="voice_abc",
        )

        self.client.download_voice.return_value = True
        self.client.send_voice.return_value = True

        _process_message(msg, '', self.config, self.client, "telegram", "test-bot")

        self.client.download_voice.assert_called_once()
        mock_stt.assert_called_once()
        mock_claude.assert_called_once()
        # The prompt sent to Claude should contain the transcription
        prompt_arg = mock_claude.call_args[0][0]
        self.assertIn("Hello from voice", prompt_arg)
        # TTS should be called to generate voice response
        mock_tts.assert_called_once()
        # Voice should be sent
        self.client.send_voice.assert_called_once()

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("Here is the file summary."))
    def test_document_flow(self, mock_claude, mock_mem_r, mock_mem_c):
        """Document messages should be downloaded and referenced in the prompt."""
        msg = ParsedMessage(
            chat_id="999",
            message_id="42",
            doc_file_id="doc_xyz",
            doc_mime="application/pdf",
            doc_filename="report.pdf",
        )

        self.client.download_document.return_value = True

        _process_message(msg, '', self.config, self.client, "telegram", "test-bot")

        self.client.download_document.assert_called_once()
        mock_claude.assert_called_once()
        # The prompt should reference the document
        prompt_arg = mock_claude.call_args[0][0]
        self.assertIn("report.pdf", prompt_arg)
        # Response should be sent as text
        self.client.send_message.assert_called()

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("Nice photo!"))
    def test_image_flow(self, mock_claude, mock_mem_r, mock_mem_c):
        """Single image messages should be downloaded and referenced in the prompt."""
        msg = ParsedMessage(
            chat_id="999",
            message_id="42",
            text="Check this out",
            image_file_id="img_abc",
            image_ext="jpg",
        )

        self.client.download_image.return_value = True

        _process_message(msg, "Check this out", self.config, self.client,
                         "telegram", "test-bot")

        self.client.download_image.assert_called_once()
        mock_claude.assert_called_once()
        prompt_arg = mock_claude.call_args[0][0]
        self.assertIn("image", prompt_arg.lower())
        self.assertIn("Check this out", prompt_arg)

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("Group photos!"))
    def test_image_media_group(self, mock_claude, mock_mem_r, mock_mem_c):
        """Media group (multiple images) should download all photos."""
        msg = ParsedMessage(
            chat_id="999",
            message_id="42",
            text="album",
            image_file_id="img_main",
            image_ext="jpg",
            extra_photos=["img_extra1", "img_extra2"],
        )

        self.client.download_image.return_value = True

        _process_message(msg, "album", self.config, self.client,
                         "telegram", "test-bot")

        # download_image called for main + 2 extras
        self.assertEqual(self.client.download_image.call_count, 3)
        mock_claude.assert_called_once()
        prompt_arg = mock_claude.call_args[0][0]
        self.assertIn("3 images", prompt_arg)

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result(""))
    def test_empty_response_sends_error(self, mock_claude, mock_mem_r, mock_mem_c):
        """When Claude returns an empty response, an error message should be sent."""
        msg = ParsedMessage(chat_id="999", message_id="42", text="hello")

        _process_message(msg, "hello", self.config, self.client,
                         "telegram", "test-bot")

        self.client.send_message.assert_called()
        args = self.client.send_message.call_args
        self.assertIn("couldn't get a response", args[0][1])

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("OK"))
    def test_history_recorded(self, mock_claude, mock_mem_r, mock_mem_c):
        """Both user message and assistant response should be recorded in history."""
        msg = ParsedMessage(chat_id="999", message_id="42", text="hi there")

        _process_message(msg, "hi there", self.config, self.client,
                         "telegram", "test-bot")

        ctx = _history_get_context(self.config.db_file, 10)
        self.assertIn("H: hi there", ctx)
        self.assertIn("A: OK", ctx)

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("reply"))
    def test_image_download_failure(self, mock_claude, mock_mem_r, mock_mem_c):
        """When image download fails, an error message should be sent."""
        msg = ParsedMessage(
            chat_id="999",
            message_id="42",
            text="look",
            image_file_id="img_bad",
            image_ext="jpg",
        )

        self.client.download_image.return_value = False

        _process_message(msg, "look", self.config, self.client,
                         "telegram", "test-bot")

        mock_claude.assert_not_called()
        self.client.send_message.assert_called_once()
        args = self.client.send_message.call_args
        self.assertIn("couldn't download", args[0][1].lower())

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.stt_transcribe", return_value='')
    @patch("lib.handlers.run_claude")
    def test_voice_transcription_failure(self, mock_claude, mock_stt,
                                         mock_mem_r, mock_mem_c):
        """When STT returns empty, an error message should be sent."""
        msg = ParsedMessage(
            chat_id="999",
            message_id="42",
            voice_file_id="voice_bad",
        )
        self.client.download_voice.return_value = True

        _process_message(msg, '', self.config, self.client,
                         "telegram", "test-bot")

        mock_claude.assert_not_called()
        self.client.send_message.assert_called_once()
        args = self.client.send_message.call_args
        self.assertIn("couldn't transcribe", args[0][1].lower())

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("wa reply"))
    def test_whatsapp_audio_flow(self, mock_claude, mock_mem_r, mock_mem_c):
        """WhatsApp audio uses download_audio instead of download_voice."""
        msg = ParsedMessage(
            chat_id="5551234",
            message_id="wamid.1",
            voice_file_id="aud_123",
        )
        self.client.download_audio.return_value = True

        with patch("lib.handlers.stt_transcribe", return_value="wa transcription"), \
             patch("lib.handlers.tts_convert", return_value=True):
            _process_message(msg, '', self.config, self.client,
                             "whatsapp", "test-bot")

        self.client.download_audio.assert_called_once()
        # WhatsApp sends audio, not voice
        self.client.send_audio.assert_called_once()

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("Got it"))
    def test_no_voice_key_sends_text(self, mock_claude, mock_mem_r, mock_mem_c):
        """Non-voice messages should be replied with text, not audio."""
        msg = ParsedMessage(chat_id="999", message_id="42", text="plain text")

        _process_message(msg, "plain text", self.config, self.client,
                         "telegram", "test-bot")

        self.client.send_message.assert_called()
        self.client.send_voice.assert_not_called()

    @patch("lib.handlers._memory_consolidate")
    @patch("lib.handlers._memory_retrieve", return_value='')
    @patch("lib.handlers.run_claude", return_value=_mock_claude_result("reply"))
    def test_voice_no_elevenlabs_key(self, mock_claude, mock_mem_r, mock_mem_c):
        """Voice message without ElevenLabs key should return an error."""
        self.config.elevenlabs_api_key = ''
        msg = ParsedMessage(
            chat_id="999",
            message_id="42",
            voice_file_id="voice_abc",
        )

        _process_message(msg, '', self.config, self.client,
                         "telegram", "test-bot")

        mock_claude.assert_not_called()
        self.client.send_message.assert_called_once()
        args = self.client.send_message.call_args
        self.assertIn("ELEVENLABS_API_KEY", args[0][1])


if __name__ == "__main__":
    unittest.main()
