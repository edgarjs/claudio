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

The user message is passed as a one-shot prompt, along with some context to maintain continuity. All messages are kept in the database, with the last 100 used as conversation context.

After Claude Code finishes, it outputs the response in plain text to stdout for Claudio to capture it and forward it to Telegram API.

---

## Installation

### **CAUTION: Security Risk**

The whole purpose of Claudio is to give you remote access to Claude Code from your Telegram app — without breaking Anthropic's Terms of Service. This means Claude Code runs with full permissions by design:

**⚠️ CLAUDE CODE IS EXECUTED WITH `--dangerously-skip-permissions`, `--permission-mode bypassPermissions`, AND `--disable-slash-commands`**

**When running as root, `IS_SANDBOX=1` is required for Claude Code to start. Claudio sets this automatically when it detects root. For non-root users, this variable is optional.**

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

- Install dependencies (`sqlite3`, `jq`, `cloudflared`, `fastembed`) if not already present
- Create a symlink at `~/.local/bin/claudio` so you can run `claudio` from anywhere
- Set up a Cloudflare **named tunnel** (permanent URL, requires a free Cloudflare account)
- Install a systemd/launchd service
- Enable loginctl linger on Linux (so the service survives logout on headless systems)

> **Note:** If `~/.local/bin` is not in your PATH, you'll need to add it. See the installation output for instructions.

2. Set up Telegram bot

In Telegram, message `@BotFather` with `/newbot` and follow instructions to create a bot. At the end, you'll be given a secret token, copy it and then run:

```bash
claudio telegram setup
```

Paste your Telegram token when asked, and press Enter. Then, send a `/start` message to your bot from the Telegram account that you'll use to communicate with Claude Code.

The setup wizard will confirm when it receives the message and finish. Once done, the service restarts automatically, and you can start chatting with Claude Code.

> For security, only the `chat_id` captured during setup is authorized to send messages.

> A cron job runs every minute to monitor the webhook endpoint. It verifies the webhook is registered and re-registers it if needed. If the server is unreachable, it auto-restarts the service (throttled to once per 3 minutes, max 3 attempts). After exhausting restart attempts without recovery, it sends a Telegram alert and stops retrying until the server responds with HTTP 200. To reset the restart counter, delete `$HOME/.claudio/.last_restart_attempt` and `$HOME/.claudio/.restart_fail_count`.

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

> On Linux, `claudio update` also enables loginctl linger for existing installs upgrading to this version.

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

> On Linux, uninstall disables loginctl linger if no other user services remain enabled.

---

## Customization

### Model

Claudio uses Haiku by default. If you want to switch to another model, just send the name of the model as a command: `/opus`, `/sonnet`, or `/haiku`. And then continue chatting.

### Voice

Claudio supports voice messages in both directions using ElevenLabs. Set `ELEVENLABS_API_KEY` in your `service.env` to enable voice features (`ELEVENLABS_VOICE_ID` defaults to Chris if not set). When you send a voice message, Claudio transcribes it (STT), processes it, and responds with a voice message (as a reply) followed by a text version for reference. Text messages always get text-only responses. If `ELEVENLABS_API_KEY` is not set, voice messages will be rejected with an error reply.

### Images

You can send photos or image files directly to Claudio. Include an optional caption to tell Claude what to do with the image, or send it without a caption and Claude will describe it.

- **Supported formats:** JPEG, PNG, GIF, WebP
- **Sending as photo:** Telegram compresses images automatically
- **Sending as document:** Attach an image file for lossless quality
- **Size limit:** 20 MB (Telegram bot API constraint)

Images are validated (magic byte verification, size check) and stored temporarily during processing, then deleted immediately after Claude responds.

### Files

You can send documents (PDF, text files, CSV, code files, etc.) to Claudio. Include an optional caption to tell Claude what to do with the file, or send it without a caption and Claude will read and summarize it.

- **Any file type** supported by Telegram's document upload (files with image MIME types are routed through the image pipeline instead)
- **Size limit:** 20 MB (Telegram bot API constraint)
- Claude reads the file directly from disk — text-based formats (PDF, CSV, code, plain text) work best; binary files may produce limited results
- Files are stored temporarily during processing, then deleted immediately after Claude responds

### Parallel Agents

Claudio can spawn independent `claude -p` subprocesses for parallel work (reviews, research, etc.).

**How it works:**

- `agent_spawn` launches each agent in an independent process session (`setsid` on Linux, `POSIX::setsid` via perl on macOS). The agent runs `claude -p` with the given prompt and writes its output to SQLite when done.
- `agent_wait` polls the database every N seconds, sending Telegram typing indicators while agents work. It also enforces timeouts and detects dead processes.
- `agent_get_results` returns a JSON array with all agent outputs for the parent invocation.
- Each agent gets a unique ID, tracked in an `agents` table with status lifecycle: `pending` → `running` → `completed`/`failed`/`timeout`/`orphaned`.

