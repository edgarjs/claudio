#!/usr/bin/env python3

import base64
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
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

MAX_BODY_SIZE = 1024 * 1024  # 1 MB
MAX_QUEUE_SIZE = 5  # Max queued messages per chat
WEBHOOK_TIMEOUT = 600  # 10 minutes max per Claude invocation
HEALTH_CACHE_TTL = 30  # seconds between health check API calls
QUEUE_WARNING_RATIO = 0.8  # Warn when queue reaches this fraction of max
MEDIA_GROUP_WAIT = 1.5  # seconds to wait for all photos in a media group
MAX_MEDIA_GROUPS = 10  # Max concurrent media groups being buffered
MAX_PHOTOS_PER_GROUP = 10  # Max photos allowed in a single media group


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDIO_BIN = os.path.join(SCRIPT_DIR, "..", "claudio")
CLAUDIO_PATH = os.path.join(os.path.expanduser("~"), ".claudio")
LOG_FILE = os.path.join(CLAUDIO_PATH, "claudio.log")
MEMORY_SOCKET = os.path.join(CLAUDIO_PATH, "memory.sock")
MEMORY_DAEMON_LOG = os.path.join(CLAUDIO_PATH, "memory-daemon.log")
PORT = int(os.environ.get("PORT", 8421))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
ALEXA_SKILL_ID = os.environ.get("ALEXA_SKILL_ID", "")

# Multi-bot registry: loaded from ~/.claudio/bots/*/bot.env
# bots: dict of bot_id -> {"token": str, "chat_id": str, "secret": str, ...}
# bots_by_secret: list of (secret, bot_id) for dispatch
bots = {}
bots_by_secret = []
bots_lock = threading.Lock()

# Per-chat message queues for serial processing
chat_queues = {}  # queue_key -> deque of (webhook_body, bot_id)
chat_active = {}  # queue_key -> bool, True if a processor thread is running
active_threads = []  # Non-daemon processor threads to wait on during shutdown
queue_lock = threading.Lock()
seen_updates = OrderedDict()  # Track processed update_ids to prevent duplicates
MAX_SEEN_UPDATES = 1000
shutting_down = False  # Set to True on SIGTERM to reject new webhooks

# Media group buffering: group_key -> {"bodies": [str], "chat_id": str, "bot_id": str, "timer": Timer}
media_groups = {}
media_group_lock = threading.Lock()

# Health check cache
_health_cache = {"result": None, "time": 0}


def parse_env_file(path):
    """Parse a KEY="value" or KEY=value env file. Mirrors _safe_load_env in bash."""
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                eq = line.find("=")
                if eq < 1:
                    continue
                key = line[:eq]
                val = line[eq + 1:]
                # Strip surrounding double quotes
                if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                    # Reverse _env_quote escaping
                    val = val.replace("\\n", "\n")
                    val = val.replace("\\`", "`")
                    val = val.replace("\\$", "$")
                    val = val.replace('\\"', '"')
                    val = val.replace("\\\\", "\\")
                result[key] = val
    except (OSError, IOError):
        pass
    return result


def is_valid_bot_id(bot_id):
    """Validate bot_id contains only safe characters (alphanumeric, underscore, hyphen)."""
    import re
    return bool(re.match(r'^[a-zA-Z0-9_-]+$', bot_id))


