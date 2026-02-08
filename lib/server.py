#!/usr/bin/env python3

import hmac
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import deque, OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

MAX_BODY_SIZE = 1024 * 1024  # 1 MB
MAX_QUEUE_SIZE = 100  # Max queued messages per chat
WEBHOOK_TIMEOUT = 600  # 10 minutes max per Claude invocation
HEALTH_CACHE_TTL = 30  # seconds between health check API calls
QUEUE_WARNING_RATIO = 0.8  # Warn when queue reaches this fraction of max


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDIO_BIN = os.path.join(SCRIPT_DIR, "..", "claudio")
CLAUDIO_PATH = os.path.join(os.path.expanduser("~"), ".claudio")
LOG_FILE = os.path.join(CLAUDIO_PATH, "claudio.log")
MEMORY_SOCKET = os.path.join(CLAUDIO_PATH, "memory.sock")
MEMORY_DAEMON_LOG = os.path.join(CLAUDIO_PATH, "memory-daemon.log")
PORT = int(os.environ.get("PORT", 8421))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# Per-chat message queues for serial processing
chat_queues = {}  # chat_id -> deque of webhook bodies
chat_active = {}  # chat_id -> bool, True if a processor thread is running
active_threads = []  # Non-daemon processor threads to wait on during shutdown
queue_lock = threading.Lock()
seen_updates = OrderedDict()  # Track processed update_ids to prevent duplicates
MAX_SEEN_UPDATES = 1000
shutting_down = False  # Set to True on SIGTERM to reject new webhooks

# Health check cache
_health_cache = {"result": None, "time": 0}


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
    current = threading.current_thread()
    try:
        _process_queue_loop(chat_id)
    finally:
        with queue_lock:
            if current in active_threads:
                active_threads.remove(current)


def _process_queue_loop(chat_id):
    """Inner loop for process_queue, separated for clean thread tracking."""
    while True:
        with queue_lock:
            if chat_id not in chat_queues or not chat_queues[chat_id]:
                # Queue empty, clean up
                if chat_id in chat_queues:
                    del chat_queues[chat_id]
                chat_active.pop(chat_id, None)
                return
            body = chat_queues[chat_id].popleft()

        proc = None
        try:
            with open(LOG_FILE, "a") as log_fh:
                # Ensure PATH includes ~/.local/bin for claude command
                env = os.environ.copy()
                home = os.path.expanduser("~")
                local_bin = os.path.join(home, ".local", "bin")
                if local_bin not in env.get("PATH", "").split(os.pathsep):
                    env["PATH"] = f"{local_bin}{os.pathsep}{env.get('PATH', '')}"

                proc = subprocess.Popen(
                    [CLAUDIO_BIN, "_webhook"],
                    stdin=subprocess.PIPE,
                    stdout=log_fh,
                    stderr=log_fh,
                    env=env,
                    start_new_session=True,
                )
                proc.communicate(input=body.encode(), timeout=WEBHOOK_TIMEOUT)
                if proc.returncode != 0:
                    sys.stderr.write(
                        f"[queue] Webhook handler exited with code {proc.returncode} "
                        f"for chat {chat_id}\n"
                    )
        except subprocess.TimeoutExpired:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                except OSError:
                    try:
                        proc.wait()
                    except Exception:
                        pass
            sys.stderr.write(f"[queue] Timeout processing message for chat {chat_id}\n")
        except Exception as e:
            sys.stderr.write(f"[queue] Error processing message for chat {chat_id}: {e}\n")
            time.sleep(1)  # Avoid tight loop on persistent errors


