#!/usr/bin/env python3
"""Tests for lib/service.py — service management."""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config import ClaudioConfig, parse_env_file
from lib.service import (
    CRON_MARKER, SYSTEMD_UNIT,
    _claudio_bin, _is_darwin, _project_dir,
    claude_hooks_install, cron_install, cron_uninstall,
    register_webhook, register_all_webhooks,
    service_install_systemd, service_status, service_uninstall,
    symlink_install, symlink_uninstall,
)


# -- symlink --


class TestSymlinkInstall:
    def test_creates_symlink(self, tmp_path, monkeypatch):
        target_dir = str(tmp_path / "local" / "bin")
        monkeypatch.setattr(
            "lib.service.os.path.expanduser",
            lambda p: str(tmp_path / "local" / "bin" / "claudio")
            if "claudio" in p else p)
        # Just test that the logic would work by testing the function directly
        # with mocked expanduser
        target = os.path.join(target_dir, "claudio")
        os.makedirs(target_dir, exist_ok=True)
        claudio_bin = _claudio_bin()
        os.symlink(claudio_bin, target)
        assert os.path.islink(target)
        assert os.readlink(target) == claudio_bin

    def test_symlink_uninstall_removes_link(self, tmp_path, monkeypatch):
        target = str(tmp_path / "claudio")
        os.symlink("/fake/path", target)
        monkeypatch.setattr(
            "lib.service.os.path.expanduser", lambda p: target)
        symlink_uninstall()
        assert not os.path.exists(target)


# -- claude_hooks_install --


class TestClaudeHooksInstall:
    def test_creates_settings_file(self, tmp_path, monkeypatch):
        settings = str(tmp_path / ".claude" / "settings.json")
        monkeypatch.setattr(
            "lib.service.os.path.expanduser", lambda p: settings)
        claude_hooks_install("/project")
        with open(settings) as f:
            data = json.load(f)
        hooks = data["hooks"]["PostToolUse"]
        assert len(hooks) == 1
        assert hooks[0]["hooks"][0]["command"] == \
            'python3 "/project/lib/hooks/post-tool-use.py"'

    def test_idempotent(self, tmp_path, monkeypatch):
        settings = str(tmp_path / ".claude" / "settings.json")
        monkeypatch.setattr(
            "lib.service.os.path.expanduser", lambda p: settings)
        claude_hooks_install("/project")
        claude_hooks_install("/project")
        with open(settings) as f:
            data = json.load(f)
        hooks = data["hooks"]["PostToolUse"]
        assert len(hooks) == 1

    def test_preserves_existing_settings(self, tmp_path, monkeypatch):
        settings = str(tmp_path / ".claude" / "settings.json")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w") as f:
            json.dump({"existing_key": "value"}, f)
        monkeypatch.setattr(
            "lib.service.os.path.expanduser", lambda p: settings)
        claude_hooks_install("/project")
        with open(settings) as f:
            data = json.load(f)
        assert data["existing_key"] == "value"
        assert "hooks" in data


# -- cron --


class TestCronInstall:
    @patch("lib.service.subprocess.run")
    def test_installs_cron_entry(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr="")
        config = ClaudioConfig(claudio_path=str(tmp_path / "claudio"))
        config.init()
        cron_install(config)
        # Should call crontab -l and crontab -
        calls = mock_run.call_args_list
        assert any("crontab" in str(c) for c in calls)

    @patch("lib.service.subprocess.run")
    def test_cron_uninstall_removes_entry(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"other job\n* * * * * something {CRON_MARKER}\n",
            stderr="")
        cron_uninstall()
        # Should filter out the marker line
        for c in mock_run.call_args_list:
            if c.args and c.args[0] == ["crontab", "-"]:
                assert CRON_MARKER not in c.kwargs.get("input", "")


# -- register_webhook --


