#!/usr/bin/env python3
"""Tests for lib/claude_runner.py — Claude CLI runner."""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from collections import namedtuple
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path so `lib.*` imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.claude_runner import (
    ClaudeResult,
    WEBHOOK_TIMEOUT,
    _build_full_prompt,
    _load_system_prompt,
    _persist_usage,
    _read_notifier_log,
    _read_tool_log,
    build_mcp_config,
    find_claude_cmd,
    run_claude,
)
from lib.config import BotConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path, **overrides):
    """Build a BotConfig with sensible test defaults, using tmp_path for bot_dir."""
    bot_dir = str(tmp_path / "bot")
    os.makedirs(bot_dir, exist_ok=True)
    defaults = dict(
        bot_id="test-bot",
        bot_dir=bot_dir,
        telegram_token="tok:123",
        telegram_chat_id="999",
        model="sonnet",
        db_file=str(tmp_path / "history.db"),
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


# ---------------------------------------------------------------------------
# find_claude_cmd
# ---------------------------------------------------------------------------

class TestFindClaudeCmd(unittest.TestCase):
    """Tests for find_claude_cmd()."""

    @patch("lib.claude_runner.shutil.which", return_value="/usr/local/bin/claude")
    def test_found_via_which(self, mock_which):
        result = find_claude_cmd()
        self.assertEqual(result, "/usr/local/bin/claude")
        mock_which.assert_called_once_with("claude")

    @patch("lib.claude_runner.shutil.which", return_value=None)
    @patch("lib.claude_runner.os.path.isfile", return_value=True)
    @patch("lib.claude_runner.os.access", return_value=True)
    def test_found_via_fallback_path(self, mock_access, mock_isfile, mock_which):
        result = find_claude_cmd()
        self.assertIsNotNone(result)
        # The first fallback candidate that matches should be returned
        mock_isfile.assert_called()
        mock_access.assert_called()

    @patch("lib.claude_runner.shutil.which", return_value=None)
    @patch("lib.claude_runner.os.path.isfile", return_value=False)
    def test_not_found_returns_none(self, mock_isfile, mock_which):
        result = find_claude_cmd()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# build_mcp_config
# ---------------------------------------------------------------------------

class TestBuildMcpConfig(unittest.TestCase):
    """Tests for build_mcp_config()."""

    def test_correct_structure(self):
        cfg = build_mcp_config(
            lib_dir="/opt/claudio/lib",
            telegram_token="tok:abc",
            chat_id="12345",
            notifier_log="/tmp/notifier.log",
        )

        self.assertIn("mcpServers", cfg)
        server = cfg["mcpServers"]["claudio-tools"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["/opt/claudio/lib/mcp_tools.py"])
        self.assertEqual(server["env"]["TELEGRAM_BOT_TOKEN"], "tok:abc")
        self.assertEqual(server["env"]["TELEGRAM_CHAT_ID"], "12345")
        self.assertEqual(server["env"]["NOTIFIER_LOG_FILE"], "/tmp/notifier.log")


# ---------------------------------------------------------------------------
# _load_system_prompt
# ---------------------------------------------------------------------------

class TestLoadSystemPrompt(unittest.TestCase):
    """Tests for _load_system_prompt()."""

    def test_loads_system_prompt(self, ):
        """Should load SYSTEM_PROMPT.md from the repo root."""
        # We know the repo root relative to claude_runner.py
        lib_dir = os.path.dirname(os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "lib", "claude_runner.py")
        ))
        repo_root = os.path.dirname(lib_dir)
        prompt_path = os.path.join(repo_root, "SYSTEM_PROMPT.md")

        # Only run the assertion if the file exists (it should in this repo)
        if os.path.isfile(prompt_path):
            result = _load_system_prompt('')
            self.assertIn("Claudio", result)  # SYSTEM_PROMPT.md mentions Claudio
        else:
            # If SYSTEM_PROMPT.md is missing, should return ''
            result = _load_system_prompt('')
            self.assertEqual(result, '')

    def test_appends_per_bot_claude_md(self, tmp_path=None):
        """When bot_dir has a CLAUDE.md, it should be appended."""
        import tempfile
        bot_dir = tempfile.mkdtemp()
        try:
            claude_md = os.path.join(bot_dir, "CLAUDE.md")
            with open(claude_md, 'w') as f:
                f.write("Custom bot instructions here.")

            result = _load_system_prompt(bot_dir)
            # The result should include the bot CLAUDE.md content
            self.assertIn("Custom bot instructions here.", result)
        finally:
            os.unlink(claude_md)
            os.rmdir(bot_dir)

    def test_missing_bot_claude_md(self):
        """Missing per-bot CLAUDE.md should not cause errors."""
        import tempfile
        bot_dir = tempfile.mkdtemp()
        try:
            result = _load_system_prompt(bot_dir)
            # Should succeed without the per-bot file; just the global prompt
            self.assertIsInstance(result, str)
        finally:
            os.rmdir(bot_dir)

    def test_missing_system_prompt_returns_empty(self):
        """If SYSTEM_PROMPT.md does not exist, return empty string."""
        with patch("builtins.open", side_effect=OSError("not found")):
            result = _load_system_prompt('')
            self.assertEqual(result, '')