def enqueue_webhook(body):
    """Add webhook to per-chat queue and start processor if needed."""
    update_id, chat_id = parse_webhook(body)
    if not chat_id:
        return  # Invalid webhook, skip

    # Validate chat_id against authorized chat (defense in depth, also checked in bash)
    if not TELEGRAM_CHAT_ID or chat_id != TELEGRAM_CHAT_ID:
        return

    with queue_lock:
        # Reject new messages during shutdown — let active handlers finish
        if shutting_down:
            sys.stderr.write(f"[queue] Rejecting webhook during shutdown for chat {chat_id}\n")
            return

        # Deduplicate: skip if we've already seen this update_id
        if update_id is not None:
            if update_id in seen_updates:
                return  # Duplicate webhook, skip
            seen_updates[update_id] = True
            while len(seen_updates) > MAX_SEEN_UPDATES:
                seen_updates.popitem(last=False)

        # Initialize queue for this chat if needed
        if chat_id not in chat_queues:
            chat_queues[chat_id] = deque()

        # Prevent unbounded queue growth
        queue_size = len(chat_queues[chat_id])
        if queue_size >= MAX_QUEUE_SIZE:
            sys.stderr.write(f"[queue] Queue full for chat {chat_id} ({queue_size}/{MAX_QUEUE_SIZE}), dropping message\n")
            return
        if queue_size >= MAX_QUEUE_SIZE * QUEUE_WARNING_RATIO:
            sys.stderr.write(f"[queue] Warning: queue for chat {chat_id} at {queue_size}/{MAX_QUEUE_SIZE} ({queue_size * 100 // MAX_QUEUE_SIZE}%)\n")

        chat_queues[chat_id].append(body)

        if not chat_active.get(chat_id):
            chat_active[chat_id] = True
            thread = threading.Thread(
                target=process_queue,
                args=(chat_id,),
                daemon=False,
            )
            active_threads.append(thread)
            thread.start()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


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
            # Reject early during shutdown so Telegram retries later
            if shutting_down:
                self._respond(503, {"error": "shutting down"})
                return
            # Verify webhook secret before reading body
            token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(token, WEBHOOK_SECRET):
                self._respond(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                self._respond(400, {"error": "invalid content-length"})
                return
            if length > MAX_BODY_SIZE:
                self._respond(413, {"error": "payload too large"})
                return
            body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            self._respond(200, {"ok": True})
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
    now = time.monotonic()
    if _health_cache["result"] and 0 <= (now - _health_cache["time"]) < HEALTH_CACHE_TTL:
        return _health_cache["result"]

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

    # Check 2: Memory daemon (non-critical — degrades gracefully)
    checks["memory_daemon"] = _check_memory_daemon()

    result = {"status": status, "checks": checks}
    # Only cache healthy results — unhealthy states should be re-checked
    # immediately so recovery is detected without waiting for TTL expiry
    if status == "healthy":
        _health_cache["result"] = result
        _health_cache["time"] = time.monotonic()
    else:
        _health_cache["result"] = None
    return result


def _register_webhook(webhook_url):
    """Attempt to re-register the Telegram webhook."""
    try:
        params = {"url": webhook_url, "allowed_updates": '["message"]'}
        if WEBHOOK_SECRET:
            params["secret_token"] = WEBHOOK_SECRET
        data = urllib.parse.urlencode(params)

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


def _graceful_shutdown(server, shutdown_event):
    """Wait for SIGTERM, then stop accepting requests and drain active handlers."""
    global shutting_down
    shutdown_event.wait()

    with queue_lock:
        shutting_down = True
    sys.stderr.write("[shutdown] SIGTERM received, draining active handlers...\n")

    # Stop accepting new HTTP requests
    server.shutdown()

    # Wait for all active processor threads to finish their current message
    with queue_lock:
        threads_to_wait = list(active_threads)
    if threads_to_wait:
        sys.stderr.write(f"[shutdown] Waiting for {len(threads_to_wait)} active handler(s)...\n")
        for t in threads_to_wait:
            t.join(timeout=WEBHOOK_TIMEOUT + 10)
            if t.is_alive():
                sys.stderr.write(f"[shutdown] WARNING: thread {t.name} still alive after timeout\n")
    sys.stderr.write("[shutdown] All handlers finished, exiting cleanly.\n")


def _start_cloudflared():
    """Start cloudflared tunnel as a subprocess managed by Python.

    Returns the Popen object, or None if no tunnel is configured.
    Logs are written to cloudflared.log with size rotation (10 MB max, 1 backup).
    """
    tunnel_name = os.environ.get("TUNNEL_NAME", "")
    if not tunnel_name:
        sys.stderr.write("[cloudflared] No tunnel configured, skipping.\n")
        return None

    log_path = os.path.join(CLAUDIO_PATH, "cloudflared.log")

    # Rotate: keep max 10 MB, rename old log
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 10 * 1024 * 1024:
            backup = log_path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(log_path, backup)
    except OSError:
        pass

    log_fh = open(log_path, "a")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "run",
         "--url", f"http://localhost:{PORT}", tunnel_name],
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    log_fh.close()  # Parent doesn't need the fd after Popen inherits it
    sys.stderr.write(f"[cloudflared] Named tunnel '{tunnel_name}' started (pid {proc.pid}).\n")
    return proc


