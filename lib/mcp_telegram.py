#!/usr/bin/env python3
"""
Minimal MCP (stdio) server for sending Telegram notifications.
Reads JSON-RPC from stdin, writes JSON-RPC to stdout.
Uses only Python stdlib — no external dependencies.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def respond(req_id, result):
    msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def error(req_id, code, message):
    msg = {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_telegram(text):
    """Send a message via Telegram Bot API. Returns (ok, detail)."""
    if not BOT_TOKEN or not CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured"

    url = f"{TELEGRAM_API}{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    ).encode()

    # Try with Markdown first, fall back to plain text
    for attempt_payload in [
        payload,
        urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode(),
    ]:
        try:
            req = urllib.request.Request(url, data=attempt_payload)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
                if body.get("ok"):
                    return True, "Message sent successfully"
        except (urllib.error.URLError, json.JSONDecodeError, OSError):
            continue

    return False, "Failed to send message after retries"


def handle(line):
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        return

    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        respond(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "telegram-notifier", "version": "1.0.0"},
        })

    elif method == "notifications/initialized":
        pass  # no response needed

    elif method == "tools/list":
        respond(req_id, {
            "tools": [{
                "name": "send_telegram_message",
                "description": (
                    "Send an async message to the user via Telegram. "
                    "Use this to send progress updates, partial results, or "
                    "notifications while you are still working on a task. "
                    "The message is delivered immediately and independently "
                    "of your final response. Use Telegram-compatible formatting: "
                    "*bold*, _italic_, `code`, ```code blocks```."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The message text to send to the user",
                        }
                    },
                    "required": ["message"],
                },
            }],
        })

    elif method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name != "send_telegram_message":
            error(req_id, -32601, f"Unknown tool: {tool_name}")
            return

        message = arguments.get("message", "")
        if not message:
            error(req_id, -32602, "Missing required parameter: message")
            return

        ok, detail = send_telegram(message)
        content = [{"type": "text", "text": detail}]
        if ok:
            respond(req_id, {"content": content})
        else:
            respond(req_id, {"content": content, "isError": True})

    else:
        if req_id is not None:
            error(req_id, -32601, f"Method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        handle(line)


if __name__ == "__main__":
    main()
