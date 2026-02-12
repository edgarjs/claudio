"""Bot configuration management for Claudio webhook handlers.

Provides a BotConfig dataclass that loads from bot.env + service.env.
"""

import os
import re
import sys

# Only allow alphanumeric keys with underscores (standard env var names)
_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def parse_env_file(path):
    """Parse a KEY="value" or KEY=value env file.

    Mirrors parse_env_file() in server.py and _safe_load_env() in config.sh.
    Duplicated here to avoid circular imports (server.py will import handlers.py
    which imports config.py).

    Keys must match [A-Za-z_][A-Za-z0-9_]*. Invalid keys are skipped.
    """
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                eq = line.find('=')
                if eq < 1:
                    continue
                key = line[:eq]
                if not _ENV_KEY_RE.match(key):
                    sys.stderr.write(
                        f"[config] Skipping invalid key in {path}: {key!r}\n"
                    )
                    continue
                val = line[eq + 1:]
                if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                    val = val.replace('\\n', '\n')
                    val = val.replace('\\`', '`')
                    val = val.replace('\\$', '$')
                    val = val.replace('\\"', '"')
                    val = val.replace('\\\\', '\\')
                result[key] = val
    except (OSError, IOError):
        pass
    return result


class BotConfig:
    """Typed configuration for a single bot.

    Loads from a bot_config dict (as built by server.py's load_bots())
    and optionally merges in service.env globals.
    """

    __slots__ = (
        'bot_id', 'bot_dir',
        # Telegram
        'telegram_token', 'telegram_chat_id', 'webhook_secret',
        # WhatsApp
        'whatsapp_phone_number_id', 'whatsapp_access_token',
        'whatsapp_app_secret', 'whatsapp_verify_token', 'whatsapp_phone_number',
        # Common
        'model', 'max_history_lines',
        # ElevenLabs (from service.env)
        'elevenlabs_api_key', 'elevenlabs_voice_id', 'elevenlabs_model',
        'elevenlabs_stt_model',
        # Memory (from service.env)
        'memory_enabled',
        # Database
        'db_file',
    )

    def __init__(self, bot_id, bot_dir=None,
                 telegram_token='', telegram_chat_id='', webhook_secret='',
                 whatsapp_phone_number_id='', whatsapp_access_token='',
                 whatsapp_app_secret='', whatsapp_verify_token='',
                 whatsapp_phone_number='',
                 model='haiku', max_history_lines=100,
                 elevenlabs_api_key='', elevenlabs_voice_id='iP95p4xoKVk53GoZ742B',
                 elevenlabs_model='eleven_multilingual_v2',
                 elevenlabs_stt_model='scribe_v1',
                 memory_enabled=True, db_file=''):
        self.bot_id = bot_id
        self.bot_dir = bot_dir or ''
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.webhook_secret = webhook_secret
        self.whatsapp_phone_number_id = whatsapp_phone_number_id
        self.whatsapp_access_token = whatsapp_access_token
        self.whatsapp_app_secret = whatsapp_app_secret
        self.whatsapp_verify_token = whatsapp_verify_token
        self.whatsapp_phone_number = whatsapp_phone_number
        self.model = model
        self.max_history_lines = int(max_history_lines)
        self.elevenlabs_api_key = elevenlabs_api_key
        self.elevenlabs_voice_id = elevenlabs_voice_id
        self.elevenlabs_model = elevenlabs_model
        self.elevenlabs_stt_model = elevenlabs_stt_model
        self.memory_enabled = memory_enabled
        self.db_file = db_file or (os.path.join(bot_dir, 'history.db') if bot_dir else '')

    @classmethod
    def from_bot_config(cls, bot_id, bot_config, service_env=None):
        """Build a BotConfig from a server.py bot_config dict + service.env.

        Args:
            bot_id: The bot identifier.
            bot_config: Dict from server.py's bots or whatsapp_bots registry.
            service_env: Optional dict of service.env values (for ElevenLabs, memory, etc.)
        """
        svc = service_env or {}
        bot_dir = bot_config.get('bot_dir', '')

        return cls(
            bot_id=bot_id,
            bot_dir=bot_dir,
            # Telegram
            telegram_token=bot_config.get('token', ''),
            telegram_chat_id=bot_config.get('chat_id', ''),
            webhook_secret=bot_config.get('secret', ''),
            # WhatsApp
            whatsapp_phone_number_id=bot_config.get('phone_number_id', ''),
            whatsapp_access_token=bot_config.get('access_token', ''),
            whatsapp_app_secret=bot_config.get('app_secret', ''),
            whatsapp_verify_token=bot_config.get('verify_token', ''),
            whatsapp_phone_number=bot_config.get('phone_number', ''),
            # Common
            model=bot_config.get('model', 'haiku'),
            max_history_lines=bot_config.get('max_history_lines', '100'),
            # ElevenLabs (from service.env)
            elevenlabs_api_key=svc.get('ELEVENLABS_API_KEY', ''),
            elevenlabs_voice_id=svc.get('ELEVENLABS_VOICE_ID', 'iP95p4xoKVk53GoZ742B'),
            elevenlabs_model=svc.get('ELEVENLABS_MODEL', 'eleven_multilingual_v2'),
            elevenlabs_stt_model=svc.get('ELEVENLABS_STT_MODEL', 'scribe_v1'),
            # Memory
            memory_enabled=svc.get('MEMORY_ENABLED', '1') == '1',
            db_file=os.path.join(bot_dir, 'history.db') if bot_dir else '',
        )

    @classmethod
    def from_env_files(cls, bot_id, claudio_path=None):
        """Build a BotConfig by reading bot.env and service.env directly.

        This is the full-resolution path used when building config from scratch
        (rather than from the server.py in-memory registry).
        """
        if claudio_path is None:
            claudio_path = os.path.join(os.path.expanduser('~'), '.claudio')

        service_env_path = os.path.join(claudio_path, 'service.env')
        bot_dir = os.path.join(claudio_path, 'bots', bot_id)
        bot_env_path = os.path.join(bot_dir, 'bot.env')

        svc = parse_env_file(service_env_path)
        bot_env = parse_env_file(bot_env_path)

        return cls(
            bot_id=bot_id,
            bot_dir=bot_dir,
            # Telegram
            telegram_token=bot_env.get('TELEGRAM_BOT_TOKEN', ''),
            telegram_chat_id=bot_env.get('TELEGRAM_CHAT_ID', ''),
            webhook_secret=bot_env.get('WEBHOOK_SECRET', ''),
            # WhatsApp
            whatsapp_phone_number_id=bot_env.get('WHATSAPP_PHONE_NUMBER_ID', ''),
            whatsapp_access_token=bot_env.get('WHATSAPP_ACCESS_TOKEN', ''),
            whatsapp_app_secret=bot_env.get('WHATSAPP_APP_SECRET', ''),
            whatsapp_verify_token=bot_env.get('WHATSAPP_VERIFY_TOKEN', ''),
            whatsapp_phone_number=bot_env.get('WHATSAPP_PHONE_NUMBER', ''),
            # Common
            model=bot_env.get('MODEL', 'haiku'),
            max_history_lines=bot_env.get('MAX_HISTORY_LINES', '100'),
            # ElevenLabs (from service.env)
            elevenlabs_api_key=svc.get('ELEVENLABS_API_KEY', ''),
            elevenlabs_voice_id=svc.get('ELEVENLABS_VOICE_ID', 'iP95p4xoKVk53GoZ742B'),
            elevenlabs_model=svc.get('ELEVENLABS_MODEL', 'eleven_multilingual_v2'),
            elevenlabs_stt_model=svc.get('ELEVENLABS_STT_MODEL', 'scribe_v1'),
            # Memory
            memory_enabled=svc.get('MEMORY_ENABLED', '1') == '1',
        )

    def save_model(self, model):
        """Persist a model change to bot.env.

        Updates self.model and rewrites the bot.env file, preserving
        all other values. Mirrors claudio_save_bot_env() in config.sh.
        """
        if model not in ('opus', 'sonnet', 'haiku'):
            raise ValueError(f"Invalid model: {model}")

        self.model = model

        if not self.bot_dir:
            return

        bot_env_path = os.path.join(self.bot_dir, 'bot.env')

        lines = []
        # Telegram
        if self.telegram_token:
            lines.append(f'TELEGRAM_BOT_TOKEN="{_env_quote(self.telegram_token)}"')
            lines.append(f'TELEGRAM_CHAT_ID="{_env_quote(self.telegram_chat_id)}"')
            lines.append(f'WEBHOOK_SECRET="{_env_quote(self.webhook_secret)}"')
        # WhatsApp
        if self.whatsapp_phone_number_id:
            lines.append(f'WHATSAPP_PHONE_NUMBER_ID="{_env_quote(self.whatsapp_phone_number_id)}"')
            lines.append(f'WHATSAPP_ACCESS_TOKEN="{_env_quote(self.whatsapp_access_token)}"')
            lines.append(f'WHATSAPP_APP_SECRET="{_env_quote(self.whatsapp_app_secret)}"')
            lines.append(f'WHATSAPP_VERIFY_TOKEN="{_env_quote(self.whatsapp_verify_token)}"')
            lines.append(f'WHATSAPP_PHONE_NUMBER="{_env_quote(self.whatsapp_phone_number)}"')
        # Common
        lines.append(f'MODEL="{_env_quote(self.model)}"')
        lines.append(f'MAX_HISTORY_LINES="{_env_quote(str(self.max_history_lines))}"')

        os.makedirs(self.bot_dir, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            with open(bot_env_path, 'w') as f:
                f.write('\n'.join(lines) + '\n')
        finally:
            os.umask(old_umask)


def _env_quote(val):
    """Escape a value for double-quoted env file format.

    Mirrors _env_quote() in config.sh.
    """
    val = val.replace('\\', '\\\\')
    val = val.replace('"', '\\"')
    val = val.replace('$', '\\$')
    val = val.replace('`', '\\`')
    val = val.replace('\n', '\\n')
    return val
