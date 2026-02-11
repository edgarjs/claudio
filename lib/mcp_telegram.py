#!/usr/bin/env python3
"""MCP stdio server that exposes send_telegram_message tool.

Uses only Python stdlib — no external dependencies.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTIFIER_LOG_FILE = os.environ.get("NOTIFIER_LOG_FILE", "")

# Validate token format to prevent SSRF via malicious env var
if BOT_TOKEN and not re.match(r"^[0-9]+:[a-zA-Z0-9_-]+$", BOT_TOKEN):
    BOT_TOKEN = ""


def _log_sent_message(text: str) -> None:
    """Append sent message to log file so caller can include it in history."""
    if not NOTIFIER_LOG_FILE:
        return
    try:
        with open(NOTIFIER_LOG_FILE, "a") as f:
            f.write(json.dumps(text) + "\n")
    except OSError as e:
        print(f"mcp_telegram: Failed to write to notifier log: {e}", file=sys.stderr)


def send_telegram_message(text: str) -> dict:
    """Send a message via Telegram Bot API with Markdown fallback."""
    if not BOT_TOKEN or not CHAT_ID:
        return {"error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"}

    url = f"{TELEGRAM_API}{BOT_TOKEN}/sendMessage"

    # Try with Markdown first, fall back to plain text
    for parse_mode in ("Markdown", None):
        data = {"chat_id": CHAT_ID, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode

        payload = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=payload)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                if result.get("ok"):
                    _log_sent_message(text)
                    return {"status": "ok"}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            # If Markdown failed, retry without it
            if parse_mode and e.code == 400:
                continue
            return {"error": f"HTTP {e.code}: {body}"}
        except urllib.error.URLError as e:
            return {"error": str(e.reason)}

    return {"error": "Failed to send message after all attempts"}


def handle_request(request: dict) -> dict:
    """Route a JSON-RPC request to the appropriate handler."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "telegram-notifier",
                    "version": "1.0.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "send_telegram_message",
                        "description": (
                            "Send an async message to the user via Telegram. "
                            "Use this to send progress updates, partial results, "
                            "or notifications while you are still working on a task. "
                            "The message is delivered immediately and independently "
                            "of your final response. "
                            "Use Telegram-compatible formatting: "
                            "*bold*, _italic_, `code`, ```code blocks```."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "The message text to send to the user",
                                },
                            },
                            "required": ["message"],
                        },
                    }
                ]
            },
        }

    if method == "tools/call":
        tool_name = request.get("params", {}).get("name", "")
        args = request.get("params", {}).get("arguments", {})

        if tool_name == "send_telegram_message":
            message = args.get("message", "")
            if not message:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "Error: empty message"}],
                        "isError": True,
                    },
                }
            result = send_telegram_message(message)
            is_error = "error" in result
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": is_error,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            },
        }

    # Unknown method
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }
    return None


def main():
    """Read JSON-RPC messages from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
