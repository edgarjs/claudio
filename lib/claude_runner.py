"""Claude CLI runner for Claudio webhook handlers.

Ports the Claude invocation logic from lib/claude.sh to Python.
Stdlib only -- no external dependencies.

Designed to be imported by a future handlers.py orchestrator.
"""

import json
import os
import signal
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from collections import namedtuple

from .util import log, log_error
from .config import BotConfig

# -- Constants --

WEBHOOK_TIMEOUT = 600  # 10 minutes max per Claude invocation
_SIGTERM_GRACE = 5     # seconds to wait after SIGTERM before SIGKILL

_MODULE = "claude"

# Tools available to Claude during webhook invocations
_TOOLS_CSV = (
    "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,"
    "Task,TaskOutput,TaskStop,TodoWrite,"
    "mcp__claudio-tools__send_telegram_message,"
    "mcp__claudio-tools__restart_service"
)
_TOOLS_LIST = [t.strip() for t in _TOOLS_CSV.split(",")]

# -- Result type --

ClaudeResult = namedtuple('ClaudeResult', [
    'response',           # str: the text response
    'raw_json',           # dict or None: parsed JSON output
    'notifier_messages',  # str: newline-joined notification messages
    'tool_summary',       # str: newline-joined tool usage summaries
])


# -- Public API --

