#!/usr/bin/env python3
"""Tests for mcp_tools.py — notifier log and service management tools."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_tools_list_returns_two_tools(self):
        resp = self.mod.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(tools), 2)
        self.assertIn("send_telegram_message", names)
        self.assertIn("restart_service", names)

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

    def test_restart_rejects_non_numeric_delay(self):
        result = self.mod.restart_service(delay="abc")
        self.assertIn("error", result)
        self.assertIn("Invalid delay", result["error"])

    @patch("mcp_tools.subprocess.Popen", side_effect=OSError("mock failure"))
    def test_restart_popen_failure(self, mock_popen):
        result = self.mod.restart_service()
        self.assertIn("error", result)
        self.assertIn("mock failure", result["error"])


if __name__ == "__main__":
    unittest.main()
