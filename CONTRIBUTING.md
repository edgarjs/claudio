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

Claudio is a pure-Python project (stdlib only, no external dependencies except optional `fastembed`). No build system required.

Run locally with:

```bash
./claudio start
```

Runtime configuration and state are stored in `$HOME/.claudio/` (not in the repo).

## Project Structure

- `claudio` — Python CLI entry point, dispatches subcommands via `lib/cli.py`
- `lib/cli.py` — CLI dispatch logic with lazy imports per command for fast startup
- `lib/config.py` — `ClaudioConfig` class for global config, `BotConfig` for per-bot config, env file parsing, bot_id validation
- `lib/server.py` — Python HTTP server (stdlib `http.server`, port 8421), multi-bot dispatch via secret-token matching, SIGHUP hot-reload, `/reload` endpoint
- `lib/handlers.py` — Webhook orchestrator: unified pipeline for Telegram and WhatsApp
- `lib/telegram_api.py` — `TelegramClient` class with retry logic (send, download, typing, reactions)
- `lib/whatsapp_api.py` — `WhatsAppClient` class with retry logic (send, download, mark read)
- `lib/elevenlabs.py` — ElevenLabs TTS/STT integration
- `lib/claude_runner.py` — Claude CLI runner with JSON parsing
- `lib/setup.py` — Interactive setup wizards for Telegram, WhatsApp, and multi-platform bots
- `lib/service.py` — Service management: systemd/launchd, cloudflared tunnel, webhook registration, cron, hooks
- `lib/backup.py` — Automated backup management: rsync-based hourly/daily rotating backups with cron scheduling
- `lib/health_check.py` — Cron health-check script: auto-restart, disk/log/backup monitoring, Telegram alerts
- `lib/util.py` — Shared utilities (sanitization, validation, multipart encoding, logging, CLI output)
- `lib/db.py` — SQLite helper with parameterized queries and retry logic
- `lib/memory.py` — Cognitive memory system: embeddings, retrieval, consolidation (optional fastembed)
- `lib/mcp_tools.py` — MCP stdio server for Telegram notifications and service restart
- `lib/hooks/post-tool-use.py` — PostToolUse hook for tool usage tracking

**Multi-bot directory structure:**
- `~/.claudio/service.env` — Global configuration
- `~/.claudio/bots/<bot_id>/bot.env` — Per-bot credentials and config (bot_id must match `[a-zA-Z0-9_-]+`)
- `~/.claudio/bots/<bot_id>/history.db` — Per-bot conversation history (SQLite)
- `~/.claudio/bots/<bot_id>/CLAUDE.md` — Optional per-bot Claude Code instructions

## Running Tests

Claudio uses [pytest](https://docs.pytest.org/) for testing.

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run a specific test file
python3 -m pytest tests/test_handlers.py -v

# Run tests matching a pattern
python3 -m pytest tests/ -k "test_webhook" -v
```

Tests are located in the `tests/` directory:

- `tests/test_config.py` — Config management, multi-bot migration, env file I/O
- `tests/test_util.py` — Shared utilities (sanitization, validation, multipart encoder)
- `tests/test_setup.py` — Setup wizards (Telegram, WhatsApp, bot selection)
- `tests/test_service.py` — Service management (systemd, cron, webhooks, symlinks)
- `tests/test_backup.py` — Backup operations (rsync, rotation, cron scheduling)
- `tests/test_health_check.py` — Health check (restart throttling, disk/log/backup monitoring)
- `tests/test_cli.py` — CLI dispatch, version, usage, argument parsing
- `tests/test_server.py` — HTTP server routing and webhook dispatch
- `tests/test_handlers.py` — Webhook orchestrator (integration tests)
- `tests/test_telegram_api.py` — TelegramClient API calls
- `tests/test_whatsapp_api.py` — WhatsAppClient API calls
- `tests/test_elevenlabs.py` — ElevenLabs TTS/STT
- `tests/test_claude_runner.py` — Claude CLI runner

When contributing, please:

- Run existing tests before submitting changes
- Add tests for new functionality when possible (especially multi-bot behavior)
- Ensure all tests pass

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