def find_claude_cmd():
    """Find the claude binary.

    Checks shutil.which first, then well-known install paths.
    Returns the absolute path if found and executable, else None.
    """
    found = shutil.which("claude")
    if found:
        return found

    home = os.path.expanduser("~")
    for candidate in [
        os.path.join(home, ".local", "bin", "claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


def build_mcp_config(lib_dir, telegram_token, chat_id, notifier_log):
    """Build the MCP server configuration dict for claude CLI.

    Args:
        lib_dir: Absolute path to the lib/ directory containing mcp_tools.py.
        telegram_token: Telegram bot token for async notifications.
        chat_id: Telegram chat ID for async notifications.
        notifier_log: Path to the temp file where MCP logs notification messages.

    Returns:
        dict matching the mcpServers JSON structure expected by --mcp-config.
    """
    return {
        "mcpServers": {
            "claudio-tools": {
                "command": "python3",
                "args": [os.path.join(lib_dir, "mcp_tools.py")],
                "env": {
                    "TELEGRAM_BOT_TOKEN": telegram_token,
                    "TELEGRAM_CHAT_ID": chat_id,
                    "NOTIFIER_LOG_FILE": notifier_log,
                },
            }
        }
    }


def run_claude(prompt, config, history_context='', memories=''):
    """Run the Claude CLI with a prompt and return the result.

    Args:
        prompt: The user's message text.
        config: A BotConfig instance with bot settings.
        history_context: Optional conversation history string.
        memories: Optional recalled memories string.

    Returns:
        A ClaudeResult namedtuple.
    """
    claude_cmd = find_claude_cmd()
    if claude_cmd is None:
        log_error(_MODULE, "claude command not found in common locations",
                  bot_id=config.bot_id)
        return ClaudeResult(
            response="Error: claude CLI not found",
            raw_json=None,
            notifier_messages='',
            tool_summary='',
        )

    # Build the full prompt with memories and history context
    full_prompt = _build_full_prompt(prompt, history_context, memories)

    # Resolve paths
    lib_dir = os.path.dirname(os.path.abspath(__file__))
    system_prompt = _load_system_prompt(config.bot_dir)

    # Create temp files (all cleaned up in finally)
    tmp_files = {}
    try:
        for name in ('mcp_config', 'notifier_log', 'tool_log',
                      'prompt_file', 'output_file', 'stderr_file'):
            fd, path = tempfile.mkstemp(prefix=f'claudio_{name}_')
            os.close(fd)
            os.chmod(path, 0o600)
            tmp_files[name] = path

        # Write MCP config
        mcp_cfg = build_mcp_config(
            lib_dir,
            config.telegram_token,
            config.telegram_chat_id,
            tmp_files['notifier_log'],
        )
        with open(tmp_files['mcp_config'], 'w') as f:
            json.dump(mcp_cfg, f)

        # Write prompt
        with open(tmp_files['prompt_file'], 'w') as f:
            f.write(full_prompt)

        # Build CLI args
        claude_args = [
            claude_cmd,
            '--disable-slash-commands',
            '--mcp-config', tmp_files['mcp_config'],
            '--model', config.model,
            '--no-chrome',
            '--no-session-persistence',
            '--output-format', 'json',
            '--tools', _TOOLS_CSV,
            '--allowedTools', *_TOOLS_LIST,
            '-p', '-',
        ]

        if system_prompt:
            claude_args.extend(['--append-system-prompt', system_prompt])

        if config.model != 'haiku':
            claude_args.extend(['--fallback-model', 'haiku'])

        # Build environment
        home = os.path.expanduser("~")
        env = os.environ.copy()
        env['CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'] = '1'
        env['CLAUDIO_NOTIFIER_LOG'] = tmp_files['notifier_log']
        env['CLAUDIO_TOOL_LOG'] = tmp_files['tool_log']
        # Ensure ~/.local/bin is on PATH (where claude is commonly installed)
        local_bin = os.path.join(home, ".local", "bin")
        if local_bin not in env.get('PATH', '').split(os.pathsep):
            env['PATH'] = local_bin + os.pathsep + env.get('PATH', '')

        # Run claude in its own session/process group so its child processes
        # cannot kill the webhook handler via process group signals
        log(_MODULE, f"Running claude (model={config.model})",
            bot_id=config.bot_id)

        with open(tmp_files['prompt_file'], 'r') as stdin_f, \
             open(tmp_files['output_file'], 'w') as stdout_f, \
             open(tmp_files['stderr_file'], 'w') as stderr_f:
            proc = subprocess.Popen(
                claude_args,
                stdin=stdin_f,
                stdout=stdout_f,
                stderr=stderr_f,
                env=env,
                start_new_session=True,
            )

        # Wait with timeout
        try:
            proc.wait(timeout=WEBHOOK_TIMEOUT)
        except subprocess.TimeoutExpired:
            log_error(_MODULE,
                      f"Claude timed out after {WEBHOOK_TIMEOUT}s, sending SIGTERM",
                      bot_id=config.bot_id)
            _kill_process_group(proc)

        # Read raw output
        with open(tmp_files['output_file'], 'r') as f:
            raw_output = f.read()

        # Log stderr if any
        with open(tmp_files['stderr_file'], 'r') as f:
            stderr_text = f.read()
        if stderr_text.strip():
            log(_MODULE, stderr_text.strip(), bot_id=config.bot_id)

        # Parse JSON output
        response = ''
        raw_json = None
        if raw_output:
            try:
                raw_json = json.loads(raw_output)
                response = raw_json.get('result', '')
            except (json.JSONDecodeError, ValueError):
                # Fallback: treat as plain text
                response = raw_output

        # Read notifier messages
        notifier_messages = _read_notifier_log(tmp_files['notifier_log'])

        # Read and dedup tool usage summaries
        tool_summary = _read_tool_log(tmp_files['tool_log'])

        # Persist token usage in background (best-effort)
        if raw_json is not None and config.db_file:
            t = threading.Thread(
                target=_persist_usage,
                args=(raw_json, config.db_file),
                daemon=True,
            )
            t.start()

        log(_MODULE,
            f"Claude finished (response_len={len(response)})",
            bot_id=config.bot_id)

        return ClaudeResult(
            response=response,
            raw_json=raw_json,
            notifier_messages=notifier_messages,
            tool_summary=tool_summary,
        )

    finally:
        for path in tmp_files.values():
            try:
                os.unlink(path)
            except OSError:
                pass


# -- Internal helpers --

def _build_full_prompt(prompt, history_context, memories):
    """Assemble the full prompt with optional memories and history context."""
    parts = []

    if memories:
        parts.append(f"<recalled-memories>\n{memories}\n</recalled-memories>\n")

    if history_context:
        parts.append(f"<conversation-history>\n{history_context}\n</conversation-history>")
        parts.append(f"\nNow respond to this new message:\n\n{prompt}")
    else:
        parts.append(prompt)

    return ''.join(parts)


def _load_system_prompt(bot_dir):
    """Load the global SYSTEM_PROMPT.md and optional per-bot CLAUDE.md.

    Args:
        bot_dir: Path to the bot directory (may be empty string).

    Returns:
        The combined system prompt string, or empty string if not found.
    """
    lib_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(lib_dir)
    prompt_path = os.path.join(repo_root, "SYSTEM_PROMPT.md")

    try:
        with open(prompt_path, 'r') as f:
            system_prompt = f.read()
    except OSError:
        return ''

    # Append per-bot CLAUDE.md if available
    if bot_dir:
        bot_claude_path = os.path.join(bot_dir, "CLAUDE.md")
        try:
            with open(bot_claude_path, 'r') as f:
                bot_claude_md = f.read()
            if bot_claude_md:
                system_prompt = system_prompt + "\n\n" + bot_claude_md
        except OSError:
            pass

    return system_prompt


def _kill_process_group(proc):
    """Send SIGTERM to the process group, wait, then SIGKILL if needed."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return

    try:
        proc.wait(timeout=_SIGTERM_GRACE)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _read_notifier_log(path):
    """Read notifier log, strip JSON quotes, and format as notification lines.

    Each line in the log is a JSON-encoded string (e.g., "some message").
    Output: newline-joined "[Notification: ...]" lines.
    """
    try:
        with open(path, 'r') as f:
            content = f.read()
    except OSError:
        return ''

    if not content.strip():
        return ''

    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip surrounding JSON quotes
        if len(line) >= 2 and line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        lines.append(f"[Notification: {line}]")

    return '\n'.join(lines)


def _read_tool_log(path):
    """Read tool usage log, dedup lines, and format as tool summary lines.

    Output: newline-joined "[Tool: ...]" lines with duplicates removed.
    """
    try:
        with open(path, 'r') as f:
            content = f.read()
    except OSError:
        return ''

    if not content.strip():
        return ''

    seen = set()
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(f"[Tool: {line}]")

    return '\n'.join(lines)


def _persist_usage(raw_json, db_file):
    """Persist Claude token usage to the token_usage table (best-effort).

    Args:
        raw_json: Parsed dict from Claude's JSON output.
        db_file: Path to the bot's SQLite database.
    """
    try:
        if not db_file:
            return

        usage = raw_json.get('usage', {})
        model_usage = raw_json.get('modelUsage', {})
        model = next(iter(model_usage), None) if model_usage else None

        conn = sqlite3.connect(db_file, timeout=10)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        try:
            conn.execute(
                '''INSERT INTO token_usage
                   (model, input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (
                    model,
                    usage.get('input_tokens', 0),
                    usage.get('output_tokens', 0),
                    usage.get('cache_read_input_tokens', 0),
                    usage.get('cache_creation_input_tokens', 0),
                    raw_json.get('total_cost_usd', 0),
                    raw_json.get('duration_ms', 0),
                )
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Best-effort -- never let usage tracking break the response flow
        pass
