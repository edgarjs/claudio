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
- `lib/config.sh` — Multi-bot config management: global (`service.env`) and per-bot (`bots/<bot_id>/bot.env`) configuration, migration, loading, saving, listing
- `lib/server.sh` — Starts the Python HTTP server and cloudflared tunnel, multi-bot webhook registration
- `lib/server.py` — Python HTTP server (stdlib `http.server`, port 8421), multi-bot dispatch via secret-token matching, SIGHUP hot-reload, `/reload` endpoint
- `lib/telegram.sh` — Telegram Bot API integration (messages, webhooks, images, documents, voice), per-bot setup
- `lib/claude.sh` — Claude Code CLI wrapper with conversation context, per-bot SYSTEM_PROMPT.md and CLAUDE.md support
- `lib/history.sh` — Conversation history management, delegates to `lib/db.sh`, per-bot history database
- `lib/db.sh` — SQLite database layer for conversation storage
- `lib/log.sh` — Centralized logging
- `lib/health-check.sh` — Cron health-check script (every minute) for webhook monitoring; auto-restarts service if unreachable (throttled to once per 3 minutes, max 3 attempts), sends Telegram alert on failure
- `lib/tts.sh` — ElevenLabs text-to-speech for voice responses
- `lib/stt.sh` — ElevenLabs speech-to-text for voice message transcription
- `lib/backup.sh` — Automated backup management: rsync-based hourly/daily rotating backups with cron scheduling
- `lib/memory.sh` — Cognitive memory system (bash glue), invokes `lib/memory.py`
- `lib/memory.py` — Python memory backend: embeddings, retrieval, consolidation
- `lib/db.py` — Python SQLite helper with parameterized queries
- `lib/service.sh` — systemd/launchd service management, cloudflared setup, `bot_setup()` wizard, per-bot uninstall

**Directory structure:**
- `~/.claudio/service.env` — Global configuration
- `~/.claudio/bots/<bot_id>/bot.env` — Per-bot credentials and config
- `~/.claudio/bots/<bot_id>/history.db` — Per-bot conversation history (SQLite)
- `~/.claudio/bots/<bot_id>/SYSTEM_PROMPT.md` — Optional per-bot system prompt
- `~/.claudio/bots/<bot_id>/CLAUDE.md` — Optional per-bot Claude Code instructions

## Running Tests

Claudio uses [BATS](https://github.com/bats-core/bats-core) for testing.

```bash
# Install BATS (macOS)
brew install bats-core

# Install BATS (Linux/Debian/Ubuntu)
sudo apt-get install bats

# Run all tests
bats tests/

# Run a specific test file
bats tests/db.bats
bats tests/multibot.bats
```

Tests are located in the `tests/` directory. Key test suites:

- `tests/multibot.bats` — Multi-bot config: migration, loading, saving, listing (19 tests)
- `tests/db.bats` — SQLite conversation storage
- `tests/telegram.bats` — Telegram API integration
- `tests/claude.bats` — Claude Code CLI wrapper
- `tests/health-check.bats` — Health check and monitoring
- `tests/memory.bats` — Cognitive memory system

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