# ---------------------------------------------------------------------------
# _build_full_prompt
# ---------------------------------------------------------------------------

class TestBuildFullPrompt(unittest.TestCase):
    """Tests for _build_full_prompt()."""

    def test_prompt_only(self):
        result = _build_full_prompt("hello", '', '')
        self.assertEqual(result, "hello")

    def test_with_memories_only(self):
        result = _build_full_prompt("hello", '', 'memory1')
        self.assertIn("<recalled-memories>", result)
        self.assertIn("memory1", result)
        self.assertIn("hello", result)
        self.assertNotIn("<conversation-history>", result)

    def test_with_history_only(self):
        result = _build_full_prompt("hello", 'H: hi\nA: hey', '')
        self.assertIn("<conversation-history>", result)
        self.assertIn("H: hi\nA: hey", result)
        self.assertIn("Now respond to this new message:", result)
        self.assertIn("hello", result)
        self.assertNotIn("<recalled-memories>", result)

    def test_with_both_memories_and_history(self):
        result = _build_full_prompt("hello", 'H: hi\nA: hey', 'memory1')
        self.assertIn("<recalled-memories>", result)
        self.assertIn("memory1", result)
        self.assertIn("<conversation-history>", result)
        self.assertIn("H: hi\nA: hey", result)
        self.assertIn("Now respond to this new message:", result)
        self.assertIn("hello", result)
        # memories should come before history
        mem_pos = result.index("<recalled-memories>")
        hist_pos = result.index("<conversation-history>")
        self.assertLess(mem_pos, hist_pos)


# ---------------------------------------------------------------------------
# _read_notifier_log
# ---------------------------------------------------------------------------

class TestReadNotifierLog(unittest.TestCase):
    """Tests for _read_notifier_log()."""

    def test_parses_json_quoted_lines(self):
        import tempfile
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, 'w') as f:
                f.write('"first message"\n')
                f.write('"second message"\n')
            result = _read_notifier_log(path)
            self.assertIn("[Notification: first message]", result)
            self.assertIn("[Notification: second message]", result)
            self.assertEqual(result.count("[Notification:"), 2)
        finally:
            os.unlink(path)

    def test_empty_file(self):
        import tempfile
        fd, path = tempfile.mkstemp()
        try:
            os.close(fd)
            result = _read_notifier_log(path)
            self.assertEqual(result, '')
        finally:
            os.unlink(path)

    def test_missing_file(self):
        result = _read_notifier_log("/nonexistent/path/log.txt")
        self.assertEqual(result, '')

    def test_unquoted_lines_pass_through(self):
        """Lines without JSON quotes should still be included."""
        import tempfile
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, 'w') as f:
                f.write("plain message\n")
            result = _read_notifier_log(path)
            self.assertIn("[Notification: plain message]", result)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _read_tool_log
