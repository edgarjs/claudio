"""WhatsApp Business API client.

Ports all WhatsApp Business API functions from lib/whatsapp.sh to Python.
Stdlib only — no external dependencies. Intended to be imported by a future
handlers.py orchestrator.
"""

import json
import os
import time
import urllib.error
import urllib.request

from lib.util import (
    MultipartEncoder,
    log,
    log_error,
    validate_audio_magic,
    validate_image_magic,
)

_BASE_API = "https://graph.facebook.com/v21.0"

# 16 MB — WhatsApp Cloud API media size limit
_MAX_MEDIA_SIZE = 16 * 1024 * 1024

# Message body limit per WhatsApp message
_MAX_MESSAGE_LEN = 4096


class WhatsAppClient:
    """Client for the WhatsApp Business Cloud API (v21.0).

    All HTTP calls use stdlib ``urllib.request``. Credentials are never
    exposed in process lists or log output.
    """

    def __init__(self, phone_number_id, access_token, bot_id=None):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.bot_id = bot_id
        self._base_url = f"{_BASE_API}/{phone_number_id}"

    # -- internal helpers --------------------------------------------------

    def _log(self, msg):
        log("whatsapp", msg, bot_id=self.bot_id)

    def _log_error(self, msg):
        log_error("whatsapp", msg, bot_id=self.bot_id)

    def _auth_header(self):
        return f"Bearer {self.access_token}"

    # -- core API call with retry ------------------------------------------

    def api_call(self, endpoint, data=None, files=None, method="POST", timeout=30):
        """Make an authenticated API call with retry logic.

        Mirrors ``whatsapp_api()`` in whatsapp.sh (lines 35-76).

        * ``data`` (dict) — sent as JSON with Content-Type: application/json.
        * ``files`` (MultipartEncoder) — sent as multipart/form-data.
        * Retries up to 4 times on 429 and 5xx with exponential backoff.
        * Returns parsed JSON dict on success, or ``{}`` after total failure.
        """
        url = f"{self._base_url}/{endpoint}"
        max_retries = 4

        for attempt in range(max_retries + 1):
            body_bytes = None
            headers = {"Authorization": self._auth_header()}

            if files is not None:
                body_bytes = files.finish()
                headers["Content-Type"] = files.content_type
            elif data is not None:
                body_bytes = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"

            req = urllib.request.Request(
                url, data=body_bytes, headers=headers, method=method
            )

            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                    try:
                        return json.loads(resp_body)
                    except (json.JSONDecodeError, ValueError):
                        return {}
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    resp_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    resp_body = ""

                # 4xx (except 429) — client error, don't retry
                if 400 <= status < 500 and status != 429:
                    try:
                        return json.loads(resp_body)
                    except (json.JSONDecodeError, ValueError):
                        return {}

                # 429 or 5xx — retryable
                if attempt < max_retries:
                    delay = 2 ** attempt
                    self._log(f"API error (HTTP {status}), retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    self._log_error(
                        f"API failed after {max_retries + 1} attempts (HTTP {status})"
                    )
                    try:
                        return json.loads(resp_body)
                    except (json.JSONDecodeError, ValueError):
                        return {}
            except Exception as exc:
                if attempt < max_retries:
                    delay = 2 ** attempt
                    self._log(f"API error ({exc}), retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    self._log_error(
                        f"API failed after {max_retries + 1} attempts ({exc})"
                    )
                    return {}

        # Should not be reached, but satisfy the type checker.
        return {}

    # -- send_message ------------------------------------------------------

    def send_message(self, to, text, reply_to=None):
        """Send a text message, chunking at 4096 characters.

        Mirrors ``whatsapp_send_message()`` in whatsapp.sh (lines 78-129).
        Only the first chunk carries the ``context.message_id`` reply marker.
        """
        is_first = True
        offset = 0

        while offset < len(text):
            chunk = text[offset : offset + _MAX_MESSAGE_LEN]
            offset += _MAX_MESSAGE_LEN

            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            }

            if is_first and reply_to:
                payload["context"] = {"message_id": reply_to}
            is_first = False

            result = self.api_call("messages", data=payload)

            msg_id = (
                result.get("messages", [{}])[0].get("id")
                if result.get("messages")
                else None
            )
            if not msg_id:
                self._log_error(f"Failed to send message: {json.dumps(result)}")

    # -- send_audio --------------------------------------------------------

    def send_audio(self, to, audio_path, reply_to=None):
        """Upload an audio file and send it as a WhatsApp audio message.

        Mirrors ``whatsapp_send_audio()`` in whatsapp.sh (lines 131-187).
        Returns True on success, False on failure.
        """
        # Step 1: Upload the audio file via multipart
        enc = MultipartEncoder()
        enc.add_field("messaging_product", "whatsapp")
        enc.add_file("file", audio_path, content_type="audio/mpeg")

        result = self.api_call("media", files=enc)

        media_id = result.get("id")
        if not media_id:
            self._log_error(f"Failed to upload audio: {json.dumps(result)}")
            return False

        # Step 2: Send the audio message referencing the uploaded media
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "audio",
            "audio": {"id": media_id},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}

        result = self.api_call("messages", data=payload)

        msg_id = (
            result.get("messages", [{}])[0].get("id")
            if result.get("messages")
            else None
        )
        if not msg_id:
            self._log_error(f"Failed to send audio message: {json.dumps(result)}")
            return False

        return True

    # -- mark_read ---------------------------------------------------------

    def mark_read(self, message_id):
        """Send a read receipt. Fire-and-forget — never raises.

        Mirrors ``whatsapp_mark_read()`` in whatsapp.sh (lines 193-207).
        """
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }

        url = f"{self._base_url}/messages"
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as _resp:
                pass
        except Exception:
            pass

    # -- download_media ----------------------------------------------------

    def download_media(self, media_id, output_path, validate_fn=None):
        """Download a media file from the WhatsApp Cloud API.

        Mirrors ``whatsapp_download_media()`` in whatsapp.sh (lines 236-297).

        * Step 1: Resolve ``media_id`` to a download URL.
        * Step 2: Download the file (max 1 redirect).
        * Validates size (max 16 MB, non-zero).
        * Optionally runs ``validate_fn(path) -> bool``, deleting on failure.
        * Returns True on success, False on failure.
        """
        # Step 1: Get media URL
        meta_url = f"{_BASE_API}/{media_id}"
        headers = {"Authorization": self._auth_header()}
        req = urllib.request.Request(meta_url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                meta_body = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            self._log_error(f"Failed to get media URL for media_id: {media_id} ({exc})")
            return False

        try:
            meta = json.loads(meta_body)
        except (json.JSONDecodeError, ValueError):
            self._log_error(f"Failed to get media URL for media_id: {media_id}")
            return False

        media_url = meta.get("url", "")
        if not media_url:
            self._log_error(f"Failed to get media URL for media_id: {media_id}")
            return False

        # Validate URL scheme (case-insensitive)
        if not media_url.lower().startswith("https://"):
            self._log_error("Invalid media URL scheme (must be HTTPS)")
            return False

        # Step 2: Download the file
        dl_req = urllib.request.Request(
            media_url,
            headers={"Authorization": self._auth_header()},
            method="GET",
        )

        try:
            with urllib.request.urlopen(dl_req, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:
            self._log_error(f"Failed to download media ({exc})")
            return False

        # Write to output path
        try:
            with open(output_path, "wb") as f:
                f.write(data)
        except OSError as exc:
            self._log_error(f"Failed to write media file ({exc})")
            return False

        file_size = len(data)

        # Validate file size
        if file_size > _MAX_MEDIA_SIZE:
            self._log_error(f"Downloaded media exceeds size limit: {file_size} bytes")
            try:
                os.remove(output_path)
            except OSError:
                pass
            return False

        if file_size == 0:
            self._log_error("Downloaded media is empty")
            try:
                os.remove(output_path)
            except OSError:
                pass
            return False

        # Optional content validation
        if validate_fn is not None and not validate_fn(output_path):
            self._log_error("Downloaded file failed content validation")
            try:
                os.remove(output_path)
            except OSError:
                pass
            return False

        self._log(f"Downloaded media to: {output_path} ({file_size} bytes)")
        return True

    # -- convenience download wrappers -------------------------------------

    def download_image(self, media_id, output_path):
        """Download an image and validate magic bytes.

        Mirrors ``whatsapp_download_image()`` in whatsapp.sh.
        """
        return self.download_media(media_id, output_path, validate_fn=validate_image_magic)

    def download_document(self, media_id, output_path):
        """Download a document (no content validation).

        Mirrors ``whatsapp_download_document()`` in whatsapp.sh.
        """
        return self.download_media(media_id, output_path)

    def download_audio(self, media_id, output_path):
        """Download audio and validate magic bytes.

        Mirrors ``whatsapp_download_audio()`` in whatsapp.sh.
        """
        return self.download_media(media_id, output_path, validate_fn=validate_audio_magic)
