"""Configuration management for Claudio.

Provides BotConfig (per-bot) and ClaudioConfig (global installation).
"""

import os
import re
import sys

# Only allow alphanumeric keys with underscores (standard env var names)
_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Restrict bot_id to safe filesystem characters
_BOT_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')


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
        if not bot_id or not _BOT_ID_RE.match(bot_id):
            raise ValueError(f"Invalid bot_id: {bot_id!r}")

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

        Updates self.model and does a targeted update of the MODEL= line
        in bot.env, preserving all other lines (comments, extra variables).
        Falls back to appending if MODEL= is not found.
        """
        if model not in ('opus', 'sonnet', 'haiku'):
            raise ValueError(f"Invalid model: {model}")

        self.model = model

        if not self.bot_dir:
            return

        bot_env_path = os.path.join(self.bot_dir, 'bot.env')
        new_line = f'MODEL="{_env_quote(self.model)}"'

        os.makedirs(self.bot_dir, exist_ok=True)

        # Read existing file, replace MODEL= line in-place
        existing_lines = []
        found = False
        try:
            with open(bot_env_path, 'r') as f:
                for line in f:
                    stripped = line.rstrip('\n')
                    if stripped.startswith('MODEL='):
                        existing_lines.append(new_line)
                        found = True
                    else:
                        existing_lines.append(stripped)
        except FileNotFoundError:
            pass

        if not found:
            existing_lines.append(new_line)

        old_umask = os.umask(0o077)
        try:
            with open(bot_env_path, 'w') as f:
                f.write('\n'.join(existing_lines) + '\n')
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


def save_bot_env(bot_dir, fields):
    """Write bot.env with proper escaping.

    Args:
        bot_dir: Path to the bot directory.
        fields: Dict of KEY -> value to write.
    """
    os.makedirs(bot_dir, mode=0o700, exist_ok=True)
    bot_env = os.path.join(bot_dir, 'bot.env')
    old_umask = os.umask(0o077)
    try:
        with open(bot_env, 'w') as f:
            for key, val in fields.items():
                f.write(f'{key}="{_env_quote(val)}"\n')
    finally:
        os.umask(old_umask)


class ClaudioConfig:
    """Manages the Claudio installation directory and service configuration.

    Ports claudio_init(), claudio_save_env(), claudio_list_bots(),
    claudio_load_bot(), and _migrate_to_multi_bot() from config.sh.
    """

    # Keys managed in service.env (global, not per-bot)
    _MANAGED_KEYS = [
        'PORT', 'WEBHOOK_URL', 'TUNNEL_NAME', 'TUNNEL_HOSTNAME',
        'WEBHOOK_RETRY_DELAY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID',
        'ELEVENLABS_MODEL', 'ELEVENLABS_STT_MODEL', 'MEMORY_ENABLED',
        'MEMORY_EMBEDDING_MODEL', 'MEMORY_CONSOLIDATION_MODEL',
    ]

    # Legacy per-bot keys to strip during migration
    _LEGACY_KEYS = [
        'MODEL', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
        'WEBHOOK_SECRET', 'MAX_HISTORY_LINES',
    ]

    # Default values for managed keys
    _DEFAULTS = {
        'PORT': '8421',
        'WEBHOOK_URL': '',
        'TUNNEL_NAME': '',
        'TUNNEL_HOSTNAME': '',
        'WEBHOOK_RETRY_DELAY': '60',
        'ELEVENLABS_API_KEY': '',
        'ELEVENLABS_VOICE_ID': 'iP95p4xoKVk53GoZ742B',
        'ELEVENLABS_MODEL': 'eleven_multilingual_v2',
        'ELEVENLABS_STT_MODEL': 'scribe_v1',
        'MEMORY_ENABLED': '1',
        'MEMORY_EMBEDDING_MODEL': 'sentence-transformers/all-MiniLM-L6-v2',
        'MEMORY_CONSOLIDATION_MODEL': 'haiku',
    }

    def __init__(self, claudio_path=None):
        self.claudio_path = claudio_path or os.path.join(
            os.path.expanduser('~'), '.claudio')
        self.env_file = os.path.join(self.claudio_path, 'service.env')
        self.log_file = os.path.join(self.claudio_path, 'claudio.log')
        self.env = {}  # Service env values

    def init(self):
        """Initialize Claudio directory structure and load config.

        Creates ~/.claudio/ if needed, loads service.env, and auto-migrates
        single-bot layouts to the multi-bot directory structure.
        """
        os.makedirs(self.claudio_path, mode=0o700, exist_ok=True)
        self.env = parse_env_file(self.env_file)
        self._migrate_to_multi_bot()

    @property
    def port(self):
        return int(self.env.get('PORT', '8421'))

    @property
    def webhook_url(self):
        return self.env.get('WEBHOOK_URL', '')

    @webhook_url.setter
    def webhook_url(self, value):
        self.env['WEBHOOK_URL'] = value

    @property
    def tunnel_name(self):
        return self.env.get('TUNNEL_NAME', '')

    @tunnel_name.setter
    def tunnel_name(self, value):
        self.env['TUNNEL_NAME'] = value

    @property
    def tunnel_hostname(self):
        return self.env.get('TUNNEL_HOSTNAME', '')

    @tunnel_hostname.setter
    def tunnel_hostname(self, value):
        self.env['TUNNEL_HOSTNAME'] = value

    def _migrate_to_multi_bot(self):
        """Migrate single-bot config to bots/ directory layout.

        Idempotent: skips if bots/ already exists or no token is configured.
        """
        bots_dir = os.path.join(self.claudio_path, 'bots')
        if os.path.isdir(bots_dir):
            return

        token = self.env.get('TELEGRAM_BOT_TOKEN', '')
        if not token:
            return  # Fresh install, nothing to migrate

        bot_dir = os.path.join(bots_dir, 'claudio')
        os.makedirs(bot_dir, mode=0o700, exist_ok=True)

        # Write per-bot env
        fields = {
            'TELEGRAM_BOT_TOKEN': token,
            'TELEGRAM_CHAT_ID': self.env.get('TELEGRAM_CHAT_ID', ''),
            'WEBHOOK_SECRET': self.env.get('WEBHOOK_SECRET', ''),
            'MODEL': self.env.get('MODEL', 'haiku'),
            'MAX_HISTORY_LINES': self.env.get('MAX_HISTORY_LINES', '100'),
        }
        save_bot_env(bot_dir, fields)

        # Move history.db and WAL/SHM files to per-bot dir
        for suffix in ('', '-wal', '-shm'):
            src = os.path.join(self.claudio_path, f'history.db{suffix}')
            if os.path.exists(src):
                os.rename(src, os.path.join(bot_dir, f'history.db{suffix}'))

        # Move CLAUDE.md to per-bot dir
        claude_md = os.path.join(self.claudio_path, 'CLAUDE.md')
        if os.path.isfile(claude_md):
            os.rename(claude_md, os.path.join(bot_dir, 'CLAUDE.md'))

        # Re-save service.env without per-bot keys
        self.save_service_env()
        sys.stderr.write(f"Migrated single-bot config to {bot_dir}\n")

    def save_service_env(self):
        """Write managed keys to service.env, preserving unmanaged keys.

        Unmanaged keys (e.g. HASS_TOKEN) are kept as-is. Legacy per-bot
        keys are stripped during migration.
        """
        all_keys = set(self._MANAGED_KEYS + self._LEGACY_KEYS)

        # Collect unmanaged lines from existing file
        extra_lines = []
        if os.path.isfile(self.env_file):
            with open(self.env_file) as f:
                for line in f:
                    line = line.rstrip('\n')
                    # Extract key from KEY=... lines
                    eq = line.find('=')
                    key = line[:eq] if eq > 0 else ''
                    if key not in all_keys:
                        extra_lines.append(line)

        old_umask = os.umask(0o077)
        try:
            with open(self.env_file, 'w') as f:
                for key in self._MANAGED_KEYS:
                    val = self.env.get(key, self._DEFAULTS.get(key, ''))
                    f.write(f'{key}="{_env_quote(val)}"\n')
                for line in extra_lines:
                    f.write(line + '\n')
        finally:
            os.umask(old_umask)

    def list_bots(self):
        """List all configured bot IDs (sorted)."""
        bots_dir = os.path.join(self.claudio_path, 'bots')
        if not os.path.isdir(bots_dir):
            return []
        result = []
        for name in sorted(os.listdir(bots_dir)):
            bot_env = os.path.join(bots_dir, name, 'bot.env')
            if os.path.isfile(bot_env):
                result.append(name)
        return result

    def load_bot(self, bot_id):
        """Load a bot's config as a BotConfig."""
        return BotConfig.from_env_files(bot_id, claudio_path=self.claudio_path)
