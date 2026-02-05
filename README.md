# Claudio

![](header.png)

Claudio is an adapter for Claude Code CLI and Telegram. It makes a tunnel between a private network and Telegram's API. So that users can chat with Claude Code remotely, in a safe way.

```
                       +---------------------------------------------+
                       |              Remote Machine                 |
                       |                                             |
                       |                                             |
  +----------+         |    +---------+       +-----------------+    |
  | Telegram |<--------+--->| Claudio |<----->| Claude Code CLI |    |
  +----------+         |    +---------+       +-----------------+    |
                       +---------------------------------------------+
```

[![CI](https://github.com/edgarjs/claudio/actions/workflows/ci.yml/badge.svg)](https://github.com/edgarjs/claudio/actions/workflows/ci.yml)

---

## Overview

Claudio starts a local HTTP server that listens on port 8421, and creates a tunnel using [cloudflared](https://github.com/cloudflare/cloudflared). When the user sends a message from Telegram, it's sent to `<cloudflare-tunnel-url>/telegram/webhook` and forwarded to the Claude Code CLI.

The user message is passed as a one-shot prompt, along with some context to maintain continuity. The last 100 messages are kept by default (configurable via `MAX_HISTORY_LINES`).

After Claude Code finishes, it outputs the response in plain text to stdout for Claudio to capture it and forward it to Telegram API.

---

## Installation

### **CAUTION: Security Risk**

The whole purpose of Claudio is to give you remote access to Claude Code from your Telegram app — without breaking Anthropic's Terms of Service. This means Claude Code runs with full permissions by design:

**⚠️ CLAUDE CODE IS EXECUTED WITH `--dangerously-skip-permissions`, `--permission-mode bypassPermissions`, AND `--disable-slash-commands`**

**The environment variable `IS_SANDBOX=1` is also set to bypass the root user restriction.**

These flags are intentional. Since there's no human in front of the terminal to approve permission prompts, Claude Code must run autonomously. Claudio mitigates the risk through: webhook secret validation (HMAC), single authorized `chat_id`, and binding the HTTP server to localhost only (external access goes through cloudflared).

### Requirements

- Claude Code CLI (with Pro/Max subscription)
- Linux/macOS/WSL
- Telegram bot token
- Homebrew (macOS only, for installing dependencies)
- `python3`, `curl`
- `sqlite3`, `jq`, `cloudflared` (auto-installed if missing)

### Setup

1. Clone the repository and run the install command:

```bash
git clone https://github.com/edgarjs/claudio.git
cd claudio
./claudio install
```

This will:

- Install dependencies (`sqlite3`, `jq`, `cloudflared`) if not already present
- Create a symlink at `~/.local/bin/claudio` so you can run `claudio` from anywhere
- Set up a Cloudflare **named tunnel** (permanent URL, requires a free Cloudflare account)
- Install a systemd/launchd service

> **Note:** If `~/.local/bin` is not in your PATH, you'll need to add it. See the installation output for instructions.

2. Set up Telegram bot

In Telegram, message `@BotFather` with `/newbot` and follow instructions to create a bot. At the end, you'll be given a secret token, copy it and then run:

```bash
claudio telegram setup
```

Paste your Telegram token when asked, and press Enter. Then, send a `/start` message to your bot from the Telegram account that you'll use to communicate with Claude Code.

The setup wizard will confirm when it receives the message and finish. Once done, the service restarts automatically, and you can start chatting with Claude Code.

> For security, only the `chat_id` captured during setup is authorized to send messages.

> A cron job runs every 5 minutes to verify the webhook is registered and re-registers it if needed.

### Status

Check the service and webhook status:

```bash
claudio status
```

### Update

To update Claudio to the latest stable release:

```bash
claudio update
```

### Logs

To view recent logs (default 50 lines):

```bash
claudio log
```

Follow logs in real time with `-f`, or change the line count with `-n`:

```bash
claudio log -f
claudio log -n 100
```

### Uninstall

To stop and remove the service run:

```bash
claudio uninstall
```

If you want to remove the `$HOME/.claudio` directory completely, run:

```bash
claudio uninstall --purge
```

---

## Customization

### Model

Claudio uses Haiku by default. If you want to switch to another model, just send the name of the model as a command: `/opus`, `/sonnet`, or `/haiku`. And then continue chatting.

### System Prompt

Claudio appends a system prompt that defines its persona, core principles, and communication style (optimized for chat). The default is generated on first run at `$HOME/.claudio/SYSTEM_PROMPT.md`. You can customize it by editing that file — it's read at runtime so no need to restart.

### Configuration

Claudio stores its configuration and other files in `$HOME/.claudio/`. Settings are managed in the `service.env` file within that directory. After any manual edits, restart to apply your changes:

```bash
claudio restart
```

#### Environment Variables

The following variables can be set in `$HOME/.claudio/service.env`:

**Server**

- `PORT` — HTTP server listening port. Default: `8421`.

**Claude**

- `MODEL` — Claude model to use. Accepts `haiku`, `sonnet`, or `opus`. Default: `haiku`. Can also be changed at runtime via Telegram commands `/haiku`, `/sonnet`, `/opus`.
- `MAX_HISTORY_LINES` — Maximum number of conversation messages to keep in the database. Older messages are trimmed automatically. Default: `100`.

**Telegram**

- `TELEGRAM_BOT_TOKEN` — Telegram Bot API token. Set automatically during `claudio telegram setup`.
- `TELEGRAM_CHAT_ID` — Authorized Telegram chat ID. Only messages from this chat are processed. Set automatically during `claudio telegram setup`.
- `WEBHOOK_URL` — Public URL where Telegram sends webhook updates (e.g. `https://claudio.example.com`). Set automatically when using a named tunnel.
- `WEBHOOK_SECRET` — HMAC secret for validating incoming webhook requests. Auto-generated on first run if not set.
- `WEBHOOK_RETRY_DELAY` — Seconds between webhook registration retry attempts. Default: `60`.

**Tunnel**

- `TUNNEL_NAME` — Name of the Cloudflare named tunnel. Set during `claudio install`.
- `TUNNEL_HOSTNAME` — Hostname for the named tunnel (e.g. `claudio.example.com`). Set during `claudio install`.

> Most of these variables are configured automatically by `claudio install` and `claudio telegram setup`. Manual editing is only needed for fine-tuning.

---

## Testing

Claudio uses [BATS](https://github.com/bats-core/bats-core) (Bash Automated Testing System) for integration tests.

```bash
# Run all tests
bats tests/

# Run a specific test file
bats tests/db.bats
```

---

## Roadmap

**Completed**

- [x] Webhook signature validation
- [x] Race conditions — Migrated from JSONL to SQLite
- [x] Basic integration tests
- [x] Input validation for model commands
- [x] ShellCheck linting for scripts
- [x] Cloudflared URL detection fails silently after 30 seconds
- [x] Webhook health check cron job
- [x] No retries with backoff for Telegram API
- [x] Health check (`/health`) doesn't verify actual system state
- [x] Auto-install dependencies (`sqlite3`, `jq`, `cloudflared`) during `claudio install`
- [x] Show webhook registration failure reason instead of generic warning
- [x] Add `claudio` to `$PATH` via symlink during install
- [x] Environment variables documentation

**Future**

- [ ] Support for editing messages and reactions
- [ ] File uploads
- [ ] Image uploads
- [ ] Rate limiting
- [ ] Voice messages from bot (TTS)
- [ ] Voice messages from human (STT)
- [ ] Support for group chats

---

## License

Claudio is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

---

## Contributing

Contributions are welcome! Please read the [CONTRIBUTING](CONTRIBUTING.md) file for details on how to contribute.

---

## Acknowledgments

Claudio is built on top of [Claude](https://claude.ai/) and [Claude Code](https://claude.ai/code).

---