# ---------------------------------------------------------------------------

class TestReadToolLog(unittest.TestCase):
    """Tests for _read_tool_log()."""

    def test_deduplicates_lines(self):
        import tempfile
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, 'w') as f:
                f.write("Read /tmp/foo.txt\n")
                f.write("Write /tmp/bar.txt\n")
                f.write("Read /tmp/foo.txt\n")  # duplicate
            result = _read_tool_log(path)
            self.assertEqual(result.count("[Tool: Read /tmp/foo.txt]"), 1)
            self.assertIn("[Tool: Write /tmp/bar.txt]", result)
        finally:
            os.unlink(path)

    def test_empty_file(self):
        import tempfile
        fd, path = tempfile.mkstemp()
        try:
            os.close(fd)
            result = _read_tool_log(path)
            self.assertEqual(result, '')
        finally:
            os.unlink(path)

    def test_missing_file(self):
        result = _read_tool_log("/nonexistent/path/tool.log")
        self.assertEqual(result, '')


# ---------------------------------------------------------------------------
# _persist_usage
# ---------------------------------------------------------------------------

class TestPersistUsage(unittest.TestCase):
    """Tests for _persist_usage()."""

    def test_inserts_into_token_usage(self):
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, 'history.db')
        try:
            # Create the table first
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE token_usage (
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
            conn.close()

            raw_json = {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                },
                "modelUsage": {"sonnet-4-20250514": {"inputTokens": 100}},
                "total_cost_usd": 0.005,
                "duration_ms": 1234,
            }

            _persist_usage(raw_json, db_path)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT * FROM token_usage").fetchone()
            conn.close()

            self.assertIsNotNone(row)
            # row: id, model, input_tokens, output_tokens, cache_read, cache_create, cost, duration, created
            self.assertEqual(row[1], "sonnet-4-20250514")
            self.assertEqual(row[2], 100)
            self.assertEqual(row[3], 50)
            self.assertEqual(row[4], 10)
            self.assertEqual(row[5], 5)
            self.assertAlmostEqual(row[6], 0.005)
            self.assertEqual(row[7], 1234)
        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    def test_handles_missing_table_gracefully(self):
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, 'history.db')
        try:
            # Create an empty db (no table) -- should not raise
            conn = sqlite3.connect(db_path)
            conn.close()
            raw_json = {"usage": {}, "modelUsage": {}, "total_cost_usd": 0, "duration_ms": 0}
            _persist_usage(raw_json, db_path)
            # No exception means success
        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    def test_empty_db_file_returns_early(self):
        """When db_file is empty string, _persist_usage should return silently."""
        _persist_usage({"usage": {}}, '')
        # No exception means success


# ---------------------------------------------------------------------------
# ClaudeResult
# ---------------------------------------------------------------------------

class TestClaudeResult(unittest.TestCase):
    """Tests for the ClaudeResult namedtuple."""

    def test_fields_accessible(self):
        r = ClaudeResult(
            response="hello",
            raw_json={"result": "hello"},
            notifier_messages="[Notification: sent]",
            tool_summary="[Tool: Read /tmp/x]",
        )
        self.assertEqual(r.response, "hello")
        self.assertEqual(r.raw_json, {"result": "hello"})
        self.assertEqual(r.notifier_messages, "[Notification: sent]")
        self.assertEqual(r.tool_summary, "[Tool: Read /tmp/x]")

    def test_is_namedtuple(self):
        r = ClaudeResult("a", None, "", "")
        self.assertIsInstance(r, tuple)
        self.assertEqual(r[0], "a")


# ---------------------------------------------------------------------------
# run_claude
# ---------------------------------------------------------------------------

