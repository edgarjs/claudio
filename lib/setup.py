"""Interactive setup wizards for Claudio bot configuration."""

import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from lib.config import parse_env_file, save_bot_env
from lib.util import print_error, print_success, print_warning

TELEGRAM_API = "https://api.telegram.org/bot"
WHATSAPP_API = "https://graph.facebook.com/v21.0"

# Restrict bot_id to safe filesystem characters
_BOT_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')

# Timeout for polling /start message (seconds)
_POLL_TIMEOUT = 120


def _telegram_api_call(token, method, data=None, timeout=30):
    """Make a Telegram Bot API call.

    Args:
        token: Bot API token.
        method: API method name (e.g. 'getMe').
        data: Optional dict of POST parameters.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response dict.

    Raises:
        SetupError: On network or API errors.
    """
    url = f"{TELEGRAM_API}{token}/{method}"
    body = None
    if data:
        body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"description": str(e)}
        raise SetupError(
            f"Telegram API error ({method}): {err_body.get('description', e)}"
        ) from e
    except (urllib.error.URLError, OSError) as e:
        raise SetupError(f"Network error calling Telegram API: {e}") from e


def _whatsapp_api_call(phone_id, access_token, endpoint="", timeout=30):
    """Make a WhatsApp Graph API call.

    Args:
        phone_id: Phone Number ID.
        access_token: Bearer access token.
        endpoint: Optional sub-endpoint appended after phone_id.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response dict.

    Raises:
        SetupError: On network or API errors.
    """
    url = f"{WHATSAPP_API}/{phone_id}"
    if endpoint:
        url = f"{url}/{endpoint}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {}
        msg = err_body.get("error", {}).get("message", str(e))
        raise SetupError(f"WhatsApp API error: {msg}") from e
    except (urllib.error.URLError, OSError) as e:
        raise SetupError(f"Network error calling WhatsApp API: {e}") from e


class SetupError(Exception):
    """Raised when a setup step fails."""


def _validate_bot_id(bot_id):
    """Validate bot_id format. Raises SetupError on invalid input."""
    if not _BOT_ID_RE.match(bot_id):
        raise SetupError(
            f"Invalid bot name: '{bot_id}'. "
            "Use only letters, numbers, hyphens, and underscores."
        )


def _build_bot_env_fields(existing, telegram=None, whatsapp=None):
    """Build a merged bot.env fields dict.

    Starts from existing config, overlays telegram and/or whatsapp fields,
    and includes common fields (MODEL, MAX_HISTORY_LINES).

    Args:
        existing: Dict of existing bot.env values (from parse_env_file).
        telegram: Optional dict with keys: token, chat_id, webhook_secret.
        whatsapp: Optional dict with keys: phone_number_id, access_token,
                  app_secret, verify_token, phone_number.

    Returns:
        Ordered dict of KEY -> value for save_bot_env().
    """
    fields = {}

    # Telegram fields
    tg_token = ""
    tg_chat_id = ""
    tg_secret = ""

    if telegram:
        tg_token = telegram["token"]
        tg_chat_id = telegram["chat_id"]
        tg_secret = telegram["webhook_secret"]
    elif existing.get("TELEGRAM_BOT_TOKEN"):
        tg_token = existing["TELEGRAM_BOT_TOKEN"]
        tg_chat_id = existing.get("TELEGRAM_CHAT_ID", "")
        tg_secret = existing.get("WEBHOOK_SECRET", "")

    if tg_token:
        fields["TELEGRAM_BOT_TOKEN"] = tg_token
        fields["TELEGRAM_CHAT_ID"] = tg_chat_id
        fields["WEBHOOK_SECRET"] = tg_secret

    # WhatsApp fields
    wa_phone_id = ""
    wa_token = ""
    wa_secret = ""
    wa_verify = ""
    wa_phone = ""

    if whatsapp:
        wa_phone_id = whatsapp["phone_number_id"]
        wa_token = whatsapp["access_token"]
        wa_secret = whatsapp["app_secret"]
        wa_verify = whatsapp["verify_token"]
        wa_phone = whatsapp["phone_number"]
    elif existing.get("WHATSAPP_PHONE_NUMBER_ID"):
        wa_phone_id = existing["WHATSAPP_PHONE_NUMBER_ID"]
        wa_token = existing.get("WHATSAPP_ACCESS_TOKEN", "")
        wa_secret = existing.get("WHATSAPP_APP_SECRET", "")
        wa_verify = existing.get("WHATSAPP_VERIFY_TOKEN", "")
        wa_phone = existing.get("WHATSAPP_PHONE_NUMBER", "")

    if wa_phone_id:
        fields["WHATSAPP_PHONE_NUMBER_ID"] = wa_phone_id
        fields["WHATSAPP_ACCESS_TOKEN"] = wa_token
        fields["WHATSAPP_APP_SECRET"] = wa_secret
        fields["WHATSAPP_VERIFY_TOKEN"] = wa_verify
        fields["WHATSAPP_PHONE_NUMBER"] = wa_phone

    # Common fields (preserve existing or use defaults)
    fields["MODEL"] = existing.get("MODEL", "haiku")
    fields["MAX_HISTORY_LINES"] = existing.get("MAX_HISTORY_LINES", "100")

    return fields


