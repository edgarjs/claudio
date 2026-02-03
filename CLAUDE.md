# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Claudio

Claudio is a Telegram-to-Claude Code bridge. It runs a local HTTP server (port 8421), tunneled via cloudflared, that receives Telegram webhook messages and forwards them as one-shot prompts to the Claude Code CLI. Responses are sent back to Telegram.

## Architecture

- `bin/claudio` — Main CLI entry point, dispatches subcommands (`start`, `install`, `uninstall`, `update`, `restart`, `telegram setup`).
- `lib/config.sh` — Shared config loading, env file management (`$HOME/.claudio/service.env`).
- `lib/server.sh` — Starts the Python HTTP server and cloudflared tunnel together. Handles ephemeral URL detection and auto webhook registration.
- `lib/server.py` — Python HTTP server (stdlib `http.server`), listens on port 8421, routes POST `/telegram/webhook`.
- `lib/telegram.sh` — Telegram Bot API integration (send messages, parse webhooks, setup wizard).
- `lib/claude.sh` — Claude Code CLI wrapper with conversation context injection.
- `lib/history.sh` — Conversation history (last 5 pairs) stored in `$HOME/.claudio/history.jsonl`.
- `lib/service.sh` — systemd (Linux) and launchd (macOS) service management. Also handles cloudflared installation and tunnel setup (ephemeral or named) during `claudio install`.
- Runtime config/state lives in `$HOME/.claudio/` (not in the repo).

## Development

No build system, test suite, or linter. Run locally with `bash bin/claudio start`. Requires `jq`, `curl`, `python3`, `cloudflared`, and `claude` CLI.