def load_bots():
    """Scan ~/.claudio/bots/*/bot.env and build bot registry."""
    global bots, bots_by_secret
    bots_dir = os.path.join(CLAUDIO_PATH, "bots")
    new_bots = {}
    new_by_secret = []

    if not os.path.isdir(bots_dir):
        with bots_lock:
            bots = new_bots
            bots_by_secret = new_by_secret
        return

    bots_dir_real = os.path.realpath(bots_dir)
    for entry in sorted(os.listdir(bots_dir)):
        # Security: Validate bot_id format to prevent command injection
        if not is_valid_bot_id(entry):
            sys.stderr.write(f"[bots] Invalid bot_id '{entry}', skipping (must match [a-zA-Z0-9_-]+)\n")
            continue

        # Security: Validate entry doesn't contain path traversal sequences
        if '..' in entry or '/' in entry or entry.startswith('.'):
            sys.stderr.write(f"[bots] Skipping invalid bot directory name: {entry}\n")
            continue

        bot_env = os.path.join(bots_dir, entry, "bot.env")

        # Security: Verify the path is actually under bots_dir (defense against symlink attacks)
        bot_env_real = os.path.realpath(bot_env)
        if not bot_env_real.startswith(bots_dir_real + os.sep):
            sys.stderr.write(f"[bots] Path traversal detected in bot '{entry}', skipping\n")
            continue

        if not os.path.isfile(bot_env):
            continue
        cfg = parse_env_file(bot_env)
        token = cfg.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = cfg.get("TELEGRAM_CHAT_ID", "")
        secret = cfg.get("WEBHOOK_SECRET", "")
        if not token or not secret:
            sys.stderr.write(f"[bots] Skipping bot '{entry}': missing token or secret\n")
            continue
        new_bots[entry] = {
            "token": token,
            "chat_id": chat_id,
            "secret": secret,
            "model": cfg.get("MODEL", "haiku"),
            "max_history_lines": cfg.get("MAX_HISTORY_LINES", "100"),
            "bot_dir": os.path.join(bots_dir, entry),
        }
        new_by_secret.append((secret, entry))

    with bots_lock:
        bots = new_bots
        bots_by_secret = new_by_secret

    sys.stderr.write(f"[bots] Loaded {len(new_bots)} bot(s): {', '.join(new_bots.keys())}\n")


def match_bot_by_secret(token_header):
    """Find bot matching the secret token header. Returns (bot_id, bot_config) or (None, None)."""
    if not token_header:
        return None, None
    with bots_lock:
        for secret, bot_id in bots_by_secret:
            if hmac.compare_digest(token_header, secret):
                return bot_id, bots[bot_id]
    return None, None


def parse_webhook(body):
    """Extract update_id, chat_id, and media_group_id from webhook body."""
    try:
        data = json.loads(body)
        update_id = data.get("update_id")
        msg = data.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        media_group_id = msg.get("media_group_id", "")
        return update_id, chat_id, media_group_id
    except (json.JSONDecodeError, AttributeError):
        return None, "", ""


def process_queue(queue_key):
    """Process messages for a chat one at a time."""
    current = threading.current_thread()
    try:
        _process_queue_loop(queue_key)
    finally:
        with queue_lock:
            if current in active_threads:
                active_threads.remove(current)


def _process_queue_loop(queue_key):
    """Inner loop for process_queue, separated for clean thread tracking."""
    while True:
        with queue_lock:
            if queue_key not in chat_queues or not chat_queues[queue_key]:
                # Queue empty, clean up
                if queue_key in chat_queues:
                    del chat_queues[queue_key]
                chat_active.pop(queue_key, None)
                return
            body, bot_id = chat_queues[queue_key].popleft()

        proc = None
        try:
            with open(LOG_FILE, "a") as log_fh:
                # Ensure PATH includes ~/.local/bin for claude command
                env = os.environ.copy()
                home = os.path.expanduser("~")
                local_bin = os.path.join(home, ".local", "bin")
                if local_bin not in env.get("PATH", "").split(os.pathsep):
                    env["PATH"] = f"{local_bin}{os.pathsep}{env.get('PATH', '')}"

                # Pass bot_id so the webhook handler loads the right config
                env["CLAUDIO_BOT_ID"] = bot_id

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
                        f"for {queue_key}\n"
                    )
        except subprocess.TimeoutExpired:
            sys.stderr.write(
                f"[queue] Webhook handler timed out after {WEBHOOK_TIMEOUT}s "
                f"for {queue_key}, killing process\n"
            )
            if proc:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception as e:
            sys.stderr.write(f"[queue] Error processing message for {queue_key}: {e}\n")
            time.sleep(1)  # Avoid tight loop on persistent errors