def telegram_setup(config, bot_id=None):
    """Interactive Telegram bot setup wizard.

    Prompts for a bot token, validates it via the Telegram API, polls for
    a /start message, and saves the configuration.

    Args:
        config: ClaudioConfig instance (provides claudio_path, webhook_url).
        bot_id: Optional bot identifier for multi-bot setups.

    Raises:
        SystemExit: On validation failure, timeout, or missing config.
    """
    print("=== Claudio Telegram Setup ===")
    if bot_id:
        print(f"Bot: {bot_id}")
    print()

    try:
        token = input("Enter your Telegram Bot Token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    if not token:
        print_error("Token cannot be empty.")
        sys.exit(1)

    # Validate token via getMe
    try:
        me = _telegram_api_call(token, "getMe")
    except SetupError as e:
        print_error(f"Invalid bot token. {e}")
        sys.exit(1)

    if not me.get("ok"):
        print_error("Invalid bot token.")
        sys.exit(1)

    bot_name = me.get("result", {}).get("username", "unknown")
    bot_url = f"https://t.me/{bot_name}"
    print_success(f"Bot verified: @{bot_name}")
    print(f"Bot URL: {bot_url}")

    # Remove webhook temporarily so getUpdates works for polling
    try:
        _telegram_api_call(token, "deleteWebhook", {"drop_pending_updates": "true"})
    except SetupError:
        pass  # Non-critical

    print()
    print(f"Opening {bot_url} ...")
    print("Send /start to your bot from the Telegram account you want to use.")
    print("Waiting for the message...")

    # Attempt to open the bot URL in a browser (best-effort)
    try:
        webbrowser.open(bot_url)
    except Exception:
        pass

    # Poll for /start message
    chat_id = _poll_for_start(token)

    print_success(f"Received /start from chat_id: {chat_id}")

    # Send confirmation message
    try:
        _telegram_api_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "Hola! Please return to your terminal to complete the webhook setup.",
        })
    except SetupError:
        pass  # Non-critical

    # Verify tunnel is configured
    if not config.webhook_url:
        print_warning("No tunnel configured. Run 'claudio install' first.")
        sys.exit(1)

    # Save config
    if bot_id:
        _validate_bot_id(bot_id)
        _save_telegram_config(config, bot_id, token, chat_id)
    else:
        print_warning("No bot_id specified. Cannot save config without a bot identifier.")
        sys.exit(1)

    print_success("Setup complete!")


def _poll_for_start(token):
    """Poll Telegram getUpdates for a /start message.

    Args:
        token: Bot API token.

    Returns:
        The chat_id that sent /start.

    Raises:
        SystemExit: If polling times out after _POLL_TIMEOUT seconds.
    """
    start_time = time.monotonic()

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed >= _POLL_TIMEOUT:
            print_error("Timed out waiting for /start message. Please try again.")
            sys.exit(1)

        try:
            updates = _telegram_api_call(
                token, "getUpdates",
                {"timeout": "5", "allowed_updates": '["message"]'},
                timeout=15,
            )
        except SetupError:
            time.sleep(1)
            continue

        results = updates.get("result", [])
        if results:
            last = results[-1]
            msg = last.get("message", {})
            msg_text = msg.get("text", "")
            msg_chat_id = msg.get("chat", {}).get("id")

            if msg_text == "/start" and msg_chat_id:
                # Clear the processed update
                update_id = last.get("update_id", 0)
                try:
                    _telegram_api_call(
                        token, "getUpdates",
                        {"offset": str(update_id + 1)},
                    )
                except SetupError:
                    pass
                return str(msg_chat_id)

        time.sleep(1)


