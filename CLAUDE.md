# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Claudio

Claudio is a Telegram-to-Claude Code bridge. It runs a local HTTP server (port 8421), tunneled via cloudflared, that receives Telegram webhook messages and forwards them as one-shot prompts to the Claude Code CLI. Responses are sent back to Telegram.

## Architecture

- `claudio` — Main CLI entry point, dispatches subcommands (`status`, `start`, `install [bot_id]`, `uninstall {<bot_id>|--purge}`, `update`, `restart`, `log`, `telegram setup`, `version`).
- `lib/config.sh` — Multi-bot config management. Handles global config (`$HOME/.claudio/service.env`) and per-bot config (`$HOME/.claudio/bots/<bot_id>/bot.env`). Functions: `claudio_load_bot()`, `claudio_save_bot_env()`, `claudio_list_bots()`, `_migrate_to_multi_bot()` (auto-migrates single-bot installs).
- `lib/server.sh` — Starts the Python HTTP server and cloudflared named tunnel together. Handles webhook registration with retry logic. `register_all_webhooks()` registers webhooks for all configured bots.
- `lib/server.py` — Python HTTP server (stdlib `http.server`), listens on port 8421, routes POST `/telegram/webhook`. Multi-bot dispatch: matches incoming webhooks to bots via secret-token header, loads bot registry from `~/.claudio/bots/*/bot.env`. SIGHUP handler for hot-reload. Composite queue keys (`bot_id:chat_id`) for per-bot message isolation. `/reload` endpoint (requires `MANAGEMENT_SECRET` authentication). Logging includes bot_id via `log_msg()` helper.
- `lib/telegram.sh` — Telegram Bot API integration (send messages, parse webhooks, image download/validation, document download, voice message handling). `telegram_setup()` accepts optional bot_id for per-bot configuration. Model commands (`/haiku`, `/sonnet`, `/opus`) save to bot.env when `CLAUDIO_BOT_DIR` is set.
- `lib/claude.sh` — Claude Code CLI wrapper with conversation context injection. Supports per-bot SYSTEM_PROMPT.md and CLAUDE.md (loaded from `$CLAUDIO_BOT_DIR` when set).
- `lib/history.sh` — Conversation history wrapper, delegates to `lib/db.sh` for SQLite storage. Per-bot history stored in `$CLAUDIO_BOT_DIR/history.db`.
- `lib/db.sh` — SQLite database layer for conversation storage.
- `lib/log.sh` — Centralized logging with module prefix, optional bot_id (from `CLAUDIO_BOT_ID` env var), and file output. Format: `[timestamp] [module] [bot_id] message`.
- `lib/health-check.sh` — Cron health-check script (runs every minute) that calls `/health` endpoint. Auto-restarts the service if unreachable (throttled to once per 3 minutes, max 3 attempts). Sends Telegram alert after exhausting retries. Additional checks when healthy: disk usage alerts, log rotation, backup freshness, and recent log analysis (errors, restart loops, slow API — configurable via `LOG_CHECK_WINDOW` and `LOG_ALERT_COOLDOWN`). State: `.last_restart_attempt`, `.restart_fail_count`, `.last_log_alert` in `$HOME/.claudio/`. Loads first bot's credentials for alerting.
- `lib/tts.sh` — ElevenLabs text-to-speech integration for generating voice responses.
- `lib/stt.sh` — ElevenLabs speech-to-text integration for transcribing incoming voice messages.
- `lib/backup.sh` — Automated backup management: rsync-based hourly/daily rotating backups of `$HOME/.claudio/` with cron scheduling. Subcommands: `backup <dest>`, `backup status <dest>`, `backup cron install/uninstall`.
- `lib/memory.sh` — Cognitive memory system (bash glue). Invokes `lib/memory.py` for embedding-based retrieval and ACT-R activation scoring. Consolidates conversation history into long-term memories. Degrades gracefully if fastembed is not installed.
- `lib/mcp_tools.py` — MCP stdio server exposing Claudio tools: Telegram notifications (`send_telegram_message`) and delayed service restart (`restart_service`). Pure stdlib, no external dependencies.
- `lib/hooks/post-tool-use.py` — PostToolUse hook that appends compact tool usage summaries to `$CLAUDIO_TOOL_LOG`. Captures Read, Write, Edit, Bash, Glob, Grep, Task, WebSearch, WebFetch usage. Skips MCP tools (already tracked by the notifier system). Active only when `CLAUDIO_TOOL_LOG` is set.
- `lib/memory.py` — Python backend for cognitive memory: embedding generation (fastembed), SQLite-backed storage, ACT-R activation scoring for retrieval, and memory consolidation via Claude.
- `lib/db.py` — Python SQLite helper providing parameterized queries to eliminate SQL injection risk. Used by `db.sh`.
- `lib/service.sh` — systemd (Linux) and launchd (macOS) service management. Also handles cloudflared installation and named tunnel setup during `claudio install`. `bot_setup()` wizard for interactive bot configuration. `service_install()` accepts optional bot_id (defaults to "claudio"). `service_uninstall()` can remove individual bots or purge all data. Enables loginctl linger on install/update (so the user service survives logout) and disables it on uninstall if no other user services remain.
- Runtime config/state lives in `$HOME/.claudio/` (not in the repo). Multi-bot directory structure: `~/.claudio/bots/<bot_id>/` containing `bot.env`, `SYSTEM_PROMPT.md`, `CLAUDE.md`, `history.db`, and SQLite WAL files.

## Development

Run locally with `./claudio start`. Requires `jq`, `curl`, `python3`, `sqlite3`, `cloudflared`, and `claude` CLI. The memory system optionally requires the `fastembed` Python package (degrades gracefully without it).

**Tests:** Run `bats tests/` (requires [bats-core](https://github.com/bats-core/bats-core)). Tests use an isolated `$CLAUDIO_PATH` to avoid touching production data.