def _merge_media_group(group_key):
    """Merge buffered media group messages into a single webhook body and enqueue it.

    Called by the Timer after MEDIA_GROUP_WAIT seconds of inactivity. Takes the
    first message as the base webhook and injects an '_extra_photos' field with
    file_ids from subsequent messages. telegram.sh reads this field to download
    and pass all images to Claude in a single invocation.

    On merge failure, falls back to enqueueing only the first photo (others are
    lost) to avoid blocking the queue entirely.
    """
    with media_group_lock:
        group = media_groups.pop(group_key, None)
    if not group or not group["bodies"]:
        return

    bodies = group["bodies"]
    bot_id = group["bot_id"]
    if len(bodies) == 1:
        # Single photo, enqueue as-is
        _enqueue_single(bodies[0], group["chat_id"], bot_id)
        return

    # Merge: use the first message as base, collect all photo file_ids
    try:
        base = json.loads(bodies[0])
        extra_photos = []
        for b in bodies[1:]:
            data = json.loads(b)
            msg = data.get("message", {})
            photo = msg.get("photo", [])
            if photo:
                extra_photos.append(photo[-1]["file_id"])
            elif msg.get("document", {}).get("mime_type", "").startswith("image/"):
                extra_photos.append(msg["document"]["file_id"])

        # Inject extra photo file_ids into the base message as a custom field
        if extra_photos:
            base["message"]["_extra_photos"] = extra_photos
            sys.stderr.write(
                f"[media-group] Merged {len(bodies)} photos into one webhook "
                f"(group {group_key})\n"
            )

        _enqueue_single(json.dumps(base), group["chat_id"], bot_id)
    except (json.JSONDecodeError, KeyError) as e:
        sys.stderr.write(f"[media-group] Error merging group {group_key}: {e}\n")
        # Fallback: enqueue just the first message
        _enqueue_single(bodies[0], group["chat_id"], bot_id)


def enqueue_webhook(body, bot_id, bot_config):
    """Add webhook to per-chat queue and start processor if needed.

    Media group messages (multiple photos sent together) are buffered briefly
    and merged into a single webhook before processing.
    """
    update_id, chat_id, media_group_id = parse_webhook(body)
    if not chat_id:
        return  # Invalid webhook, skip

    # Validate chat_id against authorized chat (defense in depth, also checked in bash)
    bot_chat_id = bot_config.get("chat_id", "")
    if not bot_chat_id or chat_id != bot_chat_id:
        return

    # Composite queue key for per-bot, per-chat isolation
    queue_key = f"{bot_id}:{chat_id}"

    with queue_lock:
        # Reject new messages during shutdown — let active handlers finish
        if shutting_down:
            sys.stderr.write(f"[queue] Rejecting webhook during shutdown for {queue_key}\n")
            return

        # Deduplicate: skip if we've already seen this update_id
        if update_id is not None:
            if update_id in seen_updates:
                return  # Duplicate webhook, skip
            seen_updates[update_id] = True
            while len(seen_updates) > MAX_SEEN_UPDATES:
                seen_updates.popitem(last=False)

    # Buffer media group messages, then merge after a short delay
    if media_group_id:
        group_key = f"{bot_id}:{media_group_id}"
        with media_group_lock:
            if group_key in media_groups:
                if len(media_groups[group_key]["bodies"]) >= MAX_PHOTOS_PER_GROUP:
                    sys.stderr.write(
                        f"[media-group] Dropping photo — group {group_key} "
                        f"reached {MAX_PHOTOS_PER_GROUP} photo limit\n"
                    )
                    return
                media_groups[group_key]["bodies"].append(body)
                # Reset timer — extend window for late-arriving photos
                media_groups[group_key]["timer"].cancel()
            else:
                if len(media_groups) >= MAX_MEDIA_GROUPS:
                    sys.stderr.write(
                        f"[media-group] Dropping group {group_key} — "
                        f"reached {MAX_MEDIA_GROUPS} concurrent group limit\n"
                    )
                    return
                media_groups[group_key] = {
                    "bodies": [body],
                    "chat_id": chat_id,
                    "bot_id": bot_id,
                    "timer": None,
                }
            timer = threading.Timer(MEDIA_GROUP_WAIT, _merge_media_group, [group_key])
            media_groups[group_key]["timer"] = timer
            timer.start()
        return

    _enqueue_single(body, chat_id, bot_id)