def _save_telegram_config(config, bot_id, token, chat_id):
    """Save Telegram credentials to bot.env, preserving existing WhatsApp config.

    Args:
        config: ClaudioConfig instance.
        bot_id: Bot identifier.
        token: Telegram bot token.
        chat_id: Telegram chat ID.
    """
    bot_dir = os.path.join(config.claudio_path, "bots", bot_id)
    bot_env_path = os.path.join(bot_dir, "bot.env")

    # Load existing config to preserve other platform's credentials
    existing = parse_env_file(bot_env_path)

    # Generate webhook secret only if not already set
    webhook_secret = existing.get("WEBHOOK_SECRET", "")
    if not webhook_secret:
        webhook_secret = secrets.token_hex(32)

    telegram = {
        "token": token,
        "chat_id": chat_id,
        "webhook_secret": webhook_secret,
    }

    fields = _build_bot_env_fields(existing, telegram=telegram)
    save_bot_env(bot_dir, fields)

    print_success(f"Bot config saved to {bot_dir}/bot.env")


def whatsapp_setup(config, bot_id=None):
    """Interactive WhatsApp Business API setup wizard.

    Prompts for credentials, validates via the Graph API, and saves
    the configuration.

    Args:
        config: ClaudioConfig instance (provides claudio_path, webhook_url).
        bot_id: Optional bot identifier for multi-bot setups.

    Raises:
        SystemExit: On validation failure or missing config.
    """
    print("=== Claudio WhatsApp Business API Setup ===")
    if bot_id:
        print(f"Bot: {bot_id}")
    print()
    print("You'll need the following from your WhatsApp Business account:")
    print("1. Phone Number ID (from Meta Business Suite)")
    print("2. Access Token (permanent token from Meta for Developers)")
    print("3. App Secret (from your Meta app settings)")
    print("4. Authorized phone number (the number you want to receive messages from)")
    print()

    try:
        phone_id = input("Enter your WhatsApp Phone Number ID: ").strip()
        if not phone_id:
            print_error("Phone Number ID cannot be empty.")
            sys.exit(1)

        access_token = input("Enter your WhatsApp Access Token: ").strip()
        if not access_token:
            print_error("Access Token cannot be empty.")
            sys.exit(1)

        app_secret = input("Enter your WhatsApp App Secret: ").strip()
        if not app_secret:
            print_error("App Secret cannot be empty.")
            sys.exit(1)

        phone_number = input("Enter authorized phone number (format: 1234567890): ").strip()
        if not phone_number:
            print_error("Phone number cannot be empty.")
            sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    # Generate verify token
    verify_token = secrets.token_hex(32)

    # Validate credentials via the Graph API
    try:
        result = _whatsapp_api_call(phone_id, access_token)
    except SetupError as e:
        print_error(
            f"Failed to verify WhatsApp credentials. "
            f"Check your Phone Number ID and Access Token. {e}"
        )
        sys.exit(1)

    verified_name = result.get("verified_name", "")
    if not verified_name:
        print_error(
            "Failed to verify WhatsApp credentials. "
            "Check your Phone Number ID and Access Token."
        )
        sys.exit(1)

    print_success(f"Credentials verified: {verified_name}")

    # Verify tunnel is configured
    if not config.webhook_url:
        print_warning("No tunnel configured. Run 'claudio install' first.")
        sys.exit(1)

    # Save config
    if bot_id:
        _validate_bot_id(bot_id)
        _save_whatsapp_config(
            config, bot_id, phone_id, access_token,
            app_secret, verify_token, phone_number,
        )
    else:
        print_warning("No bot_id specified. Cannot save config without a bot identifier.")
        sys.exit(1)

    # Print webhook configuration instructions
    print()
    print("=== Webhook Configuration ===")
    print("Configure your WhatsApp webhook in Meta for Developers:")
    print()
    print(f"  Callback URL: {config.webhook_url}/whatsapp/webhook")
    print(f"  Verify Token: {verify_token}")
    print()
    print("Subscribe to these webhook fields:")
    print("  - messages")
    print()
    print_success("Setup complete!")


