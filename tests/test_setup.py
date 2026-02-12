"""Tests for lib/setup.py -- interactive setup wizards."""

import io
import json
import os
import sys
import urllib.error
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config import ClaudioConfig, parse_env_file, save_bot_env
from lib.setup import (
    SetupError,
    _build_bot_env_fields,
    _poll_for_start,
    _save_telegram_config,
    _save_whatsapp_config,
    _telegram_api_call,
    _validate_bot_id,
    _whatsapp_api_call,
    bot_setup,
    telegram_setup,
    whatsapp_setup,
)


# -- Helpers --


def _make_config(tmp_path, webhook_url="https://test.example.com"):
    """Create a ClaudioConfig with a populated service.env."""
    claudio_path = str(tmp_path / "claudio")
    os.makedirs(claudio_path, exist_ok=True)
    env_file = os.path.join(claudio_path, "service.env")
    with open(env_file, "w") as f:
        f.write(f'WEBHOOK_URL="{webhook_url}"\n')
        f.write('PORT="8421"\n')
    cfg = ClaudioConfig(claudio_path=claudio_path)
    cfg.init()
    return cfg


class FakeResponse:
    """Fake urllib response that acts as a context manager."""

    def __init__(self, data):
        self._data = json.dumps(data).encode("utf-8") if isinstance(data, dict) else data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _make_urlopen_responses(responses):
    """Build a side_effect function for urllib.request.urlopen.

    Args:
        responses: List of dicts (returned as JSON) or callables.
                   A single dict is reused for all calls.
    """
    if isinstance(responses, dict):
        responses = [responses]
    call_idx = [0]

    def side_effect(req, timeout=None):
        idx = call_idx[0]
        call_idx[0] += 1
        entry = responses[min(idx, len(responses) - 1)]
        if callable(entry) and not isinstance(entry, dict):
            return entry(req, timeout)
        return FakeResponse(entry)

    return side_effect


# -- _validate_bot_id --


class TestValidateBotId:
    def test_valid_ids(self):
        for bid in ("claudio", "bot-1", "my_bot", "Bot123"):
            _validate_bot_id(bid)  # Should not raise

    def test_invalid_ids(self):
        for bid in ("../evil", "", "-start", " spaces", "a/b"):
            with pytest.raises(SetupError):
                _validate_bot_id(bid)


# -- _build_bot_env_fields --


class TestBuildBotEnvFields:
    def test_telegram_only(self):
        fields = _build_bot_env_fields(
            {},
            telegram={"token": "tok", "chat_id": "123", "webhook_secret": "sec"},
        )
        assert fields["TELEGRAM_BOT_TOKEN"] == "tok"
        assert fields["TELEGRAM_CHAT_ID"] == "123"
        assert fields["WEBHOOK_SECRET"] == "sec"
        assert "WHATSAPP_PHONE_NUMBER_ID" not in fields
        assert fields["MODEL"] == "haiku"
        assert fields["MAX_HISTORY_LINES"] == "100"

    def test_whatsapp_only(self):
        fields = _build_bot_env_fields(
            {},
            whatsapp={
                "phone_number_id": "pn1",
                "access_token": "at",
                "app_secret": "as",
                "verify_token": "vt",
                "phone_number": "555",
            },
        )
        assert fields["WHATSAPP_PHONE_NUMBER_ID"] == "pn1"
        assert "TELEGRAM_BOT_TOKEN" not in fields

    def test_preserves_existing_whatsapp_when_setting_telegram(self):
        existing = {
            "WHATSAPP_PHONE_NUMBER_ID": "pn_old",
            "WHATSAPP_ACCESS_TOKEN": "at_old",
            "WHATSAPP_APP_SECRET": "as_old",
            "WHATSAPP_VERIFY_TOKEN": "vt_old",
            "WHATSAPP_PHONE_NUMBER": "555_old",
            "MODEL": "sonnet",
            "MAX_HISTORY_LINES": "50",
        }
        fields = _build_bot_env_fields(
            existing,
            telegram={"token": "new_tok", "chat_id": "999", "webhook_secret": "new_sec"},
        )
        assert fields["TELEGRAM_BOT_TOKEN"] == "new_tok"
        assert fields["WHATSAPP_PHONE_NUMBER_ID"] == "pn_old"
        assert fields["MODEL"] == "sonnet"

    def test_preserves_existing_telegram_when_setting_whatsapp(self):
        existing = {
            "TELEGRAM_BOT_TOKEN": "tok_old",
            "TELEGRAM_CHAT_ID": "123_old",
            "WEBHOOK_SECRET": "sec_old",
            "MODEL": "opus",
        }
        fields = _build_bot_env_fields(
            existing,
            whatsapp={
                "phone_number_id": "pn_new",
                "access_token": "at_new",
                "app_secret": "as_new",
                "verify_token": "vt_new",
                "phone_number": "777",
            },
        )
        assert fields["TELEGRAM_BOT_TOKEN"] == "tok_old"
        assert fields["WHATSAPP_PHONE_NUMBER_ID"] == "pn_new"
        assert fields["MODEL"] == "opus"

    def test_empty_existing_no_platforms(self):
        fields = _build_bot_env_fields({})
        assert "TELEGRAM_BOT_TOKEN" not in fields
        assert "WHATSAPP_PHONE_NUMBER_ID" not in fields
        assert fields["MODEL"] == "haiku"