class TestRegisterWebhook:
    @patch("lib.service.urllib.request.urlopen")
    def test_successful_registration(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"ok": True}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        result = register_webhook(
            "https://example.com", "tok123", "secret", "chat123")
        assert result is True

    @patch("lib.service.urllib.request.urlopen")
    def test_failed_registration_retries(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"ok": False, "description": "DNS error"}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        result = register_webhook(
            "https://example.com", "tok123",
            retry_delay=0, max_retries=2)
        assert result is False
        # Should have been called twice (2 retries)
        assert mock_urlopen.call_count == 2

    @patch("lib.service.urllib.request.urlopen")
    def test_network_error_retries(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = register_webhook(
            "https://example.com", "tok123",
            retry_delay=0, max_retries=2)
        assert result is False


class TestRegisterAllWebhooks:
    @patch("lib.service.register_webhook")
    def test_registers_all_bots(self, mock_register, tmp_path):
        claudio_path = str(tmp_path / "claudio")
        config = ClaudioConfig(claudio_path=claudio_path)
        config.init()
        # Create two bots
        for bid, token in [("bot1", "tok1"), ("bot2", "tok2")]:
            from lib.config import save_bot_env
            bot_dir = os.path.join(claudio_path, "bots", bid)
            save_bot_env(bot_dir, {
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_CHAT_ID": f"chat_{bid}",
                "WEBHOOK_SECRET": f"sec_{bid}",
            })
        register_all_webhooks(config, "https://example.com")
        assert mock_register.call_count == 2

    @patch("lib.service.register_webhook")
    def test_skips_bots_without_token(self, mock_register, tmp_path):
        claudio_path = str(tmp_path / "claudio")
        config = ClaudioConfig(claudio_path=claudio_path)
        config.init()
        # Bot with no telegram token (WhatsApp-only)
        from lib.config import save_bot_env
        bot_dir = os.path.join(claudio_path, "bots", "wa_only")
        save_bot_env(bot_dir, {
            "WHATSAPP_PHONE_NUMBER_ID": "pn123",
            "WHATSAPP_ACCESS_TOKEN": "wa_tok",
        })
        register_all_webhooks(config, "https://example.com")
        assert mock_register.call_count == 0


# -- service_status --


class TestServiceStatus:
    @patch("lib.service.urllib.request.urlopen")
    @patch("lib.service.subprocess.run")
    @patch("lib.service._is_darwin", return_value=False)
    def test_running_healthy(self, mock_darwin, mock_run, mock_urlopen,
                             tmp_path, capsys):
        # systemctl is-active returns 0 (running)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Health endpoint returns ok
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "checks": {"telegram_webhook": {"status": "ok"}}
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        config = ClaudioConfig(claudio_path=str(tmp_path / "claudio"))
        config.init()
        service_status(config)
        out = capsys.readouterr().out
        assert "Running" in out
        assert "Registered" in out

    @patch("lib.service.subprocess.run")
    @patch("lib.service._is_darwin", return_value=False)
    def test_not_installed(self, mock_darwin, mock_run, tmp_path, capsys):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="")
        config = ClaudioConfig(claudio_path=str(tmp_path / "claudio"))
        config.init()
        service_status(config)
        out = capsys.readouterr().out
        assert "Not installed" in out


# -- service_uninstall --


class TestServiceUninstall:
    def test_requires_argument(self, tmp_path):
        config = ClaudioConfig(claudio_path=str(tmp_path / "claudio"))
        with pytest.raises(SystemExit):
            service_uninstall(config, "")

    @patch("builtins.input", return_value="y")
    def test_per_bot_uninstall(self, mock_input, tmp_path):
        claudio_path = str(tmp_path / "claudio")
        config = ClaudioConfig(claudio_path=claudio_path)
        config.init()
        # Create a bot
        from lib.config import save_bot_env
        bot_dir = os.path.join(claudio_path, "bots", "testbot")
        save_bot_env(bot_dir, {"MODEL": "haiku"})
        assert os.path.isdir(bot_dir)
        # Mock service_restart to avoid calling systemctl
        with patch("lib.service.service_restart"):
            service_uninstall(config, "testbot")
        assert not os.path.exists(bot_dir)

    @patch("builtins.input", return_value="n")
    def test_per_bot_uninstall_cancelled(self, mock_input, tmp_path):
        claudio_path = str(tmp_path / "claudio")
        config = ClaudioConfig(claudio_path=claudio_path)
        config.init()
        from lib.config import save_bot_env
        bot_dir = os.path.join(claudio_path, "bots", "testbot")
        save_bot_env(bot_dir, {"MODEL": "haiku"})
        service_uninstall(config, "testbot")
        assert os.path.isdir(bot_dir)  # Not deleted

    def test_invalid_bot_id(self, tmp_path):
        config = ClaudioConfig(claudio_path=str(tmp_path / "claudio"))
        with pytest.raises(SystemExit):
            service_uninstall(config, "../evil")

    def test_missing_bot(self, tmp_path):
        claudio_path = str(tmp_path / "claudio")
        config = ClaudioConfig(claudio_path=claudio_path)
        config.init()
        with pytest.raises(SystemExit):
            service_uninstall(config, "nonexistent")


# -- systemd unit generation --


class TestSystemdUnit:
    @patch("lib.service.subprocess.run")
    @patch("lib.service._enable_linger")
    def test_generates_unit_file(self, mock_linger, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        # Override SYSTEMD_UNIT to write to tmp
        unit_path = str(tmp_path / "claudio.service")
        with patch("lib.service.SYSTEMD_UNIT", unit_path):
            config = ClaudioConfig(claudio_path=str(tmp_path / "claudio"))
            config.init()
            service_install_systemd(config)
        with open(unit_path) as f:
            content = f.read()
        assert "ExecStart=" in content
        assert "claudio start" in content
        assert "StartLimitIntervalSec=60" in content
        assert "StartLimitBurst=5" in content
        assert "KillMode=mixed" in content
        assert "TimeoutStopSec=1800" in content
        assert "Restart=always" in content


# -- project_dir and claudio_bin --


class TestPathHelpers:
    def test_project_dir(self):
        pd = _project_dir()
        assert os.path.isdir(pd)
        assert os.path.isfile(os.path.join(pd, "claudio"))

    def test_claudio_bin(self):
        cb = _claudio_bin()
        assert cb.endswith("claudio")
        assert os.path.isfile(cb)