def _save_whatsapp_config(config, bot_id, phone_id, access_token,
                          app_secret, verify_token, phone_number):
    """Save WhatsApp credentials to bot.env, preserving existing Telegram config.

    Args:
        config: ClaudioConfig instance.
        bot_id: Bot identifier.
        phone_id: WhatsApp Phone Number ID.
        access_token: WhatsApp access token.
        app_secret: WhatsApp app secret.
        verify_token: Generated verify token.
        phone_number: Authorized phone number.
    """
    bot_dir = os.path.join(config.claudio_path, "bots", bot_id)
    bot_env_path = os.path.join(bot_dir, "bot.env")

    # Load existing config to preserve other platform's credentials
    existing = parse_env_file(bot_env_path)

    whatsapp = {
        "phone_number_id": phone_id,
        "access_token": access_token,
        "app_secret": app_secret,
        "verify_token": verify_token,
        "phone_number": phone_number,
    }

    fields = _build_bot_env_fields(existing, whatsapp=whatsapp)
    save_bot_env(bot_dir, fields)

    print_success(f"Bot config saved to {bot_dir}/bot.env")


def bot_setup(config, bot_id):
    """Interactive platform choice menu for bot setup.

    Checks existing configuration, shows available options, and dispatches
    to the appropriate platform setup wizard(s).

    Args:
        config: ClaudioConfig instance.
        bot_id: Bot identifier.

    Raises:
        SystemExit: On invalid choice.
    """
    bot_dir = os.path.join(config.claudio_path, "bots", bot_id)
    bot_env_path = os.path.join(bot_dir, "bot.env")

    # Check what's already configured
    has_telegram = False
    has_whatsapp = False
    if os.path.isfile(bot_env_path):
        existing = parse_env_file(bot_env_path)
        has_telegram = bool(existing.get("TELEGRAM_BOT_TOKEN"))
        has_whatsapp = bool(existing.get("WHATSAPP_PHONE_NUMBER_ID"))

    print()
    print(f"=== Setting up bot: {bot_id} ===")

    # Show what's already configured
    if has_telegram or has_whatsapp:
        print()
        print("Current configuration:")
        if has_telegram:
            print("  [ok] Telegram configured")
        if has_whatsapp:
            print("  [ok] WhatsApp configured")

    # Offer platform choices
    print()
    print("Which platform(s) do you want to configure?")
    print("  1) Telegram only")
    print("  2) WhatsApp Business API only")
    print("  3) Both Telegram and WhatsApp")
    if has_telegram:
        print("  4) Re-configure Telegram")
    if has_whatsapp:
        print("  5) Re-configure WhatsApp")
    print()

    max_choice = 3
    if has_telegram:
        max_choice = 4
    if has_whatsapp:
        max_choice = 5

    try:
        choice = input(f"Enter choice [1-{max_choice}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    if choice == "1":
        telegram_setup(config, bot_id)
        # Offer to set up WhatsApp too
        if not has_whatsapp:
            print()
            try:
                add_whatsapp = input(
                    "Would you like to also configure WhatsApp for this bot? [y/N] "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if add_whatsapp.lower().startswith("y"):
                print()
                whatsapp_setup(config, bot_id)

    elif choice == "2":
        whatsapp_setup(config, bot_id)
        # Offer to set up Telegram too
        if not has_telegram:
            print()
            try:
                add_telegram = input(
                    "Would you like to also configure Telegram for this bot? [y/N] "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if add_telegram.lower().startswith("y"):
                print()
                telegram_setup(config, bot_id)

    elif choice == "3":
        telegram_setup(config, bot_id)
        print()
        whatsapp_setup(config, bot_id)

    elif choice == "4":
        if has_telegram:
            telegram_setup(config, bot_id)
        else:
            print("Invalid choice.")
            sys.exit(1)

    elif choice == "5":
        if has_whatsapp:
            whatsapp_setup(config, bot_id)
        else:
            print("Invalid choice.")
            sys.exit(1)

    else:
        print("Invalid choice. Please enter a valid option.")
        sys.exit(1)
