#!/usr/bin/env python3
"""Tests for mcp_telegram.py — notifier log functionality."""

import json
import os
import sys
import tempfile
import unittest

# Add lib/ to path so we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))


class TestLogSentMessage(unittest.TestCase):
    """Test _log_sent_message writes to NOTIFIER_LOG_FILE."""

    def setUp(self):
        self.log_fd, self.log_path = tempfile.mkstemp()
        os.close(self.log_fd)
        # Patch NOTIFIER_LOG_FILE before importing (module reads it at import)
        os.environ["NOTIFIER_LOG_FILE"] = self.log_path
        # Force re-import to pick up the new env var
        if "mcp_telegram" in sys.modules:
            del sys.modules["mcp_telegram"]
        import mcp_telegram

        self.mod = mcp_telegram

    def tearDown(self):
        os.unlink(self.log_path)
        os.environ.pop("NOTIFIER_LOG_FILE", None)
        if "mcp_telegram" in sys.modules:
            del sys.modules["mcp_telegram"]

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
        if "mcp_telegram" in sys.modules:
            del sys.modules["mcp_telegram"]
        import mcp_telegram

        # Truncate the file first to ensure nothing is written
        with open(self.log_path, "w"):
            pass
        mcp_telegram._log_sent_message("should not be logged")
        with open(self.log_path) as f:
            self.assertEqual(f.read(), "")


if __name__ == "__main__":
    unittest.main()
