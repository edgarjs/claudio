#!/usr/bin/env python3
"""MCP stdio server exposing Claudio tools.

Tools:
  - send_telegram_message: async Telegram notifications
  - restart_service: schedule a delayed service restart
  - update_service: git pull + delayed service restart

Uses only Python stdlib — no external dependencies.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment.
"""

import json
import os
import platform
import re
import subprocess
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

# Project root (parent of lib/)
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DELAY = 5


def _log_sent_message(text: str) -> None:
    """Append sent message to log file so caller can include it in history."""
    if not NOTIFIER_LOG_FILE:
        return
    try:
        with open(NOTIFIER_LOG_FILE, "a") as f:
            f.write(json.dumps(text) + "\n")
    except OSError as e:
        print(f"mcp_tools: Failed to write to notifier log: {e}", file=sys.stderr)


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


def _validate_delay(delay) -> tuple:
    """Validate and clamp delay to [1, 300]. Returns (value, error_dict)."""
    try:
        return max(1, min(300, int(delay))), None
    except (ValueError, TypeError):
        return None, {"error": f"Invalid delay: {delay!r}"}


def _schedule_restart(delay: int) -> dict:
    """Spawn a detached process that sleeps then restarts the service."""
    delay, err = _validate_delay(delay)
    if err:
        return err

    if platform.system() == "Darwin":
        cmd = (
            f"sleep {delay} && "
            "launchctl stop com.claudio.server; "
            "launchctl start com.claudio.server"
        )
    else:
        cmd = f"sleep {delay} && systemctl --user restart claudio"

    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"status": "ok", "message": f"Restart scheduled in {delay}s"}
    except OSError as e:
        return {"error": f"Failed to schedule restart: {e}"}


def restart_service(delay: int = DEFAULT_DELAY) -> dict:
    """Schedule a delayed service restart."""
    return _schedule_restart(delay)


def update_service(delay: int = DEFAULT_DELAY) -> dict:
    """Git pull --ff-only then schedule a delayed service restart."""
    if not os.path.isdir(os.path.join(PROJECT_DIR, ".git")):
        return {"error": f"Not a git repository: {PROJECT_DIR}"}

    delay, err = _validate_delay(delay)
    if err:
        return err

    try:
        # Capture HEAD before pull to detect changes (locale-independent)
        head_before = subprocess.run(
            ["git", "-C", PROJECT_DIR, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        result = subprocess.run(
            ["git", "-C", PROJECT_DIR, "pull", "--ff-only", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return {"error": f"git pull failed: {result.stderr.strip()}"}

        head_after = subprocess.run(
            ["git", "-C", PROJECT_DIR, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Compare HEAD hashes to detect changes
        if (
            head_before.returncode == 0
            and head_after.returncode == 0
            and head_before.stdout.strip() == head_after.stdout.strip()
        ):
            return {
                "status": "ok",
                "message": "Already up to date",
                "restarting": False,
            }

        # Schedule restart after successful pull
        restart_result = _schedule_restart(delay)
        if "error" in restart_result:
            return {
                "error": f"Updated but restart failed: {restart_result['error']}"
            }

        return {
            "status": "ok",
            "message": f"Updated and restart scheduled in {delay}s",
            "pull_output": result.stdout.strip(),
            "restarting": True,
        }
    except subprocess.TimeoutExpired:
        return {"error": "git pull timed out after 60s"}
    except OSError as e:
        return {"error": f"Failed to run git pull: {e}"}


# --- MCP protocol handling ---

TOOL_DEFINITIONS = [
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
    },
    {
        "name": "restart_service",
        "description": (
            "Schedule a delayed restart of the Claudio service. "
            "The restart is deferred so the current turn can finish "
            "and the response can be delivered before the service stops. "
            "Use this instead of running systemctl/launchctl directly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "delay_seconds": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait before restarting (default 5)"
                    ),
                    "default": 5,
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": [],
        },
    },
    {
        "name": "update_service",
        "description": (
            "Update Claudio by pulling the latest code from git, "
            "then schedule a delayed service restart. "
            "Performs git pull --ff-only origin main. "
            "If already up to date, skips the restart. "
            "Use this instead of manually running git pull + restart."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "delay_seconds": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait before restarting (default 5)"
                    ),
                    "default": 5,
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": [],
        },
    },
]

TOOL_HANDLERS = {
    "send_telegram_message": lambda args: (
        send_telegram_message(args.get("message", ""))
        if args.get("message")
        else {"error": "empty message"}
    ),
    "restart_service": lambda args: restart_service(
        delay=args.get("delay_seconds", DEFAULT_DELAY)
    ),
    "update_service": lambda args: update_service(
        delay=args.get("delay_seconds", DEFAULT_DELAY)
    ),
}


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
                    "name": "claudio-tools",
                    "version": "2.0.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }

    if method == "tools/call":
        tool_name = request.get("params", {}).get("name", "")
        args = request.get("params", {}).get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": f"Unknown tool: {tool_name}"}
                    ],
                    "isError": True,
                },
            }

        result = handler(args)
        is_error = "error" in result
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": is_error,
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