# -- _telegram_api_call --


class TestTelegramApiCall:
    @patch("lib.setup.urllib.request.urlopen")
    def test_successful_call(self, mock_urlopen):
        expected = {"ok": True, "result": {"username": "testbot"}}
        mock_urlopen.return_value = FakeResponse(expected)
        result = _telegram_api_call("fake_token", "getMe")
        assert result == expected

    @patch("lib.setup.urllib.request.urlopen")
    def test_http_error_raises_setup_error(self, mock_urlopen):
        body = json.dumps({"description": "Unauthorized"}).encode()
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.telegram.org/bot/getMe", 401, "Unauthorized",
            {}, io.BytesIO(body),
        )
        with pytest.raises(SetupError, match="Unauthorized"):
            _telegram_api_call("bad_token", "getMe")

    @patch("lib.setup.urllib.request.urlopen")
    def test_network_error_raises_setup_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with pytest.raises(SetupError, match="Network error"):
            _telegram_api_call("tok", "getMe")


# -- _whatsapp_api_call --


class TestWhatsAppApiCall:
    @patch("lib.setup.urllib.request.urlopen")
    def test_successful_call(self, mock_urlopen):
        expected = {"verified_name": "Test Business", "id": "123"}
        mock_urlopen.return_value = FakeResponse(expected)
        result = _whatsapp_api_call("123", "fake_token")
        assert result == expected

    @patch("lib.setup.urllib.request.urlopen")
    def test_sets_authorization_header(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse({"verified_name": "Biz"})
        _whatsapp_api_call("123", "my_bearer_token")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer my_bearer_token"

    @patch("lib.setup.urllib.request.urlopen")
    def test_http_error_raises_setup_error(self, mock_urlopen):
        body = json.dumps({"error": {"message": "Invalid token"}}).encode()
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://graph.facebook.com/v21.0/123", 400, "Bad Request",
            {}, io.BytesIO(body),
        )
        with pytest.raises(SetupError, match="Invalid token"):
            _whatsapp_api_call("123", "bad")


# -- telegram_setup --


