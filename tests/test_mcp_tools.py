#!/usr/bin/env python3
"""Tests for mcp_tools.py — notifier log and service management tools."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Add lib/ to path so we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

MODULE_NAME = "mcp_tools"


class TestLogSentMessage(unittest.TestCase):
    """Test _log_sent_message writes to NOTIFIER_LOG_FILE."""

    def setUp(self):
        self.log_fd, self.log_path = tempfile.mkstemp()
        os.close(self.log_fd)
        # Patch NOTIFIER_LOG_FILE before importing (module reads it at import)
        os.environ["NOTIFIER_LOG_FILE"] = self.log_path
        # Force re-import to pick up the new env var
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]
        import mcp_tools

        self.mod = mcp_tools

    def tearDown(self):
        os.unlink(self.log_path)
        os.environ.pop("NOTIFIER_LOG_FILE", None)
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]

    def test_log_writes_json_line(self):
        self.mod._log_sent_message("hello world")
        with open(self.log_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0].strip()), "hello world")

    def test_log_multiple_messages(self):
        self.mod._log_sent_message("first")
        self.mod._log_sent_message("second")
        with open(self.log_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0].strip()), "first")
        self.assertEqual(json.loads(lines[1].strip()), "second")

    def test_log_handles_special_characters(self):
        msg = 'message with "quotes" and\nnewlines'
        self.mod._log_sent_message(msg)
        with open(self.log_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0].strip()), msg)

    def test_log_noop_when_no_log_file(self):
        """When NOTIFIER_LOG_FILE is empty, _log_sent_message is a no-op."""
        os.environ["NOTIFIER_LOG_FILE"] = ""
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]
        import mcp_tools

        # Truncate the file first to ensure nothing is written
        with open(self.log_path, "w"):
            pass
        mcp_tools._log_sent_message("should not be logged")
        with open(self.log_path) as f:
            self.assertEqual(f.read(), "")


class TestToolDefinitions(unittest.TestCase):
    """Test that MCP tool definitions are well-formed."""

    def setUp(self):
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]
        import mcp_tools

        self.mod = mcp_tools

    def tearDown(self):
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]

    def test_all_tools_have_handlers(self):
        tool_names = {t["name"] for t in self.mod.TOOL_DEFINITIONS}
        handler_names = set(self.mod.TOOL_HANDLERS.keys())
        self.assertEqual(tool_names, handler_names)

    def test_tool_definitions_have_required_fields(self):
        for tool in self.mod.TOOL_DEFINITIONS:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)

    def test_initialize_returns_claudio_tools(self):
        resp = self.mod.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        self.assertEqual(
            resp["result"]["serverInfo"]["name"], "claudio-tools"
        )

    def test_tools_list_returns_three_tools(self):
        resp = self.mod.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("send_telegram_message", names)
        self.assertIn("restart_service", names)
        self.assertIn("update_service", names)

    def test_unknown_tool_returns_error(self):
        resp = self.mod.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "nonexistent", "arguments": {}},
            }
        )
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("Unknown tool", resp["result"]["content"][0]["text"])

    def test_empty_message_returns_specific_error(self):
        """send_telegram_message with empty message returns 'empty message' error."""
        resp = self.mod.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "send_telegram_message", "arguments": {}},
            }
        )
        self.assertTrue(resp["result"]["isError"])
        result = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("empty message", result["error"])

    def test_missing_message_returns_specific_error(self):
        """send_telegram_message with missing message key returns error."""
        resp = self.mod.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "send_telegram_message",
                    "arguments": {"message": ""},
                },
            }
        )
        self.assertTrue(resp["result"]["isError"])
        result = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("empty message", result["error"])


class TestRestartService(unittest.TestCase):
    """Test restart_service spawns a detached process with correct args."""

    def setUp(self):
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]
        import mcp_tools

        self.mod = mcp_tools

    def tearDown(self):
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]

    @patch("mcp_tools.subprocess.Popen")
    def test_restart_spawns_detached_process(self, mock_popen):
        result = self.mod.restart_service(delay=5)
        self.assertEqual(result["status"], "ok")
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args
        self.assertTrue(call_kwargs.kwargs["start_new_session"])
        self.assertEqual(call_kwargs.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(call_kwargs.kwargs["stderr"], subprocess.DEVNULL)
        cmd = call_kwargs.args[0]
        self.assertEqual(cmd[0], "bash")
        self.assertEqual(cmd[1], "-c")
        self.assertIn("sleep 5", cmd[2])

    @patch("mcp_tools.subprocess.Popen")
    def test_restart_default_delay(self, mock_popen):
        result = self.mod.restart_service()
        self.assertEqual(result["status"], "ok")
        self.assertIn("5s", result["message"])
        cmd = mock_popen.call_args.args[0][2]
        self.assertIn("sleep 5", cmd)

    @patch("mcp_tools.subprocess.Popen")
    def test_restart_custom_delay(self, mock_popen):
        result = self.mod.restart_service(delay=10)
        self.assertEqual(result["status"], "ok")
        self.assertIn("10s", result["message"])
        cmd = mock_popen.call_args.args[0][2]
        self.assertIn("sleep 10", cmd)

    @patch("mcp_tools.subprocess.Popen")
    def test_restart_clamps_delay_minimum(self, mock_popen):
        result = self.mod.restart_service(delay=0)
        self.assertEqual(result["status"], "ok")
        self.assertIn("1s", result["message"])
        cmd = mock_popen.call_args.args[0][2]
        self.assertIn("sleep 1", cmd)

    @patch("mcp_tools.subprocess.Popen")
    def test_restart_clamps_delay_maximum(self, mock_popen):
        result = self.mod.restart_service(delay=999)
        self.assertEqual(result["status"], "ok")
        self.assertIn("300s", result["message"])
        cmd = mock_popen.call_args.args[0][2]
        self.assertIn("sleep 300", cmd)

    @patch("mcp_tools.subprocess.Popen")
    def test_restart_casts_string_delay_to_int(self, mock_popen):
        result = self.mod.restart_service(delay="10")
        self.assertEqual(result["status"], "ok")
        cmd = mock_popen.call_args.args[0][2]
        self.assertIn("sleep 10", cmd)

    @patch("mcp_tools.subprocess.Popen")
    def test_restart_via_mcp(self, mock_popen):
        resp = self.mod.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "restart_service",
                    "arguments": {"delay_seconds": 3},
                },
            }
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(result["status"], "ok")
        self.assertIn("3s", result["message"])
        mock_popen.assert_called_once()

    @patch("mcp_tools.subprocess.Popen", side_effect=OSError("mock failure"))
    def test_restart_popen_failure(self, mock_popen):
        result = self.mod.restart_service()
        self.assertIn("error", result)
        self.assertIn("mock failure", result["error"])


class TestUpdateService(unittest.TestCase):
    """Test update_service runs git pull and schedules restart."""

    def setUp(self):
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]
        import mcp_tools

        self.mod = mcp_tools

    def tearDown(self):
        if MODULE_NAME in sys.modules:
            del sys.modules[MODULE_NAME]

    @patch("mcp_tools._schedule_restart")
    @patch("mcp_tools.subprocess.run")
    def test_update_already_up_to_date(self, mock_run, mock_restart):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Already up to date.\n", stderr=""
        )
        result = self.mod.update_service()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["restarting"])
        self.assertIn("Already up to date", result["message"])
        mock_restart.assert_not_called()

    @patch("mcp_tools._schedule_restart", return_value={"status": "ok", "message": "Restart scheduled in 5s"})
    @patch("mcp_tools.subprocess.run")
    def test_update_with_changes_triggers_restart(self, mock_run, mock_restart):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Updating abc1234..def5678\nFast-forward\n lib/mcp_tools.py | 5 ++---\n",
            stderr="",
        )
        result = self.mod.update_service(delay=7)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["restarting"])
        self.assertIn("7s", result["message"])
        mock_restart.assert_called_once_with(7)

    @patch("mcp_tools._schedule_restart")
    @patch("mcp_tools.subprocess.run")
    def test_update_pull_failure(self, mock_run, mock_restart):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="fatal: Not possible to fast-forward, aborting.\n",
        )
        result = self.mod.update_service()
        self.assertIn("error", result)
        self.assertIn("git pull failed", result["error"])
        mock_restart.assert_not_called()

    @patch("mcp_tools._schedule_restart")
    @patch("mcp_tools.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 60))
    def test_update_timeout(self, mock_run, mock_restart):
        result = self.mod.update_service()
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])
        mock_restart.assert_not_called()

    @patch("mcp_tools._schedule_restart")
    @patch("mcp_tools.subprocess.run", side_effect=OSError("git not found"))
    def test_update_git_not_found(self, mock_run, mock_restart):
        result = self.mod.update_service()
        self.assertIn("error", result)
        self.assertIn("git not found", result["error"])
        mock_restart.assert_not_called()

    @patch("mcp_tools._schedule_restart", return_value={"error": "spawn failed"})
    @patch("mcp_tools.subprocess.run")
    def test_update_restart_failure(self, mock_run, mock_restart):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Updating abc1234..def5678\nFast-forward\n",
            stderr="",
        )
        result = self.mod.update_service()
        self.assertIn("error", result)
        self.assertIn("Updated but restart failed", result["error"])

    @patch("mcp_tools._schedule_restart", return_value={"status": "ok", "message": "Restart scheduled in 5s"})
    @patch("mcp_tools.subprocess.run")
    def test_update_via_mcp(self, mock_run, mock_restart):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Updating abc..def\nFast-forward\n",
            stderr="",
        )
        resp = self.mod.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "update_service",
                    "arguments": {"delay_seconds": 10},
                },
            }
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["restarting"])
        mock_restart.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
