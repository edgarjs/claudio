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

---

## Overview

Claudio starts a local HTTP server that listens on port 8421, and creates a tunnel using [cloudflared](https://github.com/cloudflare/cloudflared). When the user sends a message from Telegram, it's sent to `<cloudflare-tunnel-url>/telegram/webhook` and forwarded it to the Claude Code CLI.

The user message is passed as a one-shot prompt, along with some context to maintain continuity. Only the last five messages of the conversation are injected in the context to keep it small.

After Claude Code finishes, it outputs the response in plain text to stdout for Claudio to capture it and forward it to Telegram API.

---

## Installation

### **CAUTION: Security Risk**

As you should know already, Claude Code has direct access to your machine terminal and filesystem. Beware this will expose it to your Telegram account.

**⚠️ CLAUDE CODE IS EXECUTED WITH DANGEROUSLY PERMISSIONS ENABLED**

### Requirements

- Claude Code CLI (with Pro/Max subscription)
- Cloudflare account (free)
- Linux/MacOS/WSL
- Telegram bot token

### Setup

1. Download the latest binary for your machine from the [releases page](https://github.com/edgarjs/claudio/releases), and add it to your `PATH`. Then run:

```bash
claudio install
```

This will install and start a systemd/launchd service so that the server is started with the machine.

2. Create Cloudflare tunnel.

You have two options: A temporal tunnel that doesn're require an account, but doesn't provide a permanent URL so you'll have to re-configure it again when it changes. Or a permanent tunel with your own domain.

Install and follow [cloudflared](https://github.com/cloudflare/cloudflared) instructions for the option that suits you.

3. Set Telegram Webhook

In Telegram, message `@BotFather` with `/newbot` and follow instructions to create a bot. At the end, you'll be given a secret token, copy it and then run:

```bash
claudio telegram setup
```

Paste your Telegram token when asked, and press Enter. Then, send a `/start` message to your bot from the Telegram account that you'll use to communicate with Claude Code.

The setup wizard will confirm when it receives the webhook notification and finish. If you've sent the start message but it's not being received, wait a little bit more for the Cloudflare tunnel DNS to propagate and try again.

Once the setup has finished, it'll restart the service automatically, and now you can start chatting with Claude Code.

> For security, only one account is allowed to send messages by default.

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
