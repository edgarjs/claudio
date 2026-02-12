# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Claudio

Claudio is a messaging-to-Claude Code bridge. It supports both Telegram and WhatsApp Business API, running a local HTTP server (port 8421), tunneled via cloudflared, that receives webhook messages and forwards them as one-shot prompts to the Claude Code CLI. Responses are sent back to the originating platform.

## Architecture

- `claudio` — Main CLI entry point, dispatches subcommands (`status`, `start`, `install [bot_id]`, `uninstall {<bot_id>|--purge}`, `update`, `restart`, `log`, `telegram setup`, `whatsapp setup`, `version`).
- `lib/config.sh` — Multi-bot config management. Handles global config (`$HOME/.claudio/service.env`) and per-bot config (`$HOME/.claudio/bots/<bot_id>/bot.env`). Functions: `claudio_load_bot()`, `claudio_save_bot_env()`, `claudio_list_bots()`, `_migrate_to_multi_bot()` (auto-migrates single-bot installs).
- `lib/server.sh` — Starts the Python HTTP server and cloudflared named tunnel together. Handles webhook registration with retry logic. `register_all_webhooks()` registers webhooks for all configured bots.
- `lib/server.py` — Python HTTP server (stdlib `http.server`), listens on port 8421, routes POST `/telegram/webhook` and POST/GET `/whatsapp/webhook`. Multi-bot dispatch: matches Telegram webhooks via secret-token header, WhatsApp webhooks via HMAC-SHA256 signature verification. Supports dual-platform bots (same bot_id serving both Telegram and WhatsApp). Loads bot registry from `~/.claudio/bots/*/bot.env`. SIGHUP handler for hot-reload. Composite queue keys (`bot_id:chat_id` for Telegram, `bot_id:phone_number` for WhatsApp) for per-bot, per-user message isolation. `/reload` endpoint (requires `MANAGEMENT_SECRET` authentication). Webhook processing delegates to `lib/handlers.py`.
- **Python webhook handler modules**:
  - `lib/handlers.py` — Webhook orchestrator: parses webhooks, runs unified message pipeline (media download, voice transcription, Claude invocation, response delivery). Entry point: `process_webhook()`.
  - `lib/telegram_api.py` — `TelegramClient` class: send messages (4096-char chunking with Markdown fallback), send voice, typing indicator, reactions, file downloads with magic byte validation. Retry on 429/5xx.
  - `lib/whatsapp_api.py` — `WhatsAppClient` class: send messages (4096-char chunking), send audio, mark read, media downloads (two-step URL resolution). Retry on 429/5xx.
  - `lib/elevenlabs.py` — ElevenLabs TTS (`tts_convert()`) and STT (`stt_transcribe()`). Stdlib only.
  - `lib/claude_runner.py` — Claude CLI invocation with `start_new_session=True`, MCP config, JSON output parsing, token usage persistence. Returns `ClaudeResult` namedtuple.
  - `lib/config.py` — `BotConfig` class with typed fields for all bot.env + service.env keys. `save_model()` for /opus, /sonnet, /haiku commands.
  - `lib/util.py` — Shared utilities: `sanitize_for_prompt()`, `summarize()`, filename validation, magic byte checks (image/audio/OGG), `MultipartEncoder`, `strip_markdown()`, logging helpers.
- **Bash modules** (setup, health-check, and CLI):
  - `lib/telegram.sh` — Telegram Bot API: `telegram_api()`, `telegram_send_message()` (used by setup wizard + health-check alerts), and `telegram_setup()` for interactive bot configuration.
  - `lib/whatsapp.sh` — WhatsApp Business API: `whatsapp_api()`, `whatsapp_send_message()`, and `whatsapp_setup()` for interactive bot configuration.
- `lib/history.sh` — Conversation history wrapper, delegates to `lib/db.sh` for SQLite storage. Per-bot history stored in `$CLAUDIO_BOT_DIR/history.db`.
- `lib/db.sh` — SQLite database layer for conversation storage.
- `lib/log.sh` — Centralized logging with module prefix, optional bot_id (from `CLAUDIO_BOT_ID` env var), and file output. Format: `[timestamp] [module] [bot_id] message`.
- `lib/health-check.sh` — Cron health-check script (runs every minute) that calls `/health` endpoint. Auto-restarts the service if unreachable (throttled to once per 3 minutes, max 3 attempts). Sends Telegram alert after exhausting retries. Additional checks when healthy: disk usage alerts, log rotation, backup freshness, and recent log analysis (errors, restart loops, slow API — configurable via `LOG_CHECK_WINDOW` and `LOG_ALERT_COOLDOWN`). State: `.last_restart_attempt`, `.restart_fail_count`, `.last_log_alert` in `$HOME/.claudio/`. Loads first bot's credentials for alerting.
- `lib/backup.sh` — Automated backup management: rsync-based hourly/daily rotating backups of `$HOME/.claudio/` with cron scheduling. Subcommands: `backup <dest>`, `backup status <dest>`, `backup cron install/uninstall`.
- `lib/memory.sh` — Cognitive memory system (bash glue). Invokes `lib/memory.py` for embedding-based retrieval and ACT-R activation scoring. Consolidates conversation history into long-term memories. Degrades gracefully if fastembed is not installed.
- `lib/mcp_tools.py` — MCP stdio server exposing Claudio tools: Telegram notifications (`send_telegram_message`) and delayed service restart (`restart_service`). Pure stdlib, no external dependencies.
- `lib/hooks/post-tool-use.py` — PostToolUse hook that appends compact tool usage summaries to `$CLAUDIO_TOOL_LOG`. Captures Read, Write, Edit, Bash, Glob, Grep, Task, WebSearch, WebFetch usage. Skips MCP tools (already tracked by the notifier system). Active only when `CLAUDIO_TOOL_LOG` is set.
- `lib/memory.py` — Python backend for cognitive memory: embedding generation (fastembed), SQLite-backed storage, ACT-R activation scoring for retrieval, and memory consolidation via Claude.
- `lib/db.py` — Python SQLite helper providing parameterized queries to eliminate SQL injection risk. Used by `db.sh`.
- `lib/service.sh` — systemd (Linux) and launchd (macOS) service management. Also handles cloudflared installation and named tunnel setup during `claudio install`. `bot_setup()` wizard for interactive bot configuration. `service_install()` accepts optional bot_id (defaults to "claudio"). `service_uninstall()` can remove individual bots or purge all data. Enables loginctl linger on install/update (so the user service survives logout) and disables it on uninstall if no other user services remain.
- Runtime config/state lives in `$HOME/.claudio/` (not in the repo). Multi-bot directory structure: `~/.claudio/bots/<bot_id>/` containing `bot.env`, `CLAUDE.md`, `history.db`, and SQLite WAL files.

## Development

Run locally with `./claudio start`. Requires `jq`, `curl`, `python3`, `sqlite3`, `cloudflared`, and `claude` CLI. The memory system optionally requires the `fastembed` Python package (degrades gracefully without it).

**Tests:**
- Bash: `bats tests/` (requires [bats-core](https://github.com/bats-core/bats-core)). Tests use an isolated `$CLAUDIO_PATH` to avoid touching production data.
- Python: `python3 -m pytest tests/` — covers the Python webhook handler modules (util, config, telegram_api, whatsapp_api, elevenlabs, claude_runner, handlers).