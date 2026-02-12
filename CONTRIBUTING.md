# Contributing to Claudio

Contributions are welcome! Bug reports, feature requests, documentation fixes, and code contributions are all appreciated.

## Getting Started

1. Fork the repository on GitHub
2. Clone your fork locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/claudio.git
   cd claudio
   ```
3. Create a branch for your work:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Setup

Claudio is a shell/Python project with no build system. [ShellCheck](https://www.shellcheck.net/) is used for linting and runs automatically via a pre-commit hook.

Run locally with:

```bash
./claudio start
```

Runtime configuration and state are stored in `$HOME/.claudio/` (not in the repo).

## Project Structure

- `claudio` — Main CLI entry point, dispatches subcommands
- `lib/config.sh` — Multi-bot config management: global (`service.env`) and per-bot (`bots/<bot_id>/bot.env`) configuration, migration, loading, saving, listing, bot_id validation for security
- `lib/server.sh` — Starts the Python HTTP server and cloudflared tunnel, multi-bot webhook registration
- `lib/server.py` — Python HTTP server (stdlib `http.server`, port 8421), multi-bot dispatch via secret-token matching, SIGHUP hot-reload, `/reload` endpoint
- `lib/telegram.sh` — Telegram Bot API integration (messages, webhooks, images, documents, voice), per-bot setup (Bash handler path)
- `lib/whatsapp.sh` — WhatsApp Business API integration (Bash handler path)
- `lib/claude.sh` — Claude Code CLI wrapper with conversation context, global SYSTEM_PROMPT.md and per-bot CLAUDE.md support (Bash handler path)
- `lib/handlers.py` — Python webhook orchestrator: unified pipeline for Telegram and WhatsApp (Python handler path, enabled via `CLAUDIO_PYTHON_HANDLERS=1`)
- `lib/telegram_api.py` — Python Telegram Bot API client with retry logic
- `lib/whatsapp_api.py` — Python WhatsApp Business API client with retry logic
- `lib/elevenlabs.py` — Python ElevenLabs TTS/STT integration
- `lib/claude_runner.py` — Python Claude CLI runner with JSON parsing
- `lib/config.py` — Python BotConfig class for typed bot configuration
- `lib/util.py` — Shared Python utilities (sanitization, validation, multipart encoding, logging)
- `lib/history.sh` — Conversation history management, delegates to `lib/db.sh`, per-bot history database
- `lib/db.sh` — SQLite database layer for conversation storage
- `lib/log.sh` — Centralized logging
- `lib/health-check.sh` — Cron health-check script (every minute) for webhook monitoring; auto-restarts service if unreachable (throttled to once per 3 minutes, max 3 attempts), sends Telegram alert on failure
- `lib/tts.sh` — ElevenLabs text-to-speech for voice responses (Bash handler path)
- `lib/stt.sh` — ElevenLabs speech-to-text for voice message transcription (Bash handler path)
- `lib/backup.sh` — Automated backup management: rsync-based hourly/daily rotating backups with cron scheduling
- `lib/memory.sh` — Cognitive memory system (bash glue), invokes `lib/memory.py`
- `lib/memory.py` — Python memory backend: embeddings, retrieval, consolidation
- `lib/db.py` — Python SQLite helper with parameterized queries
- `lib/service.sh` — systemd/launchd service management, cloudflared setup, `bot_setup()` wizard, per-bot uninstall

**Multi-bot directory structure:**
- `~/.claudio/service.env` — Global configuration
- `~/.claudio/bots/<bot_id>/bot.env` — Per-bot credentials and config (bot_id must match `[a-zA-Z0-9_-]+`)
- `~/.claudio/bots/<bot_id>/history.db` — Per-bot conversation history (SQLite)
- `~/.claudio/bots/<bot_id>/CLAUDE.md` — Optional per-bot Claude Code instructions

## Running Tests

Claudio uses [BATS](https://github.com/bats-core/bats-core) for Bash tests and [pytest](https://docs.pytest.org/) for Python tests.

```bash
# Install BATS (macOS)
brew install bats-core

# Install BATS (Linux/Debian/Ubuntu)
sudo apt-get install bats

# Run Bash tests
bats tests/

# Run Python tests
python3 -m pytest tests/ -v

# Run a specific test file
bats tests/db.bats
python3 -m pytest tests/test_handlers.py -v
```

Tests are located in the `tests/` directory. Key test suites:

**Bash (BATS):**
- `tests/multibot.bats` — Multi-bot config: migration, loading, saving, listing (19 tests)
- `tests/db.bats` — SQLite conversation storage
- `tests/telegram.bats` — Telegram API integration
- `tests/claude.bats` — Claude Code CLI wrapper
- `tests/health-check.bats` — Health check and monitoring
- `tests/memory.bats` — Cognitive memory system

**Python (pytest):**
- `tests/test_util.py` — Shared utilities (sanitization, validation, multipart encoder)
- `tests/test_config.py` — BotConfig and env file parsing
- `tests/test_telegram_api.py` — TelegramClient API calls
- `tests/test_whatsapp_api.py` — WhatsAppClient API calls
- `tests/test_elevenlabs.py` — ElevenLabs TTS/STT
- `tests/test_claude_runner.py` — Claude CLI runner
- `tests/test_handlers.py` — Webhook orchestrator (integration tests)

When contributing, please:

- Run existing tests before submitting changes
- Add tests for new functionality when possible (especially multi-bot behavior)
- Ensure all tests pass

## Git Hooks

The project includes a pre-commit hook that runs ShellCheck and tests before each commit. To enable it:

```bash
git config core.hooksPath .githooks
```

This ensures tests pass before any commit is allowed.

## Making Changes

- Check existing issues and PRs to avoid duplicate work
- For larger changes, open an issue first to discuss your approach
- Follow existing code style and conventions
- Test your changes locally before submitting
- Keep commits focused and atomic

## Submitting a Pull Request

1. Push your branch to your fork
2. Open a pull request with a clear description of what and why
3. Reference related issues if applicable (e.g., "Fixes #123")

## Security

Claudio runs Claude Code with all tools auto-approved (`--tools` + `--allowedTools`) by design — there's no human at the terminal to approve prompts. When contributing:

- Be mindful of security implications in any change
- Do not introduce vulnerabilities that could expose user systems
- Report security issues privately to maintainers rather than opening a public issue

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