class TestTelegramSetup:
    def _api_responses_telegram(self, chat_id=12345):
        """Return the standard sequence of Telegram API responses for setup."""
        return [
            {"ok": True, "result": {"username": "testbot"}},     # getMe
            {"ok": True, "result": True},                        # deleteWebhook
            {"ok": True, "result": [                              # getUpdates with /start
                {"update_id": 1, "message": {
                    "text": "/start",
                    "chat": {"id": chat_id},
                }}
            ]},
            {"ok": True, "result": []},                          # getUpdates (clear)
            {"ok": True, "result": {"message_id": 1}},           # sendMessage
        ]

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", return_value="123:ABC-token")
    @patch("lib.setup.urllib.request.urlopen")
    def test_successful_setup(self, mock_urlopen, mock_input, mock_browser, tmp_path):
        config = _make_config(tmp_path)
        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram(chat_id=99887)
        )

        telegram_setup(config, bot_id="mybot")

        bot_env = parse_env_file(
            os.path.join(config.claudio_path, "bots", "mybot", "bot.env")
        )
        assert bot_env["TELEGRAM_BOT_TOKEN"] == "123:ABC-token"
        assert bot_env["TELEGRAM_CHAT_ID"] == "99887"
        assert len(bot_env["WEBHOOK_SECRET"]) == 64  # 32 bytes hex

    @patch("builtins.input", return_value="")
    def test_empty_token_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            telegram_setup(config, bot_id="mybot")

    @patch("builtins.input", return_value="bad-token")
    @patch("lib.setup.urllib.request.urlopen")
    def test_invalid_token_exits(self, mock_urlopen, mock_input, tmp_path):
        config = _make_config(tmp_path)
        body = json.dumps({"ok": False, "description": "Unauthorized"}).encode()
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 401, "Unauthorized", {}, io.BytesIO(body),
        )

        with pytest.raises(SystemExit):
            telegram_setup(config, bot_id="mybot")

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", return_value="tok:new")
    @patch("lib.setup.urllib.request.urlopen")
    def test_preserves_existing_whatsapp_config(self, mock_urlopen, mock_input,
                                                mock_browser, tmp_path):
        config = _make_config(tmp_path)
        bot_dir = os.path.join(config.claudio_path, "bots", "mybot")
        save_bot_env(bot_dir, {
            "WHATSAPP_PHONE_NUMBER_ID": "pn_existing",
            "WHATSAPP_ACCESS_TOKEN": "at_existing",
            "WHATSAPP_APP_SECRET": "as_existing",
            "WHATSAPP_VERIFY_TOKEN": "vt_existing",
            "WHATSAPP_PHONE_NUMBER": "555_existing",
            "MODEL": "opus",
            "MAX_HISTORY_LINES": "75",
        })

        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram(chat_id=42)
        )

        telegram_setup(config, bot_id="mybot")

        bot_env = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert bot_env["TELEGRAM_BOT_TOKEN"] == "tok:new"
        assert bot_env["TELEGRAM_CHAT_ID"] == "42"
        assert bot_env["WHATSAPP_PHONE_NUMBER_ID"] == "pn_existing"
        assert bot_env["WHATSAPP_ACCESS_TOKEN"] == "at_existing"
        assert bot_env["MODEL"] == "opus"
        assert bot_env["MAX_HISTORY_LINES"] == "75"

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", return_value="new_tok")
    @patch("lib.setup.urllib.request.urlopen")
    def test_preserves_existing_webhook_secret(self, mock_urlopen, mock_input,
                                               mock_browser, tmp_path):
        config = _make_config(tmp_path)
        bot_dir = os.path.join(config.claudio_path, "bots", "mybot")
        save_bot_env(bot_dir, {
            "TELEGRAM_BOT_TOKEN": "old_tok",
            "TELEGRAM_CHAT_ID": "old_chat",
            "WEBHOOK_SECRET": "existing_secret_keep_me",
            "MODEL": "haiku",
            "MAX_HISTORY_LINES": "100",
        })

        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram(chat_id=999)
        )

        telegram_setup(config, bot_id="mybot")

        bot_env = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert bot_env["WEBHOOK_SECRET"] == "existing_secret_keep_me"

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", return_value="tok:fresh")
    @patch("lib.setup.urllib.request.urlopen")
    def test_generates_new_webhook_secret_if_missing(self, mock_urlopen, mock_input,
                                                     mock_browser, tmp_path):
        config = _make_config(tmp_path)
        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram(chat_id=1)
        )

        telegram_setup(config, bot_id="freshbot")

        bot_env = parse_env_file(
            os.path.join(config.claudio_path, "bots", "freshbot", "bot.env")
        )
        secret = bot_env.get("WEBHOOK_SECRET", "")
        assert len(secret) == 64
        # Verify it's valid hex
        int(secret, 16)

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", return_value="tok:val")
    @patch("lib.setup.urllib.request.urlopen")
    def test_no_webhook_url_exits(self, mock_urlopen, mock_input, mock_browser, tmp_path):
        config = _make_config(tmp_path, webhook_url="")
        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram(chat_id=1)
        )

        with pytest.raises(SystemExit):
            telegram_setup(config, bot_id="mybot")

    @patch("builtins.input", return_value="tok:maybe")
    @patch("lib.setup.urllib.request.urlopen")
    def test_getme_returns_not_ok(self, mock_urlopen, mock_input, tmp_path):
        config = _make_config(tmp_path)
        mock_urlopen.return_value = FakeResponse({"ok": False})

        with pytest.raises(SystemExit):
            telegram_setup(config, bot_id="mybot")


