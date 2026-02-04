#!/usr/bin/env python3

import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDIO_BIN = os.path.join(SCRIPT_DIR, "..", "claudio")
CLAUDIO_PATH = os.path.join(os.path.expanduser("~"), ".claudio")
LOG_FILE = os.path.join(CLAUDIO_PATH, "claudio.log")
PORT = int(os.environ.get("PORT", 8421))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# Per-chat message queues for serial processing
chat_queues = {}  # chat_id -> deque of webhook bodies
queue_lock = threading.Lock()  # Global lock for accessing chat_queues
seen_updates = set()  # Track processed update_ids to prevent duplicates
MAX_SEEN_UPDATES = 1000  # Limit memory usage


def parse_webhook(body):
    """Extract update_id and chat_id from webhook body."""
    try:
        data = json.loads(body)
        update_id = data.get("update_id")
        chat_id = str(data.get("message", {}).get("chat", {}).get("id", ""))
        return update_id, chat_id
    except (json.JSONDecodeError, AttributeError):
        return None, ""


def process_queue(chat_id):
    """Process messages for a chat one at a time."""
    while True:
        with queue_lock:
            if chat_id not in chat_queues or not chat_queues[chat_id]:
                # Queue empty, clean up
                if chat_id in chat_queues:
                    del chat_queues[chat_id]
                return
            body = chat_queues[chat_id].popleft()

        # Process this message (blocking)
        try:
            with open(LOG_FILE, "a") as log_fh:
                # Ensure PATH includes ~/.local/bin for claude command
                env = os.environ.copy()
                home = os.path.expanduser("~")
                local_bin = os.path.join(home, ".local", "bin")
                if local_bin not in env.get("PATH", "").split(os.pathsep):
                    env["PATH"] = f"{local_bin}{os.pathsep}{env.get('PATH', '')}"

                proc = subprocess.Popen(
                    [CLAUDIO_BIN, "_webhook", body],
                    stdout=log_fh,
                    stderr=log_fh,
                    env=env,
                )
                proc.wait()  # Wait for completion before processing next
        except Exception as e:
            sys.stderr.write(f"[queue] Error processing message for chat {chat_id}: {e}\n")


def enqueue_webhook(body):
    """Add webhook to per-chat queue and start processor if needed."""
    update_id, chat_id = parse_webhook(body)
    if not chat_id:
        return  # Invalid webhook, skip

    with queue_lock:
        # Deduplicate: skip if we've already seen this update_id
        if update_id is not None:
            if update_id in seen_updates:
                return  # Duplicate webhook, skip
            seen_updates.add(update_id)
            # Limit memory usage by pruning old entries
            if len(seen_updates) > MAX_SEEN_UPDATES:
                # Remove oldest entries (set doesn't preserve order, so clear half)
                to_remove = list(seen_updates)[:MAX_SEEN_UPDATES // 2]
                for uid in to_remove:
                    seen_updates.discard(uid)

        # Initialize queue for this chat if needed
        if chat_id not in chat_queues:
            chat_queues[chat_id] = deque()

        chat_queues[chat_id].append(body)

        # Start processor thread if this is the only message (queue was empty)
        if len(chat_queues[chat_id]) == 1:
            thread = threading.Thread(
                target=process_queue,
                args=(chat_id,),
                daemon=True
            )
            thread.start()


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write("[%s] [http] %s\n" % (self.log_date_time_string(), format % args))

    def do_POST(self):
        if self.path == "/telegram/webhook":
            # Verify webhook secret (required for security)
            token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            # Use constant-time comparison to prevent timing attacks
            if not hmac.compare_digest(token, WEBHOOK_SECRET):
                self._respond(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            self._respond(200, {"ok": True})
            # Add to per-chat queue for serial processing
            enqueue_webhook(body)
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/health":
            health = check_health()
            code = 200 if health["status"] == "healthy" else 503
            self._respond(code, health)
        else:
            self._respond(404, {"error": "not found"})


def check_health():
    """Verify system health by checking Telegram webhook status."""
    checks = {}
    status = "healthy"

    # Check 1: Telegram webhook configuration
    if TELEGRAM_BOT_TOKEN and WEBHOOK_URL:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            if data.get("ok"):
                result = data.get("result", {})
                current_url = result.get("url", "")
                expected_url = f"{WEBHOOK_URL}/telegram/webhook"
                pending = result.get("pending_update_count", 0)
                last_error = result.get("last_error_message", "")

                if current_url == expected_url:
                    checks["telegram_webhook"] = {
                        "status": "ok",
                        "pending_updates": pending,
                    }
                    if last_error:
                        checks["telegram_webhook"]["last_error"] = last_error
                else:
                    checks["telegram_webhook"] = {
                        "status": "mismatch",
                        "expected": expected_url,
                        "actual": current_url,
                    }
                    status = "unhealthy"
                    # Try to re-register webhook
                    _register_webhook(expected_url)
            else:
                checks["telegram_webhook"] = {"status": "error", "detail": "API returned not ok"}
                status = "unhealthy"
                # Try to re-register webhook
                expected_url = f"{WEBHOOK_URL}/telegram/webhook"
                _register_webhook(expected_url)
        except (urllib.error.URLError, TimeoutError) as e:
            checks["telegram_webhook"] = {"status": "error", "detail": str(e)}
            status = "unhealthy"
            # Try to re-register webhook
            expected_url = f"{WEBHOOK_URL}/telegram/webhook"
            _register_webhook(expected_url)
    else:
        checks["telegram_webhook"] = {"status": "not_configured"}

    return {"status": status, "checks": checks}


def _register_webhook(webhook_url):
    """Attempt to re-register the Telegram webhook."""
    try:
        data = f"url={webhook_url}"
        if WEBHOOK_SECRET:
            data += f"&secret_token={WEBHOOK_SECRET}"
        data += "&allowed_updates=[\"message\"]"

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
        req = urllib.request.Request(
            url,
            data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except Exception:
        return False


def startup_health_check():
    """Run health check after startup to ensure webhook is registered."""
    # Small delay to ensure server is ready
    time.sleep(2)
    print("Running startup health check...")
    health = check_health()
    if health["status"] == "healthy":
        print("Startup health check passed")
    else:
        print(f"Startup health check: {health}")


def main():
    # Require WEBHOOK_SECRET for security
    if not WEBHOOK_SECRET:
        sys.stderr.write("Error: WEBHOOK_SECRET environment variable is required.\n")
        sys.stderr.write("Generate one with: openssl rand -hex 32\n")
        sys.exit(1)

    # Bind to localhost only - cloudflared handles external access
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claudio server listening on port {PORT}")

    # Run health check in background thread
    health_thread = threading.Thread(target=startup_health_check, daemon=True)
    health_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