**Crash recovery:** If the main process dies while agents are running, the agents keep going (they're in their own session). On the next webhook, `agent_recover` detects orphaned agents (PIDs that no longer exist), recovers any output files they wrote before dying, and injects unreported results into the conversation context.

**Usage from Claude's perspective:**

```bash
PARENT_ID="review_$(date +%s)"
agent_spawn "$PARENT_ID" "Review for security issues: ..." "haiku" 300
agent_spawn "$PARENT_ID" "Review for code quality: ..." "haiku" 300
agent_spawn "$PARENT_ID" "Review documentation: ..." "haiku" 300
agent_wait "$PARENT_ID" 5 "$TELEGRAM_CHAT_ID"
agent_get_results "$PARENT_ID"
```

### Backup

Claudio can automatically back up its `$HOME/.claudio/` directory using rsync-based rotating backups.

```bash
# One-off backup
claudio backup /path/to/destination

# Install hourly cron job (default: 24 hourly, 7 daily snapshots)
claudio backup cron install /path/to/destination

# Custom retention
claudio backup cron install /path/to/destination --hours 48 --days 14

# Check backup status
claudio backup status /path/to/destination

# Remove cron job
claudio backup cron uninstall
```

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
- `IS_SANDBOX` — Set to `1` to allow Claude Code to run in sandbox mode. Automatically enabled when running as root. Optional for non-root users.

**Telegram**

- `TELEGRAM_BOT_TOKEN` — Telegram Bot API token. Set automatically during `claudio telegram setup`.
- `TELEGRAM_CHAT_ID` — Authorized Telegram chat ID. Only messages from this chat are processed. Set automatically during `claudio telegram setup`.
- `WEBHOOK_URL` — Public URL where Telegram sends webhook updates (e.g. `https://claudio.example.com`). Set automatically when using a named tunnel.
- `WEBHOOK_SECRET` — HMAC secret for validating incoming webhook requests. Auto-generated on first run if not set.
- `WEBHOOK_RETRY_DELAY` — Seconds between webhook registration retry attempts. Default: `60`.

**Voice (TTS/STT)**

- `ELEVENLABS_API_KEY` — API key for ElevenLabs. Required for voice messages (both TTS and STT).
- `ELEVENLABS_VOICE_ID` — ElevenLabs voice ID to use for TTS. Default: `iP95p4xoKVk53GoZ742B` (Chris).
- `ELEVENLABS_MODEL` — ElevenLabs TTS model. Default: `eleven_multilingual_v2`.
- `ELEVENLABS_STT_MODEL` — ElevenLabs STT model. Default: `scribe_v1`.

**Agents**

- `AGENT_MAX_CONCURRENT` — Max parallel agents per parent invocation. Default: `5`.
- `AGENT_MAX_GLOBAL_CONCURRENT` — Max total agents system-wide across all parents. Prevents DoS via many parent IDs. Default: `15`.
- `AGENT_DEFAULT_TIMEOUT` — Per-agent timeout in seconds (capped at 3600). Default: `300`.
- `AGENT_DEFAULT_MODEL` — Default Claude model for agents. Must be `haiku`, `sonnet`, or `opus`. Default: `haiku`.
- `AGENT_POLL_INTERVAL` — Seconds between poll cycles in `agent_wait`. Default: `5`.
- `AGENT_CLEANUP_AGE` — Hours before old agent records are deleted. Default: `24`.
- `AGENT_MAX_OUTPUT_BYTES` — Max bytes of agent output stored; larger outputs are truncated. Default: `524288` (512 KB).
- `AGENT_MAX_CONTEXT_BYTES` — Max bytes of recovered agent context injected into prompts. Default: `262144` (256 KB).

**Memory**

- `MEMORY_ENABLED` — Enable/disable the cognitive memory system. Default: `1`.
- `MEMORY_EMBEDDING_MODEL` — Sentence-transformers model for memory embeddings. Default: `sentence-transformers/all-MiniLM-L6-v2`.
- `MEMORY_CONSOLIDATION_MODEL` — Claude model used for memory consolidation. Default: `haiku`.

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
- [x] Image uploads
- [x] Voice messages from bot (TTS)
- [x] Voice messages from human (STT)
- [x] File uploads
- [x] Parallel agents (independent Claude processes with crash recovery)
- [x] Cognitive memory system (ACT-R activation scoring, embedding-based retrieval)
- [x] Automated backup system (hourly/daily rotating backups with rsync)

**Future**

- [ ] Support for editing messages and reactions
- [ ] Rate limiting
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