def _stop_cloudflared(proc):
    """Gracefully stop the cloudflared process."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass
    sys.stderr.write("[cloudflared] Tunnel stopped.\n")


# Module-level reference so check_health() can inspect the daemon process
_memory_proc = None
_memory_restart_count = 0
_memory_last_restart = 0
_MEMORY_MAX_RESTARTS = 3
_MEMORY_RESTART_COOLDOWN = 300  # seconds


def _start_memory_daemon():
    """Start the memory daemon as a subprocess.

    Returns the Popen object, or None on failure.
    """
    memory_py = os.path.join(SCRIPT_DIR, "memory.py")
    if not os.path.isfile(memory_py):
        sys.stderr.write("[memory-daemon] memory.py not found, skipping.\n")
        return None

    log_path = MEMORY_DAEMON_LOG
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 10 * 1024 * 1024:
            backup = log_path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(log_path, backup)
    except OSError:
        pass

    log_fh = open(log_path, "a")
    try:
        proc = subprocess.Popen(
            [sys.executable, memory_py, "serve"],
            stdout=subprocess.PIPE,
            stderr=log_fh,
            start_new_session=True,
        )
    except Exception as e:
        log_fh.close()
        sys.stderr.write(f"[memory-daemon] Failed to start: {e}\n")
        return None

    # Wait for readiness signal (MEMORY_DAEMON_READY on stdout)
    deadline = time.monotonic() + 30
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if os.path.exists(MEMORY_SOCKET):
            ready = True
            break
        time.sleep(0.2)

    # Close stdout pipe — daemon should only log to stderr
    try:
        proc.stdout.close()
    except Exception:
        pass

    if ready:
        sys.stderr.write(f"[memory-daemon] Started (pid {proc.pid}).\n")
        log_fh.close()
        return proc
    else:
        sys.stderr.write("[memory-daemon] Failed to start within 30s, continuing without it.\n")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        log_fh.close()
        return None


def _stop_memory_daemon(proc):
    """Gracefully stop the memory daemon."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass
    sys.stderr.write("[memory-daemon] Stopped.\n")


def _check_memory_daemon():
    """Check memory daemon health for inclusion in health check response.

    Returns a status dict. Does NOT affect overall health status since
    the daemon degrades gracefully (falls back to local execution).
    """
    global _memory_proc, _memory_restart_count, _memory_last_restart

    check = {}
    # Check if process is alive; attempt restart with rate limiting
    if _memory_proc is not None and _memory_proc.poll() is not None:
        now = time.monotonic()
        if now - _memory_last_restart > _MEMORY_RESTART_COOLDOWN:
            _memory_restart_count = 0  # Reset counter after cooldown
        if _memory_restart_count < _MEMORY_MAX_RESTARTS:
            _memory_restart_count += 1
            _memory_last_restart = now
            sys.stderr.write(
                f"[memory-daemon] Process died, attempting restart "
                f"({_memory_restart_count}/{_MEMORY_MAX_RESTARTS}).\n"
            )
            _memory_proc = _start_memory_daemon()
        else:
            sys.stderr.write(
                "[memory-daemon] Max restarts reached, giving up until server restart.\n"
            )

    if _memory_proc is None:
        check["status"] = "down"
        check["detail"] = "not running"
        return check

    # Ping via socket
    import socket as sock_mod
    s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(MEMORY_SOCKET)
        s.sendall(b'{"command":"ping"}\n')
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        resp = json.loads(data.strip())
        if resp.get("ok"):
            check["status"] = "ok"
        else:
            check["status"] = "error"
            check["detail"] = "unexpected response"
    except Exception as e:
        check["status"] = "error"
        check["detail"] = str(e)
    finally:
        s.close()

    return check


def main():
    # Require WEBHOOK_SECRET for security
    if not WEBHOOK_SECRET:
        sys.stderr.write("Error: WEBHOOK_SECRET environment variable is required.\n")
        sys.stderr.write("Generate one with: openssl rand -hex 32\n")
        sys.exit(1)

    # Warn if TELEGRAM_CHAT_ID is not set (server will reject all messages)
    if not TELEGRAM_CHAT_ID:
        sys.stderr.write("Warning: TELEGRAM_CHAT_ID not set — all messages will be rejected.\n")
        sys.stderr.write("Run 'claudio telegram setup' to configure it.\n")

    # Start memory daemon (pre-loads ONNX model to eliminate cold-start)
    global _memory_proc
    _memory_proc = _start_memory_daemon()

    # Start cloudflared tunnel (managed by Python for proper cleanup)
    cloudflared_proc = _start_cloudflared()

    # Bind to localhost only - cloudflared handles external access
    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claudio server listening on port {PORT}")

    # Graceful shutdown: SIGTERM → stop HTTP server → wait for active handlers
    shutdown_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda s, f: shutdown_event.set())
    shutdown_thread = threading.Thread(
        target=_graceful_shutdown,
        args=(server, shutdown_event),
        daemon=False,
    )
    shutdown_thread.start()

    # Run health check in background thread
    health_thread = threading.Thread(target=startup_health_check, daemon=True)
    health_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown_event.set()  # Trigger graceful shutdown on Ctrl+C too
    # Wait for graceful shutdown to finish draining handlers
    shutdown_thread.join()
    server.server_close()
    _stop_cloudflared(cloudflared_proc)
    _stop_memory_daemon(_memory_proc)


if __name__ == "__main__":
    main()
