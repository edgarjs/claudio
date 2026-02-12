#!/usr/bin/env python3
"""Tests for lib/config.py — bot configuration management."""

import os
import sys

import pytest

# Add parent dir to path so we can import lib/config.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config import BotConfig, _env_quote, parse_env_file


# -- parse_env_file --


class TestParseEnvFile:
    def test_basic_key_value(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("KEY=value\n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "value"}

    def test_quoted_value(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text('KEY="quoted value"\n')
        result = parse_env_file(str(f))
        assert result == {"KEY": "quoted value"}

    def test_multiple_keys(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("A=1\nB=2\nC=3\n")
        result = parse_env_file(str(f))
        assert result == {"A": "1", "B": "2", "C": "3"}

    def test_skips_comments(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("# This is a comment\nKEY=value\n# Another comment\n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "value"}

    def test_skips_empty_lines(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("\nKEY=value\n\n\n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "value"}

    def test_missing_file(self):
        result = parse_env_file("/nonexistent/path.env")
        assert result == {}

    def test_escaped_newline_in_quotes(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text('MSG="line1\\nline2"\n')
        result = parse_env_file(str(f))
        assert result["MSG"] == "line1\nline2"

    def test_escaped_backtick_in_quotes(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text('CMD="run \\`cmd\\`"\n')
        result = parse_env_file(str(f))
        assert result["CMD"] == "run `cmd`"

    def test_escaped_dollar_in_quotes(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text('PRICE="\\$100"\n')
        result = parse_env_file(str(f))
        assert result["PRICE"] == "$100"

    def test_escaped_double_quote_in_quotes(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text('TEXT="say \\"hello\\""\n')
        result = parse_env_file(str(f))
        assert result["TEXT"] == 'say "hello"'

    def test_escaped_backslash_in_quotes(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text('PATH="C:\\\\Users\\\\me"\n')
        result = parse_env_file(str(f))
        assert result["PATH"] == "C:\\Users\\me"

    def test_unquoted_value_with_equals(self, tmp_path):
        # Value containing = should still work (only first = splits)
        f = tmp_path / "test.env"
        f.write_text("FORMULA=a=b\n")
        result = parse_env_file(str(f))
        assert result == {"FORMULA": "a=b"}

    def test_skips_lines_without_equals(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("NOVALUE\nKEY=value\n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "value"}

    def test_skips_lines_starting_with_equals(self, tmp_path):
        # eq < 1 means eq == 0 (starts with =) is also skipped
        f = tmp_path / "test.env"
        f.write_text("=bad\nKEY=good\n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "good"}

    def test_empty_quoted_value(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text('EMPTY=""\n')
        result = parse_env_file(str(f))
        assert result == {"EMPTY": ""}

    def test_single_char_quoted_value_not_stripped(self, tmp_path):
        # A single " is not len >= 2 with matching quotes
        f = tmp_path / "test.env"
        f.write_text('SINGLE="\n')
        result = parse_env_file(str(f))
        assert result == {"SINGLE": '"'}

    def test_unquoted_no_escape_processing(self, tmp_path):
        # Escape sequences should only be processed inside quotes
        f = tmp_path / "test.env"
        f.write_text("RAW=hello\\nworld\n")
        result = parse_env_file(str(f))
        assert result["RAW"] == "hello\\nworld"

    def test_escape_order_backslash_last(self, tmp_path):
        """Backslash unescape runs last, so \\n in the file becomes backslash + newline.

        File bytes: VAL="\\n"
        After strip quotes val = \\n (backslash, backslash, n)
        Processing order: replace('\\n', '\\n') matches at pos 1 -> \\ + newline
        Then replace('\\\\', '\\') -> nothing left (the \\\\ was split by \\n match).
        Result: backslash followed by newline.
        """
        f = tmp_path / "test.env"
        # Python string '\\\\n' writes the file bytes: \\n (backslash backslash n)
        f.write_text('VAL="\\\\n"\n')
        result = parse_env_file(str(f))
        assert result["VAL"] == "\\\n"


# -- _env_quote --


class TestEnvQuote:
    def test_escapes_backslash(self):
        assert _env_quote("a\\b") == "a\\\\b"

    def test_escapes_double_quote(self):
        assert _env_quote('say "hi"') == 'say \\"hi\\"'

    def test_escapes_dollar(self):
        assert _env_quote("$HOME") == "\\$HOME"

    def test_escapes_backtick(self):
        assert _env_quote("`cmd`") == "\\`cmd\\`"

    def test_escapes_newline(self):
        assert _env_quote("line1\nline2") == "line1\\nline2"

    def test_no_escaping_needed(self):
        assert _env_quote("simple") == "simple"

    def test_empty_string(self):
        assert _env_quote("") == ""

    def test_all_special_chars(self):
        result = _env_quote('\\"$`\n')
        assert result == '\\\\\\"\\$\\`\\n'

    def test_roundtrip_with_parse(self, tmp_path):
        """Values escaped by _env_quote should parse back to the original."""
        original = 'complex\\value"with$special`chars\nnewline'
        f = tmp_path / "test.env"
        f.write_text(f'KEY="{_env_quote(original)}"\n')
        result = parse_env_file(str(f))
        assert result["KEY"] == original


# -- BotConfig.__init__ --


class TestBotConfigInit:
    def test_defaults(self):
        cfg = BotConfig(bot_id="test")
        assert cfg.bot_id == "test"
        assert cfg.bot_dir == ""
        assert cfg.telegram_token == ""
        assert cfg.model == "haiku"
        assert cfg.max_history_lines == 100
        assert cfg.elevenlabs_voice_id == "iP95p4xoKVk53GoZ742B"
        assert cfg.elevenlabs_model == "eleven_multilingual_v2"
        assert cfg.elevenlabs_stt_model == "scribe_v1"
        assert cfg.memory_enabled is True
        assert cfg.db_file == ""

    def test_custom_values(self):
        cfg = BotConfig(
            bot_id="mybot",
            bot_dir="/tmp/mybot",
            telegram_token="tok123",
            telegram_chat_id="456",
            webhook_secret="sec",
            model="opus",
            max_history_lines=50,
        )
        assert cfg.bot_id == "mybot"
        assert cfg.bot_dir == "/tmp/mybot"
        assert cfg.telegram_token == "tok123"
        assert cfg.telegram_chat_id == "456"
        assert cfg.webhook_secret == "sec"
        assert cfg.model == "opus"
        assert cfg.max_history_lines == 50

    def test_max_history_lines_int_conversion(self):
        cfg = BotConfig(bot_id="test", max_history_lines="200")
        assert cfg.max_history_lines == 200
        assert isinstance(cfg.max_history_lines, int)

    def test_db_file_defaults_from_bot_dir(self):
        cfg = BotConfig(bot_id="test", bot_dir="/data/bots/test")
        assert cfg.db_file == "/data/bots/test/history.db"

    def test_db_file_explicit_overrides_bot_dir(self):
        cfg = BotConfig(bot_id="test", bot_dir="/data/bots/test", db_file="/custom/path.db")
        assert cfg.db_file == "/custom/path.db"

    def test_db_file_empty_when_no_bot_dir(self):
        cfg = BotConfig(bot_id="test")
        assert cfg.db_file == ""

    def test_whatsapp_fields(self):
        cfg = BotConfig(
            bot_id="wa",
            whatsapp_phone_number_id="123",
            whatsapp_access_token="token",
            whatsapp_app_secret="secret",
            whatsapp_verify_token="verify",
            whatsapp_phone_number="+15551234",
        )
        assert cfg.whatsapp_phone_number_id == "123"
        assert cfg.whatsapp_access_token == "token"
        assert cfg.whatsapp_app_secret == "secret"
        assert cfg.whatsapp_verify_token == "verify"
        assert cfg.whatsapp_phone_number == "+15551234"


# -- BotConfig.from_bot_config --


class TestBotConfigFromBotConfig:
    def test_telegram_bot(self):
        bot_config = {
            "token": "tg_token_123",
            "chat_id": "999",
            "secret": "webhook_sec",
            "bot_dir": "/bots/mybot",
            "model": "sonnet",
            "max_history_lines": "75",
        }
        cfg = BotConfig.from_bot_config("mybot", bot_config)
        assert cfg.bot_id == "mybot"
        assert cfg.telegram_token == "tg_token_123"
        assert cfg.telegram_chat_id == "999"
        assert cfg.webhook_secret == "webhook_sec"
        assert cfg.bot_dir == "/bots/mybot"
        assert cfg.model == "sonnet"
        assert cfg.max_history_lines == 75
        assert cfg.db_file == "/bots/mybot/history.db"

    def test_whatsapp_bot(self):
        bot_config = {
            "phone_number_id": "pn123",
            "access_token": "wa_token",
            "app_secret": "wa_secret",
            "verify_token": "wa_verify",
            "phone_number": "+1234567890",
            "bot_dir": "/bots/wabot",
        }
        cfg = BotConfig.from_bot_config("wabot", bot_config)
        assert cfg.whatsapp_phone_number_id == "pn123"
        assert cfg.whatsapp_access_token == "wa_token"
        assert cfg.whatsapp_app_secret == "wa_secret"
        assert cfg.whatsapp_verify_token == "wa_verify"
        assert cfg.whatsapp_phone_number == "+1234567890"

    def test_service_env_elevenlabs(self):
        bot_config = {"bot_dir": "/bots/test"}
        service_env = {
            "ELEVENLABS_API_KEY": "el_key",
            "ELEVENLABS_VOICE_ID": "custom_voice",
            "ELEVENLABS_MODEL": "custom_model",
            "ELEVENLABS_STT_MODEL": "custom_stt",
        }
        cfg = BotConfig.from_bot_config("test", bot_config, service_env=service_env)
        assert cfg.elevenlabs_api_key == "el_key"
        assert cfg.elevenlabs_voice_id == "custom_voice"
        assert cfg.elevenlabs_model == "custom_model"
        assert cfg.elevenlabs_stt_model == "custom_stt"

    def test_service_env_memory_enabled(self):
        bot_config = {"bot_dir": "/bots/test"}
        cfg = BotConfig.from_bot_config("test", bot_config, service_env={"MEMORY_ENABLED": "1"})
        assert cfg.memory_enabled is True

    def test_service_env_memory_disabled(self):
        bot_config = {"bot_dir": "/bots/test"}
        cfg = BotConfig.from_bot_config("test", bot_config, service_env={"MEMORY_ENABLED": "0"})
        assert cfg.memory_enabled is False

    def test_missing_service_env_defaults(self):
        bot_config = {"bot_dir": "/bots/test"}
        cfg = BotConfig.from_bot_config("test", bot_config, service_env=None)
        assert cfg.elevenlabs_api_key == ""
        assert cfg.elevenlabs_voice_id == "iP95p4xoKVk53GoZ742B"
        assert cfg.memory_enabled is True  # default '1' == '1'

    def test_missing_fields_default(self):
        cfg = BotConfig.from_bot_config("test", {})
        assert cfg.telegram_token == ""
        assert cfg.model == "haiku"
        assert cfg.max_history_lines == 100
        assert cfg.bot_dir == ""
        assert cfg.db_file == ""


# -- BotConfig.from_env_files --


class TestBotConfigFromEnvFiles:
    def _setup_env_files(self, tmp_path, bot_id="testbot", bot_env_lines=None, service_env_lines=None):
        """Helper to create a claudio_path with service.env and bot.env."""
        claudio_path = tmp_path / "claudio"
        bot_dir = claudio_path / "bots" / bot_id
        bot_dir.mkdir(parents=True)

        if service_env_lines:
            (claudio_path / "service.env").write_text("\n".join(service_env_lines) + "\n")
        if bot_env_lines:
            (bot_dir / "bot.env").write_text("\n".join(bot_env_lines) + "\n")

        return str(claudio_path)

    def test_reads_telegram_config(self, tmp_path):
        claudio_path = self._setup_env_files(
            tmp_path,
            bot_env_lines=[
                'TELEGRAM_BOT_TOKEN="token123"',
                'TELEGRAM_CHAT_ID="chat456"',
                'WEBHOOK_SECRET="sec789"',
                'MODEL="sonnet"',
                'MAX_HISTORY_LINES="50"',
            ],
        )
        cfg = BotConfig.from_env_files("testbot", claudio_path=claudio_path)
        assert cfg.bot_id == "testbot"
        assert cfg.telegram_token == "token123"
        assert cfg.telegram_chat_id == "chat456"
        assert cfg.webhook_secret == "sec789"
        assert cfg.model == "sonnet"
        assert cfg.max_history_lines == 50

    def test_reads_whatsapp_config(self, tmp_path):
        claudio_path = self._setup_env_files(
            tmp_path,
            bot_env_lines=[
                'WHATSAPP_PHONE_NUMBER_ID="pn123"',
                'WHATSAPP_ACCESS_TOKEN="wa_tok"',
                'WHATSAPP_APP_SECRET="wa_sec"',
                'WHATSAPP_VERIFY_TOKEN="wa_ver"',
                'WHATSAPP_PHONE_NUMBER="+15551234"',
            ],
        )
        cfg = BotConfig.from_env_files("testbot", claudio_path=claudio_path)
        assert cfg.whatsapp_phone_number_id == "pn123"
        assert cfg.whatsapp_access_token == "wa_tok"
        assert cfg.whatsapp_app_secret == "wa_sec"
        assert cfg.whatsapp_verify_token == "wa_ver"
        assert cfg.whatsapp_phone_number == "+15551234"

    def test_reads_service_env(self, tmp_path):
        claudio_path = self._setup_env_files(
            tmp_path,
            service_env_lines=[
                'ELEVENLABS_API_KEY="el_key_abc"',
                'ELEVENLABS_VOICE_ID="voice_xyz"',
                'MEMORY_ENABLED="0"',
            ],
        )
        cfg = BotConfig.from_env_files("testbot", claudio_path=claudio_path)
        assert cfg.elevenlabs_api_key == "el_key_abc"
        assert cfg.elevenlabs_voice_id == "voice_xyz"
        assert cfg.memory_enabled is False

    def test_missing_env_files_defaults(self, tmp_path):
        claudio_path = str(tmp_path / "empty_claudio")
        os.makedirs(claudio_path, exist_ok=True)
        cfg = BotConfig.from_env_files("ghost", claudio_path=claudio_path)
        assert cfg.bot_id == "ghost"
        assert cfg.telegram_token == ""
        assert cfg.model == "haiku"
        assert cfg.max_history_lines == 100
        assert cfg.memory_enabled is True

    def test_bot_dir_is_set(self, tmp_path):
        claudio_path = self._setup_env_files(tmp_path)
        cfg = BotConfig.from_env_files("testbot", claudio_path=claudio_path)
        expected_dir = os.path.join(claudio_path, "bots", "testbot")
        assert cfg.bot_dir == expected_dir

    def test_db_file_not_set_by_from_env_files(self, tmp_path):
        # from_env_files does not pass db_file, so __init__ derives it from bot_dir
        claudio_path = self._setup_env_files(tmp_path)
        cfg = BotConfig.from_env_files("testbot", claudio_path=claudio_path)
        expected = os.path.join(claudio_path, "bots", "testbot", "history.db")
        assert cfg.db_file == expected


# -- BotConfig.save_model --


class TestBotConfigSaveModel:
    def test_saves_valid_model(self, tmp_path):
        bot_dir = str(tmp_path / "bot")
        os.makedirs(bot_dir)
        cfg = BotConfig(bot_id="test", bot_dir=bot_dir, model="haiku")
        cfg.save_model("opus")
        assert cfg.model == "opus"

        # Verify the file was written
        bot_env_path = os.path.join(bot_dir, "bot.env")
        assert os.path.exists(bot_env_path)
        content = parse_env_file(bot_env_path)
        assert content["MODEL"] == "opus"

    def test_rejects_invalid_model(self):
        cfg = BotConfig(bot_id="test", bot_dir="/tmp/test")
        with pytest.raises(ValueError, match="Invalid model"):
            cfg.save_model("gpt-4")

    def test_accepts_all_valid_models(self, tmp_path):
        for model in ("opus", "sonnet", "haiku"):
            bot_dir = str(tmp_path / f"bot_{model}")
            os.makedirs(bot_dir)
            cfg = BotConfig(bot_id="test", bot_dir=bot_dir)
            cfg.save_model(model)
            assert cfg.model == model

    def test_noop_without_bot_dir(self):
        cfg = BotConfig(bot_id="test", bot_dir="")
        cfg.save_model("sonnet")
        assert cfg.model == "sonnet"
        # No file written, no error

    def test_creates_bot_dir_if_missing(self, tmp_path):
        bot_dir = str(tmp_path / "new" / "bot" / "dir")
        cfg = BotConfig(bot_id="test", bot_dir=bot_dir)
        cfg.save_model("haiku")
        assert os.path.isdir(bot_dir)
        assert os.path.exists(os.path.join(bot_dir, "bot.env"))

    def test_preserves_telegram_fields(self, tmp_path):
        bot_dir = str(tmp_path / "bot")
        os.makedirs(bot_dir)
        cfg = BotConfig(
            bot_id="test",
            bot_dir=bot_dir,
            telegram_token="tok",
            telegram_chat_id="chat",
            webhook_secret="sec",
            model="haiku",
            max_history_lines=75,
        )
        cfg.save_model("sonnet")
        content = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert content["TELEGRAM_BOT_TOKEN"] == "tok"
        assert content["TELEGRAM_CHAT_ID"] == "chat"
        assert content["WEBHOOK_SECRET"] == "sec"
        assert content["MODEL"] == "sonnet"
        assert content["MAX_HISTORY_LINES"] == "75"

    def test_preserves_whatsapp_fields(self, tmp_path):
        bot_dir = str(tmp_path / "bot")
        os.makedirs(bot_dir)
        cfg = BotConfig(
            bot_id="test",
            bot_dir=bot_dir,
            whatsapp_phone_number_id="pn",
            whatsapp_access_token="at",
            whatsapp_app_secret="as",
            whatsapp_verify_token="vt",
            whatsapp_phone_number="+1",
        )
        cfg.save_model("opus")
        content = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert content["WHATSAPP_PHONE_NUMBER_ID"] == "pn"
        assert content["WHATSAPP_ACCESS_TOKEN"] == "at"
        assert content["WHATSAPP_APP_SECRET"] == "as"
        assert content["WHATSAPP_VERIFY_TOKEN"] == "vt"
        assert content["WHATSAPP_PHONE_NUMBER"] == "+1"

    def test_omits_telegram_when_no_token(self, tmp_path):
        bot_dir = str(tmp_path / "bot")
        os.makedirs(bot_dir)
        cfg = BotConfig(bot_id="test", bot_dir=bot_dir)
        cfg.save_model("haiku")
        content = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert "TELEGRAM_BOT_TOKEN" not in content

    def test_omits_whatsapp_when_no_phone_number_id(self, tmp_path):
        bot_dir = str(tmp_path / "bot")
        os.makedirs(bot_dir)
        cfg = BotConfig(bot_id="test", bot_dir=bot_dir)
        cfg.save_model("haiku")
        content = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert "WHATSAPP_PHONE_NUMBER_ID" not in content

    def test_escapes_special_chars_in_values(self, tmp_path):
        bot_dir = str(tmp_path / "bot")
        os.makedirs(bot_dir)
        cfg = BotConfig(
            bot_id="test",
            bot_dir=bot_dir,
            telegram_token='tok$with"special\\chars',
            telegram_chat_id="chat",
            webhook_secret="sec",
        )
        cfg.save_model("haiku")
        # Re-read and verify roundtrip
        content = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert content["TELEGRAM_BOT_TOKEN"] == 'tok$with"special\\chars'

    def test_file_permissions_restrictive(self, tmp_path):
        bot_dir = str(tmp_path / "bot")
        os.makedirs(bot_dir)
        cfg = BotConfig(bot_id="test", bot_dir=bot_dir)
        cfg.save_model("haiku")
        bot_env_path = os.path.join(bot_dir, "bot.env")
        mode = os.stat(bot_env_path).st_mode & 0o777
        # umask 0o077 means file should be 0o600 (rw-------)
        assert mode == 0o600
