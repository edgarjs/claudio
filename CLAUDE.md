# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Claudio

Claudio is a messaging-to-Claude Code bridge. It supports both Telegram and WhatsApp Business API, running a local HTTP server (port 8421), tunneled via cloudflared, that receives webhook messages and forwards them as one-shot prompts to the Claude Code CLI. Responses are sent back to the originating platform.

## Architecture

- `claudio` — Python CLI entry point, imports `lib/cli.py` and dispatches subcommands (`status`, `start`, `install [bot_id]`, `uninstall {<bot_id>|--purge}`, `update`, `restart`, `log`, `telegram setup`, `whatsapp setup`, `version`).
- `lib/cli.py` — CLI dispatch logic. Uses `sys.argv` (not argparse) with lazy imports per command for fast startup.
- `lib/config.py` — `ClaudioConfig` class for global config (`~/.claudio/service.env`), `BotConfig` class for per-bot config (`~/.claudio/bots/<bot_id>/bot.env`). Functions: `parse_env_file()`, `save_bot_env()`, `save_model()`. Auto-migrates single-bot to multi-bot layout.
- `lib/server.py` — Python HTTP server (stdlib `http.server`), listens on port 8421, routes POST `/telegram/webhook` and POST/GET `/whatsapp/webhook`. Multi-bot dispatch: matches Telegram webhooks via secret-token header, WhatsApp webhooks via HMAC-SHA256 signature verification. Supports dual-platform bots (same bot_id serving both Telegram and WhatsApp). Loads bot registry from `~/.claudio/bots/*/bot.env`. SIGHUP handler for hot-reload. Composite queue keys (`bot_id:chat_id` for Telegram, `bot_id:phone_number` for WhatsApp) for per-bot, per-user message isolation. `/reload` endpoint (requires `MANAGEMENT_SECRET` authentication). Webhook processing delegates to `lib/handlers.py`.
- `lib/handlers.py` — Webhook orchestrator: parses webhooks, runs unified message pipeline (media download, voice transcription, Claude invocation, response delivery). Speech provider dispatch (`_stt_transcribe()`, `_tts_convert()`) selects ElevenLabs or Speechmatics based on `SPEECH_PROVIDER` config. Entry point: `process_webhook()`.
- `lib/telegram_api.py` — `TelegramClient` class: send messages (4096-char chunking with Markdown fallback), send voice, typing indicator, reactions, file downloads with magic byte validation. Retry on 429/5xx.
- `lib/whatsapp_api.py` — `WhatsAppClient` class: send messages (4096-char chunking), send audio, mark read, media downloads (two-step URL resolution). Retry on 429/5xx.
- `lib/elevenlabs.py` — ElevenLabs TTS (`tts_convert()`) and STT (`stt_transcribe()`). Stdlib only.
- `lib/speechmatics.py` — Speechmatics TTS (`tts_convert()`) and STT (`stt_transcribe()`). TTS returns WAV audio; STT uses async batch API (submit job, poll, fetch transcript). Stdlib only.
- `lib/claude_runner.py` — Claude CLI invocation with `start_new_session=True`, MCP config, JSON output parsing, token usage persistence. Returns `ClaudeResult` namedtuple.
- `lib/setup.py` — Interactive setup wizards: `telegram_setup()`, `whatsapp_setup()`, `bot_setup()`. Validates credentials via API calls, polls for Telegram `/start`, generates secrets, saves config.
- `lib/service.py` — Service management: systemd/launchd unit generation, symlink install, cloudflared tunnel setup, webhook registration with retry, cron health-check install, Claude hooks install, `service_status()`, `service_restart()`, `service_update()`, `service_install()`, `service_uninstall()`.
- `lib/backup.py` — Automated backup management: rsync-based hourly/daily rotating backups of `~/.claudio/` with hardlink deduplication. Functions: `backup_run()`, `backup_status()`, `backup_cron_install()`, `backup_cron_uninstall()`.
- `lib/health_check.py` — Standalone cron health-check script (runs every minute). Calls `/health` endpoint, auto-restarts service if unreachable (throttled to once per 3 minutes, max 3 attempts), sends Telegram alert after exhausting retries. Additional checks when healthy: disk usage, log rotation, backup freshness, recent log analysis. Self-contained `_parse_env_file()` for cron's minimal PATH.
- `lib/util.py` — Shared utilities: `sanitize_for_prompt()`, `summarize()`, filename validation, magic byte checks (image/audio/OGG), `MultipartEncoder`, `strip_markdown()`, logging helpers, CLI output helpers (`print_error`, `print_success`, `print_warning`).
- `lib/db.py` — Python SQLite helper providing parameterized queries with retry logic.
- `lib/memory.py` — Cognitive memory system: embedding generation (fastembed), SQLite-backed storage, ACT-R activation scoring for retrieval, and memory consolidation via Claude. Degrades gracefully if fastembed is not installed.
- `lib/mcp_tools.py` — MCP stdio server exposing Claudio tools: Telegram notifications (`send_telegram_message`) and delayed service restart (`restart_service`). Pure stdlib, no external dependencies.
- `lib/hooks/post-tool-use.py` — PostToolUse hook that appends compact tool usage summaries to `$CLAUDIO_TOOL_LOG`. Captures Read, Write, Edit, Bash, Glob, Grep, Task, WebSearch, WebFetch usage. Skips MCP tools (already tracked by the notifier system). Active only when `CLAUDIO_TOOL_LOG` is set.
- Runtime config/state lives in `$HOME/.claudio/` (not in the repo). Multi-bot directory structure: `~/.claudio/bots/<bot_id>/` containing `bot.env`, `CLAUDE.md`, `history.db`, and SQLite WAL files.

## Development

Run locally with `./claudio start`. Requires `python3`, `sqlite3`, `cloudflared`, and `claude` CLI. The memory system optionally requires the `fastembed` Python package (degrades gracefully without it).

**Tests:** `python3 -m pytest tests/` — 673 tests covering all modules (config, util, setup, service, backup, health_check, server, handlers, telegram_api, whatsapp_api, elevenlabs, speechmatics, claude_runner, cli).
