# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Claudio

Claudio is a Telegram-to-Claude Code bridge. It runs a local HTTP server (port 8421), tunneled via cloudflared, that receives Telegram webhook messages and forwards them as one-shot prompts to the Claude Code CLI. Responses are sent back to Telegram.

## Architecture

- `claudio` — Main CLI entry point, dispatches subcommands (`status`, `start`, `install`, `uninstall`, `update`, `restart`, `log`, `telegram setup`, `version`).
- `lib/config.sh` — Shared config loading, env file management (`$HOME/.claudio/service.env`).
- `lib/server.sh` — Starts the Python HTTP server and cloudflared named tunnel together. Handles webhook registration with retry logic.
- `lib/server.py` — Python HTTP server (stdlib `http.server`), listens on port 8421, routes POST `/telegram/webhook`.
- `lib/telegram.sh` — Telegram Bot API integration (send messages, parse webhooks, image download/validation, voice message handling, setup wizard).
- `lib/claude.sh` — Claude Code CLI wrapper with conversation context injection.
- `lib/history.sh` — Conversation history wrapper, delegates to `lib/db.sh` for SQLite storage.
- `lib/db.sh` — SQLite database layer for conversation storage.
- `lib/log.sh` — Centralized logging with module prefix and file output.
- `lib/health-check.sh` — Cron health-check script (runs every minute) that calls `/health` endpoint. Auto-restarts the service if unreachable (throttled to once per 3 minutes, max 3 attempts). Sends Telegram alert after exhausting retries. State: `.last_restart_attempt`, `.restart_fail_count` in `$HOME/.claudio/`.
- `lib/tts.sh` — ElevenLabs text-to-speech integration for generating voice responses.
- `lib/stt.sh` — ElevenLabs speech-to-text integration for transcribing incoming voice messages.
- `lib/service.sh` — systemd (Linux) and launchd (macOS) service management. Also handles cloudflared installation and named tunnel setup during `claudio install`.
- Runtime config/state lives in `$HOME/.claudio/` (not in the repo).

## Development

Run locally with `./claudio start`. Requires `jq`, `curl`, `python3`, `cloudflared`, and `claude` CLI.

**Tests:** Run `bats tests/` (requires [bats-core](https://github.com/bats-core/bats-core)). Tests use an isolated `$CLAUDIO_PATH` to avoid touching production data.
