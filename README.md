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

Claudio starts a local HTTP server that listens on port 8421, and creates a tunnel using [cloudflared](https://github.com/cloudflare/cloudflared). When a user sends a message from Telegram, it's sent to `<cloudflare-tunnel-url>/telegram/webhook` and forwarded to the Claude Code CLI.

Claudio supports **multiple bots**: each bot has its own Telegram token, chat ID, webhook secret, conversation history, and configuration. Incoming webhooks are matched to bots via HMAC secret-token header matching, and each bot maintains independent conversation context.

User messages are passed as one-shot prompts, along with conversation context to maintain continuity. All messages are stored in a per-bot SQLite database, with the last 100 used as conversation context (configurable via `MAX_HISTORY_LINES`).

After Claude Code finishes, it outputs a JSON response to stdout. Claudio parses the result text and token usage stats, then forwards the response to the Telegram API.

The HTTP server includes several reliability mechanisms:

- **Message queue** — Incoming webhooks are queued per-chat (max 5) and processed serially, so multiple rapid messages don't spawn overlapping Claude sessions.
- **Deduplication** — Telegram may retry webhook deliveries; Claudio tracks the last 1,000 `update_id`s to silently drop duplicates.
- **Body size limit** — Requests larger than 1 MB are rejected to prevent abuse.
- **Graceful shutdown** — On `SIGTERM`, the server stops accepting new requests and waits for active handlers to finish before exiting.
- **Fallback model** — If Claude fails with the selected model, it automatically retries with Haiku as a fallback (unless Haiku is already selected).

---

## Installation

### **CAUTION: Security Risk**

The whole purpose of Claudio is to give you remote access to Claude Code from your Telegram app — without breaking Anthropic's Terms of Service. This means Claude Code runs with full permissions by design:

**⚠️ CLAUDE CODE RUNS WITH ALL TOOLS AUTO-APPROVED (`--tools` + `--allowedTools`) AND `--disable-slash-commands`**

Since there's no human in front of the terminal to approve permission prompts, Claude Code must run autonomously. Claudio explicitly whitelists allowed tools via `--allowedTools` (safer than `--dangerously-skip-permissions`) and auto-approves them with `--tools` — excluding interactive-only tools like `AskUserQuestion` and `Chrome`. Claudio mitigates the risk through: webhook secret validation (HMAC), single authorized `chat_id` per bot, and binding the HTTP server to localhost only (external access goes through cloudflared).

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
- Configure the default bot (named "claudio")

> **Note:** If `~/.local/bin` is not in your PATH, you'll need to add it. See the installation output for instructions.

> **Multi-bot support:** To configure additional bots, run `claudio install <bot_id>` with a unique bot identifier (alphanumeric, hyphens, and underscores only). Each bot will have its own Telegram credentials, conversation history, and configuration stored in `~/.claudio/bots/<bot_id>/`.

2. Set up Telegram bot credentials

The install wizard will guide you through Telegram bot setup. If you skipped it or need to reconfigure, in Telegram, message `@BotFather` with `/newbot` and follow instructions to create a bot. At the end, you'll be given a secret token.

For the default bot (or when reconfiguring an existing bot), run:

```bash
claudio telegram setup
```

For additional bots, use `claudio install <bot_id>` which will interactively configure the new bot's Telegram credentials.

Paste your Telegram token when asked, and press Enter. Then, send a `/start` message to your bot from the Telegram account that you'll use to communicate with Claude Code.

The setup wizard will confirm when it receives the message and finish. Once done, the service restarts automatically, and you can start chatting with Claude Code.

> For security, only the `chat_id` captured during setup is authorized to send messages.

> A cron job runs every minute to monitor the webhook endpoint. It verifies the webhook is registered and re-registers it if needed. If the server is unreachable, it auto-restarts the service (throttled to once per 3 minutes, max 3 attempts). After exhausting restart attempts without recovery, it sends a Telegram alert and stops retrying until the server responds with HTTP 200. The restart counter auto-clears when the health endpoint returns HTTP 200. You can also reset it manually by deleting `$HOME/.claudio/.last_restart_attempt` and `$HOME/.claudio/.restart_fail_count`.
>
> The health check also monitors: disk usage (alerts above 90%), log file sizes (rotates files over 10MB), backup freshness (alerts if the last backup is older than 2 hours), and recent log analysis (detects errors, restart loops, and slow API responses — sends Telegram alerts with a configurable cooldown). These thresholds are configurable via environment variables.

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

To remove a specific bot's configuration:

```bash
claudio uninstall <bot_id>
```

This will prompt for confirmation, then delete the bot's directory (`~/.claudio/bots/<bot_id>/`) including its credentials, conversation history, and configuration. The service will restart to reload the bot registry.

To stop the service and remove **all** bots and configuration:

```bash
claudio uninstall --purge
```

This removes the `$HOME/.claudio` directory completely, stops the systemd/launchd service, removes the cron health check, uninstalls Claude Code hooks, and removes the `~/.local/bin/claudio` symlink.

> On Linux, uninstall disables loginctl linger if no other user services remain enabled.

---

## Customization

### Model

Claudio uses Haiku by default. If you want to switch to another model, just send the name of the model as a command: `/opus`, `/sonnet`, or `/haiku`. The model preference is saved per-bot and persists across restarts.

### Voice

Claudio supports voice messages in both directions using ElevenLabs. Set `ELEVENLABS_API_KEY` in your `service.env` to enable voice features (`ELEVENLABS_VOICE_ID` defaults to Chris if not set). When you send a voice message, Claudio transcribes it (STT), processes it, and responds with a voice message (as a reply). If TTS fails, it falls back to a text-only response. Text messages always get text-only responses. If `ELEVENLABS_API_KEY` is not set, voice messages will be rejected with an error reply.

### Images

You can send photos or image files directly to Claudio. Include an optional caption to tell Claude what to do with the image, or send it without a caption and Claude will describe it.

- **Supported formats:** JPEG, PNG, GIF, WebP
- **Albums:** When you send multiple photos at once (media group), Claudio buffers them and passes all images to Claude as a single multi-image prompt
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

### Alexa

> **⚠️ This integration is optional and carries additional security risks.** The Alexa skill exposes an additional `/alexa` endpoint that accepts voice commands and relays them to Claude Code via Telegram. This carries a *higher security risk* than the Telegram-only setup because: (1) anyone with physical access to your Alexa device can send commands to Claude Code — there is no per-user authentication beyond Amazon's skill ID validation, and (2) unlike Telegram (which binds to a single `chat_id`), the Alexa endpoint relies on the skill remaining private (unpublished) to limit access. Both `cryptography` and `ALEXA_SKILL_ID` are required — the endpoint is disabled without them. **Do not enable Alexa integration unless you understand these risks.**

Claudio can receive voice commands through an Amazon Alexa skill. When you speak to Alexa, the message is relayed to Claude Code via the same Telegram pipeline — Claude's response appears in your Telegram chat.

**How it works:**

1. You say: _"Alexa, open Claudio"_ → Alexa opens the skill
2. You say your message → Alexa sends it to the `/alexa` endpoint
3. Claudio relays it to Claude Code as a synthetic Telegram message
4. Claude's response appears in your Telegram chat
5. Alexa asks _"Anything else?"_ — you can send another message or say _"No"_ to end

**Setup:**

1. Install the `cryptography` Python library (required for signature verification):

```bash
pip3 install cryptography
```

2. Create a custom Alexa skill at [developer.amazon.com](https://developer.amazon.com/alexa/console/ask):
   - Invocation name: `claudio` (or your preferred name)
   - Endpoint: HTTPS, URL: `https://<your-tunnel-hostname>/alexa`
   - SSL certificate type: _"My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority"_
   - Create a custom intent `SendMessageIntent` with a slot `message` of type `AMAZON.SearchQuery`
   - Add sample utterances (Spanish):
     ```
     dile {message}
     dile que {message}
     dile a claudio {message}
     dile a claudio que {message}
     que {message}
     y {message}
     también {message}
     y también {message}
     pregúntale {message}
     pregúntale que {message}
     pregúntale a claudio {message}
     luego {message}
     luego que {message}
     pero {message}
     además {message}
     aparte {message}
     manda {message}
     pásale {message}
     por favor dile {message}
     dile por favor {message}
     ```
   - Add sample utterances (English):
     ```
     tell him {message}
     tell him that {message}
     tell claudio {message}
     tell claudio that {message}
     and {message}
     also {message}
     and also {message}
     ask him {message}
     ask him about {message}
     ask claudio {message}
     ask claudio about {message}
     then {message}
     but {message}
     also ask {message}
     send {message}
     pass along {message}
     please tell him {message}
     tell him please {message}
     ```
   - **Note:** `AMAZON.SearchQuery` slots require a carrier phrase — the slot cannot be the only word in the utterance, and it must appear at the end. For best practices on designing and testing utterances, see the official Alexa documentation: https://developer.amazon.com/en-US/docs/alexa/custom-skills/best-practices-for-sample-utterances-and-custom-slot-type-values.html and https://developer.amazon.com/en-US/docs/alexa/custom-skills/test-utterances-and-improve-your-interaction-model.html
   - Enable built-in intents: `AMAZON.CancelIntent`, `AMAZON.StopIntent`, `AMAZON.HelpIntent`, `AMAZON.FallbackIntent`, `AMAZON.NoIntent`

3. Copy the skill ID and add it to your config:

```bash
echo 'ALEXA_SKILL_ID="amzn1.ask.skill.YOUR-SKILL-ID"' >> ~/.claudio/service.env
claudio restart
```

4. Keep the skill in **development mode** (do not publish it) to restrict access to your Amazon account only.

**Security considerations:**

- `cryptography` and `ALEXA_SKILL_ID` are both required — without them, the Alexa endpoint is disabled
- Anyone with physical access to your Alexa device can send commands — there is no voice PIN or per-user auth
- Alexa messages appear in Telegram prefixed with `[Alexa voice query]` so you can distinguish the source

### Parallel Work

Parallel work (reviews, research, etc.) is handled by Claude Code's built-in Task tool (subagents). No custom agent infrastructure is needed — Claude natively spawns subagents, manages their lifecycle, and collects results within a single `claude -p` invocation.

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

To customize Claude's behavior globally, use `~/.claude/CLAUDE.md` (Claude Code's built-in configuration file). Instructions there are loaded automatically by Claude Code on every invocation and persist across updates.

For **per-bot customization**, create `~/.claudio/bots/<bot_id>/CLAUDE.md`. When a bot is loaded, Claudio will append the bot-specific CLAUDE.md (if it exists) to the global system prompt.

### Configuration

Claudio stores its configuration and other files in `$HOME/.claudio/`. Configuration is split into:

- **Global settings** (`$HOME/.claudio/service.env`) — Applies to all bots (server port, tunnel config, global feature flags).
- **Per-bot settings** (`$HOME/.claudio/bots/<bot_id>/bot.env`) — Bot-specific credentials, model preference, and conversation history.

After any manual edits, restart to apply your changes:

```bash
claudio restart
```

#### Environment Variables

**Global variables** (stored in `$HOME/.claudio/service.env`):

**Server**

- `PORT` — HTTP server listening port. Default: `8421`.

**Tunnel**

- `TUNNEL_NAME` — Name of the Cloudflare named tunnel. Set during `claudio install`.
- `TUNNEL_HOSTNAME` — Hostname for the named tunnel (e.g. `claudio.example.com`). Set during `claudio install`.
- `WEBHOOK_URL` — Public URL where Telegram sends webhook updates (e.g. `https://claudio.example.com`). Set automatically when using a named tunnel.
- `WEBHOOK_RETRY_DELAY` — Seconds between webhook registration retry attempts. Default: `60`.

**Alexa (Optional)**

- `ALEXA_SKILL_ID` — Amazon Alexa skill application ID. Required to enable the Alexa endpoint.

**Voice (TTS/STT)**

- `ELEVENLABS_API_KEY` — API key for ElevenLabs. Required for voice messages (both TTS and STT).
- `ELEVENLABS_VOICE_ID` — ElevenLabs voice ID to use for TTS. Default: `iP95p4xoKVk53GoZ742B` (Chris).
- `ELEVENLABS_MODEL` — ElevenLabs TTS model. Default: `eleven_multilingual_v2`.
- `ELEVENLABS_STT_MODEL` — ElevenLabs STT model. Default: `scribe_v1`.

**Memory**

- `MEMORY_ENABLED` — Enable/disable the cognitive memory system. Default: `1`.
- `MEMORY_EMBEDDING_MODEL` — Sentence-transformers model for memory embeddings. Default: `sentence-transformers/all-MiniLM-L6-v2`.
- `MEMORY_CONSOLIDATION_MODEL` — Claude model used for memory consolidation. Default: `haiku`.

**Health Check**

- `DISK_USAGE_THRESHOLD` — Disk usage percentage to trigger alerts. Default: `90`.
- `LOG_MAX_SIZE` — Maximum log file size in bytes before rotation. Default: `10485760` (10 MB).
- `BACKUP_MAX_AGE` — Maximum backup age in seconds before alerting. Default: `7200` (2 hours).
- `BACKUP_DEST` — Backup destination path for freshness checks. Default: `/mnt/ssd` (customize to your backup location).
- `LOG_CHECK_WINDOW` — Seconds of recent log history to scan for errors and anomalies. Default: `300` (5 minutes).
- `LOG_ALERT_COOLDOWN` — Minimum seconds between log-analysis alert notifications. Default: `1800` (30 minutes).

---

**Per-bot variables** (stored in `$HOME/.claudio/bots/<bot_id>/bot.env`):

**Telegram**

- `TELEGRAM_BOT_TOKEN` — Telegram Bot API token. Set automatically during `claudio telegram setup`.
- `TELEGRAM_CHAT_ID` — Authorized Telegram chat ID. Only messages from this chat are processed. Set automatically during `claudio telegram setup`.
- `WEBHOOK_SECRET` — HMAC secret for validating incoming webhook requests. Auto-generated during bot setup.

**Claude**

- `MODEL` — Claude model to use for this bot. Accepts `haiku`, `sonnet`, or `opus`. Default: `haiku`. Can also be changed at runtime via Telegram commands `/haiku`, `/sonnet`, `/opus`.
- `MAX_HISTORY_LINES` — Number of recent messages used as conversation context for this bot. Default: `100`.

---

> Most global variables are configured automatically by `claudio install`. Per-bot variables are set during bot setup (`claudio install <bot_id>` or `claudio telegram setup`). Manual editing is only needed for fine-tuning.

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
- [x] Parallel work via Claude Code's built-in Task tool (subagents)
- [x] Cognitive memory system (ACT-R activation scoring, embedding-based retrieval)
- [x] Automated backup system (hourly/daily rotating backups with rsync)
- [x] Alexa skill integration (optional voice-to-Telegram relay)
- [x] MCP tools for Telegram notifications and service restart
- [x] Media group (album) support for multi-photo messages
- [x] Tool usage capture in conversation history (PostToolUse hook)
- [x] Health check log analysis (error detection, restart loops, API slowness)
- [x] Claude code review for Pull Requests (GitHub Actions)

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
