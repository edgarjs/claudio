"""Telegram Bot API client — stdlib-only port of telegram.sh.

Provides TelegramClient with retry logic, message chunking, file downloads,
and all the API methods needed by the webhook handler. No side effects on import.
"""

import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from lib.util import (
    MultipartEncoder,
    log,
    log_error,
    validate_image_magic,
    validate_ogg_magic,
)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# Telegram's maximum message length
_MAX_MESSAGE_LEN = 4096

# Maximum file download size (20 MB — Telegram Bot API limit)
_MAX_FILE_SIZE = 20 * 1024 * 1024

# Only safe characters allowed in file_path from getFile response
_FILE_PATH_RE = re.compile(r'^[a-zA-Z0-9/_.\-]+$')


class TelegramClient:
    """Client for the Telegram Bot API.

    Mirrors the functions in telegram.sh: retry logic, message sending with
    fallback, file downloads with validation, and fire-and-forget helpers.
    """

    def __init__(self, token, bot_id=None):
        """Initialize with a bot token and optional bot_id for log context."""
        self._token = token
        self._bot_id = bot_id

    # -- Core API method with retry logic --

    def api_call(self, method, data=None, files=None, timeout=30):
        """Call a Telegram Bot API method with retry on 429 and 5xx.

        Args:
            method: API method name (e.g. "sendMessage").
            data: Dict of form fields (URL-encoded POST body).
            files: Dict of {field_name: file_path} for multipart uploads.
                   Can also contain regular string values that will be sent
                   as form fields alongside the file uploads.
            timeout: Request timeout in seconds.

        Returns:
            Parsed JSON response dict. On total failure returns {"ok": False}.
        """
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/{method}"
        max_retries = 4
        last_body = None

        for attempt in range(max_retries + 1):
            try:
                req = self._build_request(url, data, files)
                resp = urllib.request.urlopen(req, timeout=timeout)
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    return {"ok": True, "raw": body}

            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                last_body = err_body

                # 4xx (except 429) — client error, don't retry
                if 400 <= status < 500 and status != 429:
                    try:
                        return json.loads(err_body)
                    except (json.JSONDecodeError, ValueError):
                        return {"ok": False, "error_code": status, "description": err_body}

                # Retryable: 429 or 5xx
                if attempt < max_retries:
                    delay = self._retry_delay(status, err_body, attempt)
                    log("telegram", f"API error (HTTP {status}), retrying in {delay}s...",
                        bot_id=self._bot_id)
                    time.sleep(delay)
                # else fall through to next iteration / exhaustion

            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_body = str(exc)
                if attempt < max_retries:
                    delay = 2 ** attempt
                    log("telegram",
                        f"API network error ({exc}), retrying in {delay}s...",
                        bot_id=self._bot_id)
                    time.sleep(delay)

        # All retries exhausted
        log_error("telegram",
                  f"API failed after {max_retries + 1} attempts",
                  bot_id=self._bot_id)
        try:
            return json.loads(last_body) if last_body else {"ok": False}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"ok": False}

    # -- Message sending --

    def send_message(self, chat_id, text, reply_to=None):
        """Send a text message with 4096-char chunking and fallback logic.

        Attempts with Markdown parse_mode first. On failure, retries without
        parse_mode. If that also fails and reply_to was set, retries without
        reply_to. Matches telegram_send_message() in telegram.sh.
        """
        is_first = True

        while text:
            chunk = text[:_MAX_MESSAGE_LEN]
            text = text[_MAX_MESSAGE_LEN:]

            should_reply = is_first and reply_to is not None
            is_first = False

            # Attempt 1: with Markdown parse_mode
            params = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
            if should_reply:
                params["reply_to_message_id"] = reply_to
            result = self.api_call("sendMessage", data=params)

            if result.get("ok") is not True:
                # Attempt 2: without parse_mode (keep reply_to)
                params = {"chat_id": chat_id, "text": chunk}
                if should_reply:
                    params["reply_to_message_id"] = reply_to
                result = self.api_call("sendMessage", data=params)

                if result.get("ok") is not True:
                    # Attempt 3: without reply_to
                    params = {"chat_id": chat_id, "text": chunk}
                    result = self.api_call("sendMessage", data=params)

                    if result.get("ok") is not True:
                        log_error(
                            "telegram",
                            f"Failed to send message after all fallbacks for chat {chat_id}",
                            bot_id=self._bot_id,
                        )

    # -- Voice sending --

    def send_voice(self, chat_id, audio_path, reply_to=None):
        """Send a voice message via multipart upload.

        Returns True on success, False on failure.
        """
        files = {"voice": audio_path, "chat_id": str(chat_id)}
        if reply_to is not None:
            files["reply_to_message_id"] = str(reply_to)

        result = self.api_call("sendVoice", files=files)
        if result.get("ok") is not True:
            error_desc = result.get("description", "unknown error")[:200]
            log_error("telegram", f"sendVoice failed: {error_desc}", bot_id=self._bot_id)
            return False
        return True

    # -- Typing indicator --

    def send_typing(self, chat_id, action="typing"):
        """Send a chat action indicator. Fire-and-forget — never raises."""
        try:
            self.api_call(
                "sendChatAction",
                data={"chat_id": chat_id, "action": action},
                timeout=10,
            )
        except Exception:
            pass

    # -- Reaction --

    def set_reaction(self, chat_id, message_id, emoji="\U0001f440"):
        """Set a reaction on a message. Fire-and-forget — never raises."""
        try:
            payload = json.dumps({
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            }).encode("utf-8")
            url = f"{_TELEGRAM_API_BASE}/bot{self._token}/setMessageReaction"
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    # -- File downloads --

    def download_file(self, file_id, output_path, validate_fn=None):
        """Download a file by file_id via getFile, with size and magic byte validation.

        Args:
            file_id: Telegram file_id string.
            output_path: Local path to write the downloaded file.
            validate_fn: Optional callable(path) -> bool for magic byte validation.
                         File is deleted on validation failure.

        Returns:
            True on success, False on failure.

        Note: urllib does not expose a max-redirects setting. The Telegram file
        API should not redirect, but this is documented as an assumption matching
        the ``--max-redirs 0`` in the Bash implementation.
        """
        # Step 1: resolve file_id to file_path
        result = self.api_call("getFile", data={"file_id": file_id})
        file_path = (result.get("result") or {}).get("file_path")

        if not file_path:
            log_error("telegram",
                      f"Failed to get file path for file_id: {file_id}",
                      bot_id=self._bot_id)
            return False

        # Validate file_path: only safe characters, no traversal
        if not _FILE_PATH_RE.match(file_path) or ".." in file_path:
            log_error("telegram",
                      "Invalid characters in file path from API",
                      bot_id=self._bot_id)
            return False

        # Step 2: download the file
        download_url = f"{_TELEGRAM_API_BASE}/file/bot{self._token}/{file_path}"
        try:
            req = urllib.request.Request(download_url)
            resp = urllib.request.urlopen(req, timeout=60)
            file_data = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log_error("telegram",
                      f"Failed to download file: {file_path} ({exc})",
                      bot_id=self._bot_id)
            return False

        # Validate file size
        file_size = len(file_data)
        if file_size > _MAX_FILE_SIZE:
            log_error("telegram",
                      f"Downloaded file exceeds size limit: {file_size} bytes",
                      bot_id=self._bot_id)
            return False

        if file_size == 0:
            log_error("telegram", "Downloaded file is empty", bot_id=self._bot_id)
            return False

        # Write with restrictive permissions (chmod 600)
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as f:
            f.write(file_data)

        # Magic byte validation
        if validate_fn is not None:
            if not validate_fn(output_path):
                log_error("telegram",
                          "Downloaded file failed magic byte validation",
                          bot_id=self._bot_id)
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
                return False

        log("telegram",
            f"Downloaded file to: {output_path} ({file_size} bytes)",
            bot_id=self._bot_id)
        return True

    # -- Convenience download methods --

    def download_image(self, file_id, output_path):
        """Download an image file with magic byte validation."""
        return self.download_file(file_id, output_path, validate_fn=validate_image_magic)

    def download_voice(self, file_id, output_path):
        """Download a voice file with OGG magic byte validation."""
        return self.download_file(file_id, output_path, validate_fn=validate_ogg_magic)

    def download_document(self, file_id, output_path):
        """Download a document file with no magic byte validation."""
        return self.download_file(file_id, output_path)

    # -- Internal helpers --

    def _build_request(self, url, data=None, files=None):
        """Build a urllib.request.Request for the given URL and parameters.

        If files is provided, builds a multipart/form-data request.
        File entries are detected by checking if the value is a path to an
        existing file; all other entries are sent as plain form fields.

        If only data is provided, builds a URL-encoded POST body.
        """
        if files is not None:
            enc = MultipartEncoder()
            for key, value in files.items():
                # If the value is a path to an existing file, send as file upload
                if os.path.isfile(value):
                    enc.add_file(key, value)
                else:
                    enc.add_field(key, value)
            body = enc.finish()
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", enc.content_type)
            return req

        if data is not None:
            encoded = urllib.parse.urlencode(data).encode("utf-8")
            req = urllib.request.Request(url, data=encoded, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            return req

        # GET request (no body)
        return urllib.request.Request(url)

    @staticmethod
    def _retry_delay(status, body, attempt):
        """Compute the delay before the next retry attempt.

        On 429, uses retry_after from the response body if available.
        Otherwise falls back to exponential backoff (2^attempt).
        """
        if status == 429:
            try:
                parsed = json.loads(body)
                retry_after = parsed.get("parameters", {}).get("retry_after")
                if retry_after is not None and int(retry_after) >= 1:
                    return int(retry_after)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return 2 ** attempt
