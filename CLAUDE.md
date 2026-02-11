# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Claudio

Claudio is a Telegram-to-Claude Code bridge. It runs a local HTTP server (port 8421), tunneled via cloudflared, that receives Telegram webhook messages and forwards them as one-shot prompts to the Claude Code CLI. Responses are sent back to Telegram.

## Architecture

- `claudio` — Main CLI entry point, dispatches subcommands (`status`, `start`, `install`, `uninstall`, `update`, `restart`, `log`, `telegram setup`, `version`).
- `lib/config.sh` — Shared config loading, env file management (`$HOME/.claudio/service.env`).
- `lib/server.sh` — Starts the Python HTTP server and cloudflared named tunnel together. Handles webhook registration with retry logic.
- `lib/server.py` — Python HTTP server (stdlib `http.server`), listens on port 8421, routes POST `/telegram/webhook`.
- `lib/telegram.sh` — Telegram Bot API integration (send messages, parse webhooks, image download/validation, document download, voice message handling, setup wizard).
- `lib/claude.sh` — Claude Code CLI wrapper with conversation context injection.
- `lib/history.sh` — Conversation history wrapper, delegates to `lib/db.sh` for SQLite storage.
- `lib/db.sh` — SQLite database layer for conversation storage.
- `lib/log.sh` — Centralized logging with module prefix and file output.
- `lib/health-check.sh` — Cron health-check script (runs every minute) that calls `/health` endpoint. Auto-restarts the service if unreachable (throttled to once per 3 minutes, max 3 attempts). Sends Telegram alert after exhausting retries. State: `.last_restart_attempt`, `.restart_fail_count` in `$HOME/.claudio/`.
- `lib/tts.sh` — ElevenLabs text-to-speech integration for generating voice responses.
- `lib/stt.sh` — ElevenLabs speech-to-text integration for transcribing incoming voice messages.
- `lib/backup.sh` — Automated backup management: rsync-based hourly/daily rotating backups of `$HOME/.claudio/` with cron scheduling. Subcommands: `backup <dest>`, `backup status <dest>`, `backup cron install/uninstall`.
- `lib/memory.sh` — Cognitive memory system (bash glue). Invokes `lib/memory.py` for embedding-based retrieval and ACT-R activation scoring. Consolidates conversation history into long-term memories. Degrades gracefully if fastembed is not installed.
- `lib/mcp_tools.py` — MCP stdio server exposing Claudio tools: Telegram notifications (`send_telegram_message`), delayed service restart (`restart_service`), and git pull + restart (`update_service`). Pure stdlib, no external dependencies.
- `lib/memory.py` — Python backend for cognitive memory: embedding generation (fastembed), SQLite-backed storage, ACT-R activation scoring for retrieval, and memory consolidation via Claude.
- `lib/db.py` — Python SQLite helper providing parameterized queries to eliminate SQL injection risk. Used by `db.sh`.
- `lib/service.sh` — systemd (Linux) and launchd (macOS) service management. Also handles cloudflared installation and named tunnel setup during `claudio install`. Enables loginctl linger on install/update (so the user service survives logout) and disables it on uninstall if no other user services remain.
- Runtime config/state lives in `$HOME/.claudio/` (not in the repo).

## Development

Run locally with `./claudio start`. Requires `jq`, `curl`, `python3`, `sqlite3`, `cloudflared`, and `claude` CLI. The memory system optionally requires the `fastembed` Python package (degrades gracefully without it).

**Tests:** Run `bats tests/` (requires [bats-core](https://github.com/bats-core/bats-core)). Tests use an isolated `$CLAUDIO_PATH` to avoid touching production data.