def _enqueue_single(body, chat_id, bot_id):
    """Enqueue a single (possibly merged) webhook body for processing."""
    queue_key = f"{bot_id}:{chat_id}"
    with queue_lock:
        # Initialize queue for this chat if needed
        if queue_key not in chat_queues:
            chat_queues[queue_key] = deque()

        # Prevent unbounded queue growth
        queue_size = len(chat_queues[queue_key])
        if queue_size >= MAX_QUEUE_SIZE:
            sys.stderr.write(f"[queue] Queue full for {queue_key} ({queue_size}/{MAX_QUEUE_SIZE}), dropping message\n")
            return
        if queue_size >= MAX_QUEUE_SIZE * QUEUE_WARNING_RATIO:
            sys.stderr.write(f"[queue] Warning: queue for {queue_key} at {queue_size}/{MAX_QUEUE_SIZE} ({queue_size * 100 // MAX_QUEUE_SIZE}%)\n")

        chat_queues[queue_key].append((body, bot_id))

        if not chat_active.get(queue_key):
            chat_active[queue_key] = True
            thread = threading.Thread(
                target=process_queue,
                args=(queue_key,),
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

    def _read_body(self):
        """Read and return the request body, or None on error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._respond(400, {"error": "invalid content-length"})
            return None
        if length > MAX_BODY_SIZE:
            self._respond(413, {"error": "payload too large"})
            return None
        return self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

    def do_POST(self):
        if self.path == "/telegram/webhook":
            # Reject early during shutdown so Telegram retries later
            if shutting_down:
                self._respond(503, {"error": "shutting down"})
                return
            # Match bot by secret token
            token_header = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            bot_id, bot_config = match_bot_by_secret(token_header)
            if bot_id is None:
                self._respond(401, {"error": "unauthorized"})
                return
            body = self._read_body()
            if body is None:
                return
            self._respond(200, {"ok": True})
            enqueue_webhook(body, bot_id, bot_config)
        elif self.path == "/alexa":
            self._handle_alexa()
        else:
            self._respond(404, {"error": "not found"})

    def _handle_alexa(self):
        """Handle Alexa skill requests — async relay to Telegram."""
        if shutting_down:
            self._respond_alexa(_alexa_str("en", "shutting_down"), end_session=True)
            return

        body = self._read_body()
        if body is None:
            return

        # Validate the request comes from Alexa
        if not _verify_alexa_request(self.headers, body):
            self._respond(401, {"error": "invalid alexa request"})
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return

        # Validate skill ID if configured
        app_id = data.get("session", {}).get("application", {}).get("applicationId", "")
        if ALEXA_SKILL_ID and app_id != ALEXA_SKILL_ID:
            self._respond(401, {"error": "skill id mismatch"})
            return

        locale = data.get("request", {}).get("locale", "en-US")
        req_type = data.get("request", {}).get("type", "")
        intent_name = data.get("request", {}).get("intent", {}).get("name", "")
        sys.stderr.write("[alexa] req_type=%s intent=%s locale=%s session_new=%s\n" %
                         (req_type, intent_name or "-", locale,
                          data.get("session", {}).get("new")))

        if req_type == "LaunchRequest":
            self._respond_alexa(
                _alexa_str(locale, "launch"),
                end_session=False,
                reprompt=_alexa_str(locale, "reprompt"),
            )
            return

        session_id = data.get("session", {}).get("sessionId", "")

        if req_type == "SessionEndedRequest":
            # Flush buffered messages before closing
            _flush_alexa_session(session_id, locale)
            self._respond_alexa("", end_session=True)
            return

        if req_type == "IntentRequest":
            intent = data.get("request", {}).get("intent", {})
            intent_name = intent.get("name", "")

            # Built-in intents
            if intent_name in ("AMAZON.CancelIntent", "AMAZON.StopIntent", "AMAZON.NoIntent"):
                has_messages = _alexa_session_has_messages(session_id)
                _flush_alexa_session(session_id, locale)
                goodbye_key = "goodbye" if has_messages else "goodbye_empty"
                self._respond_alexa(_alexa_str(locale, goodbye_key), end_session=True)
                return
            if intent_name == "AMAZON.HelpIntent":
                self._respond_alexa(_alexa_str(locale, "help"), end_session=False)
                return
            if intent_name == "AMAZON.FallbackIntent":
                self._respond_alexa(_alexa_str(locale, "fallback"), end_session=False)
                return

            # Our custom intent: buffer message locally
            if intent_name == "SendMessageIntent":
                message = intent.get("slots", {}).get("message", {}).get("value", "")
                if not message:
                    self._respond_alexa(_alexa_str(locale, "no_message"), end_session=False)
                    return

                _buffer_alexa_message(session_id, message, locale)
                self._respond_alexa(
                    _alexa_str(locale, "buffered"),
                    end_session=False,
                    reprompt=_alexa_str(locale, "reprompt"),
                )
                return

        # Unknown request type
        sys.stderr.write("[alexa] unhandled: req_type=%s intent=%s\n" % (req_type, intent_name))
        self._respond_alexa(_alexa_str(locale, "unknown"), end_session=True)

    def _respond_alexa(self, text, end_session=True, reprompt=None):
        """Send an Alexa-formatted JSON response."""
        response = {
            "version": "1.0",
            "response": {
                "shouldEndSession": end_session,
            },
        }
        if text:
            response["response"]["outputSpeech"] = {
                "type": "PlainText",
                "text": text,
            }
        if reprompt:
            response["response"]["reprompt"] = {
                "outputSpeech": {
                    "type": "PlainText",
                    "text": reprompt,
                }
            }
        body = json.dumps(response).encode("utf-8")
        sys.stderr.write("[alexa] response: end_session=%s text_len=%d\n" % (end_session, len(text)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            health = check_health()
            code = 200 if health["status"] == "healthy" else 503
            self._respond(code, health)
        elif self.path == "/reload":
            load_bots()
            self._respond(200, {"ok": True, "bots": list(bots.keys())})
        else:
            self._respond(404, {"error": "not found"})


def _get_default_bot():
    """Get the first bot config for Alexa routing (or None)."""
    with bots_lock:
        if not bots:
            return None, None
        bot_id = next(iter(bots))
        return bot_id, bots[bot_id]


_alexa_update_counter = 0
_alexa_counter_lock = threading.Lock()

# Alexa session buffers: session_id -> {"messages": [...], "locale": str, "last_activity": float}
_alexa_sessions = {}
_alexa_sessions_lock = threading.Lock()
_ALEXA_SESSION_TTL = 300  # 5 min — cleanup stale sessions

# Alexa response strings by locale (2-letter language code)
_ALEXA_STRINGS = {
    "es": {
        "shutting_down": "Lo siento, estoy reiniciando. Intenta en un momento.",
        "launch": "Dime qué le quieres decir a Claudio.",
        "goodbye": "Listo, le paso todo a Claudio. Adiós.",
        "goodbye_empty": "Adiós.",
        "help": "Puedes decirme varios mensajes y al final se los paso todos juntos a Claudio por Telegram. Di 'eso es todo' cuando termines.",
        "fallback": "No entendí. Intenta decir: dile a Claudio, seguido de tu mensaje.",
        "no_message": "No escuché el mensaje. Intenta de nuevo.",
        "buffered": "Anotado. ¿Algo más?",
        "reprompt": "¿Algo más para Claudio?",
        "unknown": "No entendí la solicitud.",
    },
    "en": {
        "shutting_down": "Sorry, I'm restarting. Try again in a moment.",
        "launch": "Tell me what you want to say to Claudio.",
        "goodbye": "Got it, sending everything to Claudio. Goodbye.",
        "goodbye_empty": "Goodbye.",
        "help": "You can send multiple messages and I'll relay them all to Claudio at the end. Say 'that's all' when you're done.",
        "fallback": "I didn't catch that. Try saying: tell Claudio, followed by your message.",
        "no_message": "I didn't hear the message. Try again.",
        "buffered": "Noted. Anything else?",
        "reprompt": "Anything else for Claudio?",
        "unknown": "I didn't understand the request.",
    },
}


def _alexa_str(locale, key):
    """Get a localized Alexa response string. Falls back to English."""
    lang = (locale or "en")[:2].lower()
    strings = _ALEXA_STRINGS.get(lang, _ALEXA_STRINGS["en"])
    return strings[key]


def _buffer_alexa_message(session_id, message, locale):
    """Add a message to the Alexa session buffer."""
    with _alexa_sessions_lock:
        if session_id not in _alexa_sessions:
            _alexa_sessions[session_id] = {
                "messages": [],
                "locale": locale,
                "last_activity": time.monotonic(),
            }
        _alexa_sessions[session_id]["messages"].append(message)
        _alexa_sessions[session_id]["last_activity"] = time.monotonic()
        count = len(_alexa_sessions[session_id]["messages"])
    sys.stderr.write(f"[alexa] Buffered message #{count} for session {session_id[:16]}...\n")

    # Cleanup stale sessions while we're here
    _cleanup_stale_alexa_sessions()


def _alexa_session_has_messages(session_id):
    """Check if a session has buffered messages."""
    with _alexa_sessions_lock:
        session = _alexa_sessions.get(session_id)
        return bool(session and session["messages"])


def _flush_alexa_session(session_id, locale):
    """Flush all buffered messages for a session as a single webhook."""
    with _alexa_sessions_lock:
        session = _alexa_sessions.pop(session_id, None)

    if not session or not session["messages"]:
        sys.stderr.write(f"[alexa] No messages to flush for session {session_id[:16]}...\n")
        return

    messages = session["messages"]
    sys.stderr.write(f"[alexa] Flushing {len(messages)} message(s) for session {session_id[:16]}...\n")

    # Build transcript — no "Alexa" label to avoid Claude filtering out requests
    if len(messages) == 1:
        transcript = messages[0]
    else:
        lines = []
        for msg in messages:
            lines.append(f'- "{msg}"')
        transcript = "\n".join(lines)

    # Route to default (first) bot
    bot_id, bot_config = _get_default_bot()
    if bot_config is None:
        sys.stderr.write("[alexa] No bots configured, cannot relay\n")
        return

    bot_chat_id = bot_config.get("chat_id", "")
    if not bot_chat_id:
        sys.stderr.write("[alexa] Default bot has no TELEGRAM_CHAT_ID, cannot relay\n")
        return

    global _alexa_update_counter
    with _alexa_counter_lock:
        _alexa_update_counter += 1
        update_id = 900000000 + _alexa_update_counter

    body = json.dumps({
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": int(bot_chat_id), "type": "private"},
            "from": {"id": int(bot_chat_id), "first_name": "Alexa", "is_bot": False},
            "text": transcript,
        },
    })
    enqueue_webhook(body, bot_id, bot_config)


def _cleanup_stale_alexa_sessions():
    """Remove sessions older than TTL to prevent memory leaks."""
    now = time.monotonic()
    with _alexa_sessions_lock:
        stale = [sid for sid, s in _alexa_sessions.items()
                 if now - s["last_activity"] > _ALEXA_SESSION_TTL]
        for sid in stale:
            session = _alexa_sessions.pop(sid)
            count = len(session["messages"])
            if count:
                sys.stderr.write(f"[alexa] Stale session {sid[:16]}... expired with {count} unflushed message(s)\n")


def _verify_alexa_request(headers, body):
    """Verify that the request comes from Alexa by validating the certificate chain.

    Amazon requires signature verification for production skills. For dev/testing
    mode, we do basic validation of the signature headers and timestamp.
    Full certificate chain validation requires the cryptography library.
    """
    # Check required headers exist
    # Alexa sends: SignatureCertChainUrl and Signature-256 (or Signature)
    # HTTP proxies may lowercase header names, so check case-insensitively
    cert_url = headers.get("SignatureCertChainUrl", "")
    signature = headers.get("Signature-256", "")

    if not cert_url or not signature:
        sys.stderr.write("[alexa] Missing signature headers: SignatureCertChainUrl=%s Signature-256=%s\n" %
                         ("present" if cert_url else "missing", "present" if signature else "missing"))
        return False

    # Validate cert URL (must be Amazon's domain, HTTPS, port 443, path starts with /echo.api/)
    try:
        parsed = urllib.parse.urlparse(cert_url)
        if parsed.scheme.lower() != "https":
            sys.stderr.write(f"[alexa] Cert URL scheme not HTTPS: {cert_url}\n")
            return False
        if parsed.hostname.lower() != "s3.amazonaws.com":
            sys.stderr.write(f"[alexa] Cert URL hostname invalid: {parsed.hostname}\n")
            return False
        if not parsed.path.startswith("/echo.api/"):
            sys.stderr.write(f"[alexa] Cert URL path invalid: {parsed.path}\n")
            return False
        if parsed.port is not None and parsed.port != 443:
            sys.stderr.write(f"[alexa] Cert URL port invalid: {parsed.port}\n")
            return False
    except Exception as e:
        sys.stderr.write(f"[alexa] Cert URL parse error: {e}\n")
        return False

    # Validate timestamp (within 150 seconds)
    try:
        data = json.loads(body)
        timestamp = data.get("request", {}).get("timestamp", "")
        if timestamp:
            req_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = abs((now - req_time).total_seconds())
            if delta > 150:
                sys.stderr.write(f"[alexa] Timestamp too old: {delta}s\n")
                return False
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(f"[alexa] Timestamp validation error: {e}\n")
        return False

    # Full certificate signature verification
    try:
        return _verify_alexa_signature(cert_url, signature, body)
    except ImportError:
        # cryptography library not installed — fail securely
        sys.stderr.write("[alexa] cryptography library not available, rejecting request\n")
        return False
    except Exception as e:
        sys.stderr.write(f"[alexa] Signature verification error: {e}\n")
        return False


# Cache for downloaded Alexa signing certificates
_alexa_cert_cache = {}
_ALEXA_CERT_CACHE_TTL = 3600  # 1 hour


def _verify_alexa_signature(cert_url, signature_b64, body):
    """Full cryptographic verification of Alexa request signature."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    # Get or fetch the signing certificate
    now = time.monotonic()
    cached = _alexa_cert_cache.get(cert_url)
    if cached and (now - cached["time"]) < _ALEXA_CERT_CACHE_TTL:
        cert = cached["cert"]
    else:
        req = urllib.request.Request(cert_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            pem_data = resp.read()
        cert = x509.load_pem_x509_certificate(pem_data)

        # Validate certificate is currently valid
        now_utc = datetime.now(timezone.utc)
        not_before = getattr(cert, "not_valid_before_utc", cert.not_valid_before.replace(tzinfo=timezone.utc))
        not_after = getattr(cert, "not_valid_after_utc", cert.not_valid_after.replace(tzinfo=timezone.utc))
        if now_utc < not_before or now_utc > not_after:
            sys.stderr.write("[alexa] Certificate is expired or not yet valid\n")
            return False

        # Validate the certificate's Subject Alternative Name includes echo-api.amazon.com
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            dns_names = san.value.get_values_for_type(x509.DNSName)
            if "echo-api.amazon.com" not in dns_names:
                sys.stderr.write(f"[alexa] Certificate SAN missing echo-api.amazon.com: {dns_names}\n")
                return False
        except x509.ExtensionNotFound:
            sys.stderr.write("[alexa] Certificate missing SAN extension\n")
            return False

        _alexa_cert_cache[cert_url] = {"cert": cert, "time": now}

    # Verify the signature
    signature_bytes = base64.b64decode(signature_b64)
    public_key = cert.public_key()
    public_key.verify(
        signature_bytes,
        body.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    return True


def check_health():
    """Verify system health by checking Telegram webhook status for all bots."""
    now = time.monotonic()
    if _health_cache["result"] and 0 <= (now - _health_cache["time"]) < HEALTH_CACHE_TTL:
        return _health_cache["result"]

    checks = {}
    status = "healthy"

    # Check each bot's webhook
    with bots_lock:
        bot_items = list(bots.items())

    if not bot_items:
        checks["telegram_webhook"] = {"status": "not_configured"}
    else:
        for bot_id, bot_config in bot_items:
            check_key = f"telegram_webhook_{bot_id}" if len(bot_items) > 1 else "telegram_webhook"
            token = bot_config["token"]
            if token and WEBHOOK_URL:
                try:
                    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        data = json.loads(resp.read().decode())

                    if data.get("ok"):
                        result = data.get("result", {})
                        current_url = result.get("url", "")
                        expected_url = f"{WEBHOOK_URL}/telegram/webhook"
                        pending = result.get("pending_update_count", 0)
                        last_error = result.get("last_error_message", "")

                        if current_url == expected_url:
                            checks[check_key] = {
                                "status": "ok",
                                "pending_updates": pending,
                            }
                            if last_error:
                                checks[check_key]["last_error"] = last_error
                        else:
                            checks[check_key] = {
                                "status": "mismatch",
                                "expected": expected_url,
                                "actual": current_url,
                            }
                            status = "unhealthy"
                            _register_webhook(expected_url, bot_config)
                    else:
                        checks[check_key] = {"status": "error", "detail": "API returned not ok"}
                        status = "unhealthy"
                        expected_url = f"{WEBHOOK_URL}/telegram/webhook"
                        _register_webhook(expected_url, bot_config)
                except (urllib.error.URLError, TimeoutError) as e:
                    checks[check_key] = {"status": "error", "detail": str(e)}
                    status = "unhealthy"
                    expected_url = f"{WEBHOOK_URL}/telegram/webhook"
                    _register_webhook(expected_url, bot_config)
            else:
                checks[check_key] = {"status": "not_configured"}

    # Check: Memory daemon (non-critical — degrades gracefully)
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


def _register_webhook(webhook_url, bot_config):
    """Attempt to re-register the Telegram webhook for a specific bot."""
    token = bot_config["token"]
    secret = bot_config.get("secret", "")
    try:
        params = {"url": webhook_url, "allowed_updates": '["message"]'}
        if secret:
            params["secret_token"] = secret
        data = urllib.parse.urlencode(params)

        url = f"https://api.telegram.org/bot{token}/setWebhook"
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
    """Run health check after startup to ensure webhooks are registered for all bots."""
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

    # Flush pending media groups immediately (cancel timers, enqueue merged results)
    with media_group_lock:
        pending_ids = list(media_groups.keys())
        for gid in pending_ids:
            group = media_groups.get(gid)
            if group and group["timer"]:
                group["timer"].cancel()
    for gid in pending_ids:
        _merge_media_group(gid)

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
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "run",
             "--url", f"http://localhost:{PORT}", tunnel_name],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    except Exception as e:
        log_fh.close()
        sys.stderr.write(f"[cloudflared] Failed to start: {e}\n")
        return None
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
        proc.wait(timeout=30)
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
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    except Exception as e:
        log_fh.close()
        sys.stderr.write(f"[memory-daemon] Failed to start: {e}\n")
        return None

    # Wait for the daemon to be ready by checking for the socket file.
    deadline = time.monotonic() + 30
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if os.path.exists(MEMORY_SOCKET):
            ready = True
            break
        time.sleep(0.2)

    log_fh.close()

    if ready:
        sys.stderr.write(f"[memory-daemon] Started (pid {proc.pid}).\n")
        return proc
    else:
        sys.stderr.write("[memory-daemon] Failed to start within 30s, continuing without it.\n")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
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
        proc.wait(timeout=30)
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
            try:
                _memory_proc.wait(timeout=1)
            except Exception:
                pass
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


def _reload_bots_on_sighup(*args):
    """SIGHUP handler: reload bot configs without restarting."""
    sys.stderr.write("[bots] SIGHUP received, reloading bot configs...\n")
    _health_cache["result"] = None  # Invalidate health cache
    load_bots()


def main():
    # Load bot configs
    load_bots()

    with bots_lock:
        bot_count = len(bots)

    if bot_count == 0:
        sys.stderr.write("Warning: No bots configured — all webhooks will be rejected.\n")
        sys.stderr.write("Run 'claudio install <bot_name>' to configure a bot.\n")

    # Start memory daemon (pre-loads ONNX model to eliminate cold-start)
    global _memory_proc
    if os.environ.get("MEMORY_ENABLED", "1") == "1":
        _memory_proc = _start_memory_daemon()

    # Start cloudflared tunnel (managed by Python for proper cleanup)
    cloudflared_proc = _start_cloudflared()

    # Bind to localhost only - cloudflared handles external access
    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claudio server listening on port {PORT}")

    # Graceful shutdown: SIGTERM → stop HTTP server → wait for active handlers
    shutdown_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda s, f: shutdown_event.set())

    # SIGHUP: reload bot configs without restart
    signal.signal(signal.SIGHUP, _reload_bots_on_sighup)

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