class TestRunClaude(unittest.TestCase):
    """Tests for run_claude()."""

    def _make_tmp_config(self):
        """Create a BotConfig pointing to a temp directory."""
        import tempfile
        tmp = tempfile.mkdtemp()
        bot_dir = os.path.join(tmp, "bot")
        os.makedirs(bot_dir, exist_ok=True)
        db_file = os.path.join(tmp, "history.db")
        return BotConfig(
            bot_id="test-bot",
            bot_dir=bot_dir,
            telegram_token="tok:123",
            telegram_chat_id="999",
            model="sonnet",
            db_file=db_file,
        ), tmp

    @patch("lib.claude_runner.find_claude_cmd", return_value="/usr/bin/claude")
    @patch("lib.claude_runner.subprocess.Popen")
    def test_successful_run_json_output(self, mock_popen, mock_find):
        config, tmp = self._make_tmp_config()
        try:
            mock_proc = MagicMock()
            mock_proc.wait.return_value = 0
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            output_json = json.dumps({
                "result": "Hello from Claude!",
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "total_cost_usd": 0.001,
                "duration_ms": 500,
            })

            # We need to intercept the temp file writes. The function creates
            # temp files, writes the prompt, runs Popen, then reads output.
            # We patch the output file read to return our JSON.
            original_open = open

            def mock_open_side_effect(path, *args, **kwargs):
                # When reading the output file, return our JSON
                f = original_open(path, *args, **kwargs)
                return f

            # A simpler approach: after Popen is called, write to the output file.
            def popen_side_effect(cmd, **kwargs):
                # The stdout kwarg is a file handle for the output file.
                # Write our JSON output to it.
                stdout_file = kwargs.get('stdout')
                if stdout_file and hasattr(stdout_file, 'name'):
                    # stdout_file is an open file handle; write to the path
                    stdout_file.write(output_json)
                return mock_proc

            mock_popen.side_effect = popen_side_effect

            result = run_claude("hello", config)

            self.assertEqual(result.response, "Hello from Claude!")
            self.assertIsNotNone(result.raw_json)
            self.assertEqual(result.raw_json["result"], "Hello from Claude!")
            mock_popen.assert_called_once()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    @patch("lib.claude_runner.find_claude_cmd", return_value=None)
    def test_claude_not_found(self, mock_find):
        config, tmp = self._make_tmp_config()
        try:
            result = run_claude("hello", config)
            self.assertIn("not found", result.response)
            self.assertIsNone(result.raw_json)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    @patch("lib.claude_runner.find_claude_cmd", return_value="/usr/bin/claude")
    @patch("lib.claude_runner.subprocess.Popen")
    def test_timeout_handling(self, mock_popen, mock_find):
        config, tmp = self._make_tmp_config()
        try:
            mock_proc = MagicMock()
            mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=600)
            mock_proc.pid = 12345

            mock_popen.return_value = mock_proc

            # Patch _kill_process_group so it does not actually signal anything
            with patch("lib.claude_runner._kill_process_group") as mock_kill:
                result = run_claude("hello", config)
                mock_kill.assert_called_once_with(mock_proc)
                # Response should be empty or from empty output file
                self.assertIsInstance(result, ClaudeResult)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    @patch("lib.claude_runner.find_claude_cmd", return_value="/usr/bin/claude")
    @patch("lib.claude_runner.subprocess.Popen")
    def test_non_json_fallback(self, mock_popen, mock_find):
        config, tmp = self._make_tmp_config()
        try:
            mock_proc = MagicMock()
            mock_proc.wait.return_value = 0
            mock_proc.pid = 12345

            plain_text = "This is plain text, not JSON."

            def popen_side_effect(cmd, **kwargs):
                stdout_file = kwargs.get('stdout')
                if stdout_file and hasattr(stdout_file, 'write'):
                    stdout_file.write(plain_text)
                return mock_proc

            mock_popen.side_effect = popen_side_effect

            result = run_claude("hello", config)

            self.assertEqual(result.response, plain_text)
            self.assertIsNone(result.raw_json)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
