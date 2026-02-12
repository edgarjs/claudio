#!/usr/bin/env python3
"""Tests for lib/cli.py — CLI entry point."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.cli import _version, _parse_retention_args, main


class TestVersion:
    def test_reads_version_file(self):
        version = _version()
        assert version  # Non-empty
        # Should match semver-ish format
        assert "." in version

    def test_version_command(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio", "version"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "claudio v" in out

    def test_version_flag(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


class TestUsage:
    def test_no_args_shows_usage(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Usage:" in out

    def test_unknown_command_shows_usage(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio", "bogus"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


class TestParseRetentionArgs:
    def test_defaults(self):
        hours, days = _parse_retention_args([])
        assert hours == 24
        assert days == 7

    def test_custom_hours(self):
        hours, days = _parse_retention_args(["--hours", "12"])
        assert hours == 12
        assert days == 7

    def test_custom_days(self):
        hours, days = _parse_retention_args(["--days", "14"])
        assert hours == 24
        assert days == 14

    def test_both(self):
        hours, days = _parse_retention_args(
            ["--hours", "6", "--days", "30"])
        assert hours == 6
        assert days == 30

    def test_invalid_hours(self):
        with pytest.raises(SystemExit):
            _parse_retention_args(["--hours", "abc"])

    def test_missing_hours_value(self):
        with pytest.raises(SystemExit):
            _parse_retention_args(["--hours"])

    def test_unknown_arg(self):
        with pytest.raises(SystemExit):
            _parse_retention_args(["--unknown"])


class TestDispatch:
    @patch("lib.service.service_status")
    def test_status_command(self, mock_status, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio", "status"])
        main()
        assert mock_status.called

    @patch("lib.service.service_restart")
    def test_restart_command(self, mock_restart, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio", "restart"])
        main()
        assert mock_restart.called

    def test_telegram_setup_invalid(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio", "telegram", "bogus"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_whatsapp_setup_invalid(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["claudio", "whatsapp", "bogus"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_log_no_file(self, monkeypatch, tmp_path):
        # Point config to a path with no log file
        monkeypatch.setattr("sys.argv", ["claudio", "log"])
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(SystemExit) as exc_info:
            from lib.config import ClaudioConfig
            config = ClaudioConfig(claudio_path=str(tmp_path / "claudio"))
            config.init()
            from lib.cli import _handle_log
            _handle_log(config, [])
        assert exc_info.value.code == 1

    def test_backup_help(self, capsys, monkeypatch):
        from lib.cli import _handle_backup
        with pytest.raises(SystemExit) as exc_info:
            _handle_backup(["--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Usage:" in out