# -- _poll_for_start --


class TestPollForStart:
    @patch("lib.setup.time.sleep")
    @patch("lib.setup.urllib.request.urlopen")
    def test_returns_chat_id_on_start(self, mock_urlopen, mock_sleep):
        responses = [
            {"ok": True, "result": []},  # First poll: no /start
            {"ok": True, "result": [     # Second poll: got /start
                {"update_id": 5, "message": {
                    "text": "/start",
                    "chat": {"id": 42},
                }}
            ]},
            {"ok": True, "result": []},  # Clear updates
        ]
        mock_urlopen.side_effect = _make_urlopen_responses(responses)
        result = _poll_for_start("fake_token")
        assert result == "42"

    @patch("lib.setup.time.monotonic", side_effect=[0.0, 200.0])
    def test_timeout_exits(self, mock_monotonic):
        with pytest.raises(SystemExit):
            _poll_for_start("fake_token")

    @patch("lib.setup.time.sleep")
    @patch("lib.setup.time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    @patch("lib.setup.urllib.request.urlopen")
    def test_ignores_non_start_messages(self, mock_urlopen, mock_monotonic, mock_sleep):
        responses = [
            {"ok": True, "result": [
                {"update_id": 1, "message": {"text": "hello", "chat": {"id": 10}}}
            ]},
            {"ok": True, "result": [
                {"update_id": 2, "message": {"text": "/start", "chat": {"id": 10}}}
            ]},
            {"ok": True, "result": []},  # Clear
        ]
        mock_urlopen.side_effect = _make_urlopen_responses(responses)
        result = _poll_for_start("tok")
        assert result == "10"

    @patch("lib.setup.time.sleep")
    @patch("lib.setup.time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0])
    @patch("lib.setup.urllib.request.urlopen")
    def test_recovers_from_api_errors(self, mock_urlopen, mock_monotonic, mock_sleep):
        call_count = [0]

        def side_effect(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.URLError("Connection refused")
            return FakeResponse({"ok": True, "result": [
                {"update_id": 3, "message": {"text": "/start", "chat": {"id": 77}}}
            ]})

        mock_urlopen.side_effect = side_effect
        result = _poll_for_start("tok")
        assert result == "77"


# -- whatsapp_setup --


class TestWhatsAppSetup:
    @patch("lib.setup.secrets.token_hex", return_value="a" * 64)
    @patch("builtins.input", side_effect=["pn123", "access_tok", "app_sec", "5551234"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_successful_setup(self, mock_urlopen, mock_input, mock_hex,
                              tmp_path, capsys):
        config = _make_config(tmp_path)
        mock_urlopen.return_value = FakeResponse(
            {"verified_name": "My Business", "id": "pn1"}
        )

        whatsapp_setup(config, bot_id="wabot")

        bot_env = parse_env_file(
            os.path.join(config.claudio_path, "bots", "wabot", "bot.env")
        )
        assert bot_env["WHATSAPP_PHONE_NUMBER_ID"] == "pn123"
        assert bot_env["WHATSAPP_ACCESS_TOKEN"] == "access_tok"
        assert bot_env["WHATSAPP_APP_SECRET"] == "app_sec"
        assert bot_env["WHATSAPP_PHONE_NUMBER"] == "5551234"
        assert bot_env["WHATSAPP_VERIFY_TOKEN"] == "a" * 64

        # Verify webhook instructions are printed
        captured = capsys.readouterr()
        assert "/whatsapp/webhook" in captured.out
        assert "a" * 64 in captured.out

    @patch("builtins.input", side_effect=[""])
    def test_empty_phone_id_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            whatsapp_setup(config, bot_id="wabot")

    @patch("builtins.input", side_effect=["pn1", ""])
    def test_empty_access_token_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            whatsapp_setup(config, bot_id="wabot")

    @patch("builtins.input", side_effect=["pn1", "tok", ""])
    def test_empty_app_secret_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            whatsapp_setup(config, bot_id="wabot")

    @patch("builtins.input", side_effect=["pn1", "tok", "sec", ""])
    def test_empty_phone_number_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            whatsapp_setup(config, bot_id="wabot")

    @patch("builtins.input", side_effect=["pn1", "bad_tok", "sec", "555"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_api_validation_failure_exits(self, mock_urlopen, mock_input, tmp_path):
        config = _make_config(tmp_path)
        body = json.dumps({"error": {"message": "Bad request"}}).encode()
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 400, "Bad Request", {}, io.BytesIO(body),
        )
        with pytest.raises(SystemExit):
            whatsapp_setup(config, bot_id="wabot")

    @patch("builtins.input", side_effect=["pn1", "tok", "sec", "555"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_missing_verified_name_exits(self, mock_urlopen, mock_input, tmp_path):
        config = _make_config(tmp_path)
        mock_urlopen.return_value = FakeResponse({"id": "123"})  # No verified_name
        with pytest.raises(SystemExit):
            whatsapp_setup(config, bot_id="wabot")

    @patch("lib.setup.secrets.token_hex", return_value="b" * 64)
    @patch("builtins.input", side_effect=["new_pn", "new_at", "new_as", "new_phone"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_preserves_existing_telegram_config(self, mock_urlopen, mock_input,
                                                mock_hex, tmp_path):
        config = _make_config(tmp_path)
        bot_dir = os.path.join(config.claudio_path, "bots", "dualbot")
        save_bot_env(bot_dir, {
            "TELEGRAM_BOT_TOKEN": "tg_tok_keep",
            "TELEGRAM_CHAT_ID": "tg_chat_keep",
            "WEBHOOK_SECRET": "tg_sec_keep",
            "MODEL": "sonnet",
            "MAX_HISTORY_LINES": "200",
        })

        mock_urlopen.return_value = FakeResponse({"verified_name": "Biz", "id": "pn1"})

        whatsapp_setup(config, bot_id="dualbot")

        bot_env = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert bot_env["TELEGRAM_BOT_TOKEN"] == "tg_tok_keep"
        assert bot_env["TELEGRAM_CHAT_ID"] == "tg_chat_keep"
        assert bot_env["WEBHOOK_SECRET"] == "tg_sec_keep"
        assert bot_env["WHATSAPP_PHONE_NUMBER_ID"] == "new_pn"
        assert bot_env["MODEL"] == "sonnet"
        assert bot_env["MAX_HISTORY_LINES"] == "200"

    @patch("builtins.input", side_effect=["pn1", "tok", "sec", "555"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_no_webhook_url_exits(self, mock_urlopen, mock_input, tmp_path):
        config = _make_config(tmp_path, webhook_url="")
        mock_urlopen.return_value = FakeResponse({"verified_name": "Biz", "id": "pn1"})
        with pytest.raises(SystemExit):
            whatsapp_setup(config, bot_id="wabot")


# -- bot_setup --


class TestBotSetup:
    def _api_responses_telegram(self, chat_id=1):
        """Standard Telegram API responses for bot_setup tests."""
        return [
            {"ok": True, "result": {"username": "testbot"}},
            {"ok": True, "result": True},
            {"ok": True, "result": [
                {"update_id": 1, "message": {"text": "/start", "chat": {"id": chat_id}}}
            ]},
            {"ok": True, "result": []},
            {"ok": True, "result": {"message_id": 1}},
        ]

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", side_effect=["1", "tok:abc", "N"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_choice_1_telegram_only(self, mock_urlopen, mock_input,
                                    mock_browser, tmp_path):
        config = _make_config(tmp_path)
        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram()
        )

        bot_setup(config, "testbot")

        bot_env = parse_env_file(
            os.path.join(config.claudio_path, "bots", "testbot", "bot.env")
        )
        assert bot_env["TELEGRAM_BOT_TOKEN"] == "tok:abc"
        assert "WHATSAPP_PHONE_NUMBER_ID" not in bot_env

    @patch("lib.setup.secrets.token_hex", return_value="c" * 64)
    @patch("builtins.input", side_effect=["2", "pn1", "tok", "sec", "555", "N"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_choice_2_whatsapp_only(self, mock_urlopen, mock_input,
                                    mock_hex, tmp_path):
        config = _make_config(tmp_path)
        mock_urlopen.return_value = FakeResponse({"verified_name": "Biz"})

        bot_setup(config, "testbot")

        bot_env = parse_env_file(
            os.path.join(config.claudio_path, "bots", "testbot", "bot.env")
        )
        assert bot_env["WHATSAPP_PHONE_NUMBER_ID"] == "pn1"
        assert "TELEGRAM_BOT_TOKEN" not in bot_env

    @patch("lib.setup.secrets.token_hex", return_value="d" * 64)
    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", side_effect=["3", "tg_tok", "pn1", "wa_tok", "wa_sec", "555"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_choice_3_both(self, mock_urlopen, mock_input, mock_browser,
                           mock_hex, tmp_path):
        config = _make_config(tmp_path)
        # Telegram flow responses, then WhatsApp validation
        responses = self._api_responses_telegram(chat_id=42) + [
            {"verified_name": "TestBiz"},
        ]
        mock_urlopen.side_effect = _make_urlopen_responses(responses)

        bot_setup(config, "testbot")

        bot_env = parse_env_file(
            os.path.join(config.claudio_path, "bots", "testbot", "bot.env")
        )
        assert bot_env["TELEGRAM_BOT_TOKEN"] == "tg_tok"
        assert bot_env["WHATSAPP_PHONE_NUMBER_ID"] == "pn1"

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", side_effect=["4", "new_tok"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_choice_4_reconfigure_telegram(self, mock_urlopen, mock_input,
                                           mock_browser, tmp_path):
        config = _make_config(tmp_path)
        bot_dir = os.path.join(config.claudio_path, "bots", "mybot")
        save_bot_env(bot_dir, {
            "TELEGRAM_BOT_TOKEN": "old_tok",
            "TELEGRAM_CHAT_ID": "old_chat",
            "WEBHOOK_SECRET": "old_sec",
            "MODEL": "haiku",
            "MAX_HISTORY_LINES": "100",
        })

        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram()
        )

        bot_setup(config, "mybot")

        bot_env = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert bot_env["TELEGRAM_BOT_TOKEN"] == "new_tok"

    @patch("lib.setup.secrets.token_hex", return_value="e" * 64)
    @patch("builtins.input", side_effect=["5", "new_pn", "new_at", "new_as", "new_phone"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_choice_5_reconfigure_whatsapp(self, mock_urlopen, mock_input,
                                           mock_hex, tmp_path):
        config = _make_config(tmp_path)
        bot_dir = os.path.join(config.claudio_path, "bots", "mybot")
        save_bot_env(bot_dir, {
            "WHATSAPP_PHONE_NUMBER_ID": "old_pn",
            "WHATSAPP_ACCESS_TOKEN": "old_at",
            "WHATSAPP_APP_SECRET": "old_as",
            "WHATSAPP_VERIFY_TOKEN": "old_vt",
            "WHATSAPP_PHONE_NUMBER": "old_phone",
            "MODEL": "haiku",
            "MAX_HISTORY_LINES": "100",
        })

        mock_urlopen.return_value = FakeResponse({"verified_name": "NewBiz"})

        bot_setup(config, "mybot")

        bot_env = parse_env_file(os.path.join(bot_dir, "bot.env"))
        assert bot_env["WHATSAPP_PHONE_NUMBER_ID"] == "new_pn"

    @patch("builtins.input", side_effect=["4"])
    def test_choice_4_without_telegram_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            bot_setup(config, "newbot")

    @patch("builtins.input", side_effect=["5"])
    def test_choice_5_without_whatsapp_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            bot_setup(config, "newbot")

    @patch("builtins.input", side_effect=["9"])
    def test_invalid_choice_exits(self, mock_input, tmp_path):
        config = _make_config(tmp_path)
        with pytest.raises(SystemExit):
            bot_setup(config, "newbot")

    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", side_effect=["4", "new_tok"])
    @patch("lib.setup.urllib.request.urlopen")
    def test_shows_existing_config(self, mock_urlopen, mock_input,
                                   mock_browser, tmp_path, capsys):
        config = _make_config(tmp_path)
        bot_dir = os.path.join(config.claudio_path, "bots", "mybot")
        save_bot_env(bot_dir, {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "123",
            "WEBHOOK_SECRET": "sec",
            "WHATSAPP_PHONE_NUMBER_ID": "pn1",
            "WHATSAPP_ACCESS_TOKEN": "at",
            "WHATSAPP_APP_SECRET": "as",
            "WHATSAPP_VERIFY_TOKEN": "vt",
            "WHATSAPP_PHONE_NUMBER": "555",
            "MODEL": "haiku",
            "MAX_HISTORY_LINES": "100",
        })

        mock_urlopen.side_effect = _make_urlopen_responses(
            self._api_responses_telegram()
        )

        bot_setup(config, "mybot")

        captured = capsys.readouterr()
        assert "Telegram configured" in captured.out
        assert "WhatsApp configured" in captured.out

    @patch("lib.setup.secrets.token_hex", return_value="f" * 64)
    @patch("lib.setup.webbrowser.open")
    @patch("builtins.input", side_effect=[
        "1", "tg_tok", "y", "pn1", "wa_tok", "wa_sec", "555",
    ])
    @patch("lib.setup.urllib.request.urlopen")
    def test_offer_whatsapp_after_telegram_yes(self, mock_urlopen, mock_input,
                                               mock_browser, mock_hex, tmp_path):
        """Choosing Telegram (1) then 'y' for WhatsApp sets up both."""
        config = _make_config(tmp_path)
        responses = self._api_responses_telegram(chat_id=42) + [
            {"verified_name": "Biz"},
        ]
        mock_urlopen.side_effect = _make_urlopen_responses(responses)

        bot_setup(config, "testbot")

        bot_env = parse_env_file(
            os.path.join(config.claudio_path, "bots", "testbot", "bot.env")
        )
        assert bot_env["TELEGRAM_BOT_TOKEN"] == "tg_tok"
        assert bot_env["WHATSAPP_PHONE_NUMBER_ID"] == "pn1"
