# Claudio

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

Claudio starts a local HTTP server that listens on port 8421, and creates a tunnel using [cloudflared](https://github.com/cloudflare/cloudflared). When the user sends a message from Telegram, it's sent to `<cloudflare-tunnel-url>/telegram/webhook` and forwarded it to the Claude Code CLI.

The user message is passed as a one-shot prompt, along with some context to maintain continuity. Only the last 5 message pairs (user + assistant) are injected in the context to keep it small.

After Claude Code finishes, it outputs the response in plain text to stdout for Claudio to capture it and forward it to Telegram API.

---

## Installation

### **CAUTION: Security Risk**

As you should know already, Claude Code has direct access to your machine terminal and filesystem. Beware this will expose it to your Telegram account.

**⚠️ CLAUDE CODE IS EXECUTED WITH `--dangerously-skip-permissions`, `--disable-slash-commands`, AND `--permission-mode bypassPermissions`**

### Requirements

- Claude Code CLI (with Pro/Max subscription)
- Linux/macOS/WSL
- Telegram bot token
- Homebrew (macOS only, for installing dependencies)
- `sqlite3`, `jq`, `cloudflared` (auto-installed if missing)

### Setup

1. Clone the repository and run the install command:

```bash
git clone https://github.com/edgarjs/claudio.git
cd claudio
./claudio install
```

#### Installing as root (servers)

If you're installing as root, you must specify a user for the service to run as:

```bash
./claudio install --user claudio
```

This is required because the Claude CLI cannot run as root for security reasons. The command will:
- Create a system user named `claudio` (if it doesn't exist)
- Install a system-level systemd service that runs as that user
- Set up all configuration files with proper ownership

This will:

- Install dependencies (`sqlite3`, `jq`, `cloudflared`) if not already present
- Create a symlink at `~/.local/bin/claudio` so you can run `claudio` from anywhere
- Prompt you to choose between a **quick tunnel** (ephemeral, no account needed, URL changes on restart) or a **named tunnel** (permanent URL, requires a free Cloudflare account)
- Configure the tunnel and install a systemd/launchd service

> **Note:** If `~/.local/bin` is not in your PATH, you'll need to add it. See the installation output for instructions.

2. Set up Telegram bot

In Telegram, message `@BotFather` with `/newbot` and follow instructions to create a bot. At the end, you'll be given a secret token, copy it and then run:

```bash
claudio telegram setup
```

Paste your Telegram token when asked, and press Enter. Then, send a `/start` message to your bot from the Telegram account that you'll use to communicate with Claude Code.

The setup wizard will confirm when it receives the message and finish. Once done, the service restarts automatically, and you can start chatting with Claude Code.

> For security, only the `chat_id` captured during setup is authorized to send messages.

> If using a quick tunnel, the Telegram webhook is re-registered automatically each time the service starts.

> A cron job runs every 5 minutes to verify the webhook is registered and re-registers it if needed.

### Update

To update Claudio to the latest stable release:

```bash
claudio update
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

To make Claude Code respond with chat-friendly formatted messages, Claudio adjusts the system prompt by appending this:

```markdown
## Communication Style

- You communicate through a chat interface. Messages should feel like chat — not essays.
- Keep response to 1-2 short paragraphs. If more detail is needed, give the key point first, then ask if the human wants you to elaborate.
- NEVER use markdown tables under any circumstances. Use lists instead.
- NEVER use markdown headers (`#`), horizontal rules (`---`), or image syntax (`![](...)`). These are not supported in chat apps. Use **bold text** for emphasis instead of headers.
```

Once Claudio is installed, you can customize this prompt by editing the file at `$HOME/.claudio/SYSTEM_PROMPT.md`. The file is read at runtime so no need to restart.

### Configuration

Claudio stores its configuration and other files in `$HOME/.claudio/`. You can change some settings in the `service.env` file within that directory. If you do so, don't forget to restart to apply your changes:

```bash
claudio restart
```

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

**Future**
- [ ] Support for editing messages and reactions
- [ ] File uploads
- [ ] Image uploads
- [ ] Rate limiting
- [ ] Voice messages from bot (TTS)
- [ ] Voice messages from human (STT)
- [ ] Environment variables documentation
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
