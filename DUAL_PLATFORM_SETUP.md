# Dual-Platform Bot Setup

Claudio now supports running **both Telegram and WhatsApp** on the same bot, allowing you to receive messages from both platforms in a unified conversation history.

## Setup Options

When running `claudio install <bot_name>`, you'll see:

```
=== Setting up bot: mybot ===

Which platform(s) do you want to configure?
  1) Telegram only
  2) WhatsApp Business API only
  3) Both Telegram and WhatsApp
```

### Option 1: Telegram Only
Sets up Telegram credentials. After completion, you'll be asked:
```
Would you like to also configure WhatsApp for this bot? [y/N]
```

### Option 2: WhatsApp Only
Sets up WhatsApp credentials. After completion, you'll be asked:
```
Would you like to also configure Telegram for this bot? [y/N]
```

### Option 3: Both Platforms
Walks through Telegram setup first, then WhatsApp setup.

## Re-configuring Existing Bots

If a bot already has one platform configured, the wizard shows:

```
=== Setting up bot: mybot ===

Current configuration:
  ✓ Telegram configured

Which platform(s) do you want to configure?
  1) Telegram only
  2) WhatsApp Business API only
  3) Both Telegram and WhatsApp
  4) Re-configure Telegram
  5) Re-configure WhatsApp
```

Options 4 and 5 allow updating credentials without losing the other platform's config.

## How Dual-Platform Works

### Shared Bot Configuration
A single bot with both platforms has:
- **Shared**: `bot.env` file, conversation history, CLAUDE.md instructions, model preference
- **Separate**: API credentials, authorized users (chat_id vs phone_number)

### Example bot.env for Dual-Platform Bot
```bash
# Telegram configuration
TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
TELEGRAM_CHAT_ID="987654321"
WEBHOOK_SECRET="abc123..."

# WhatsApp configuration
WHATSAPP_PHONE_NUMBER_ID="1234567890"
WHATSAPP_ACCESS_TOKEN="EAAabc..."
WHATSAPP_APP_SECRET="def456..."
WHATSAPP_VERIFY_TOKEN="xyz789..."
WHATSAPP_PHONE_NUMBER="1234567890"

# Shared configuration
MODEL="sonnet"
MAX_HISTORY_LINES="100"
```

### Server Loading
The server loads each bot and registers it in both platforms if credentials exist:

```python
# Bot "mybot" with both platforms gets registered in both registries:
bots = {
    "mybot": {
        "type": "telegram",
        "token": "...",
        "chat_id": "...",
        "secret": "...",
        "bot_dir": "~/.claudio/bots/mybot"
    }
}

whatsapp_bots = {
    "mybot": {
        "type": "whatsapp",
        "phone_number_id": "...",
        "access_token": "...",
        "app_secret": "...",
        "verify_token": "...",
        "bot_dir": "~/.claudio/bots/mybot"
    }
}
```

Server output shows:
```
[bots] Loaded 1 bot(s): 2 Telegram endpoint(s), 2 WhatsApp endpoint(s)
```

This means 1 unique bot with 2 total endpoints (one per platform).

### Conversation History
Messages from **both platforms** are stored in the same `history.db`:

```
User (via Telegram): "Show me the status"
Assistant: "Everything is running smoothly!"
User (via WhatsApp): "What about the logs?"
Assistant: "The logs show no errors since your last check on Telegram."
```

The assistant remembers context across platforms!

### Webhook Routing
The server routes incoming webhooks based on authentication:

```
POST /telegram/webhook
  → Matches bot by X-Telegram-Bot-Api-Secret-Token
  → Routes to mybot (Telegram mode)
  → Loads TELEGRAM_* variables

POST /whatsapp/webhook
  → Matches bot by X-Hub-Signature-256 HMAC
  → Routes to mybot (WhatsApp mode)
  → Loads WHATSAPP_* variables
```

Both share the same bot directory and conversation history.

## Use Cases

### Personal Assistant on Both Platforms
```bash
./claudio install assistant
# Choose option 3 (Both)
# Configure Telegram with your personal chat
# Configure WhatsApp with your personal number
```

Now you can message your assistant from either platform and maintain continuous conversation.

### Team Bot with Mixed Preferences
```bash
./claudio install team-bot
# Choose option 3 (Both)
# Configure Telegram for @team_bot
# Configure WhatsApp for team phone number
```

Team members can use whichever platform they prefer.

### Migration from Telegram to WhatsApp
```bash
# Existing Telegram bot
./claudio status  # Shows telegram-bot active

# Add WhatsApp without disrupting Telegram
./claudio install telegram-bot
# Choose option 2 (WhatsApp only)
# Answer Y to "also configure WhatsApp"

# Now handles both, gradually migrate users
```

### Platform-Specific Bots
You can also run separate bots per platform:

```bash
./claudio install telegram-bot
# Choose option 1 (Telegram only)

./claudio install whatsapp-bot
# Choose option 2 (WhatsApp only)
```

Each has isolated configuration and history.

## Advanced: Adding Platform to Existing Bot

### Via Install Command
```bash
./claudio install existing-bot
# Shows: "Current configuration: ✓ Telegram configured"
# Choose option 2 (WhatsApp only)
# Answer Y to "also configure Telegram"
```

### Via Direct Setup Command
```bash
# Add WhatsApp to existing Telegram bot (default bot)
./claudio whatsapp setup

# Or add Telegram to existing WhatsApp bot (default bot)
./claudio telegram setup

# For named bots, use the install command with the bot_id:
./claudio install existing-bot
# Then choose the platform to add
```

Both methods preserve existing credentials.

## Credential Updates

When updating credentials, existing platform config is preserved:

```bash
# Update Telegram token without touching WhatsApp
./claudio telegram setup mybot
# Existing WhatsApp credentials remain intact

# Update WhatsApp access token without touching Telegram
./claudio whatsapp setup mybot
# Existing Telegram credentials remain intact
```

## Status Command

The status command shows all configured endpoints:

```bash
./claudio status

Service Status: active (running)

Configured bots:
  mybot
    - Telegram: ✓ (chat_id: 123456789)
    - WhatsApp: ✓ (phone: +1234567890)

  telegram-only
    - Telegram: ✓ (chat_id: 987654321)

  whatsapp-only
    - WhatsApp: ✓ (phone: +9876543210)

Webhooks:
  Telegram: https://your-tunnel.trycloudflare.com/telegram/webhook
  WhatsApp: https://your-tunnel.trycloudflare.com/whatsapp/webhook
```

## Troubleshooting

### "Bot already exists" Warning
This is normal when adding a second platform to an existing bot. Choose the re-configure option to proceed.

### Webhook Conflicts
Each platform needs its own webhook:
- Telegram: Configure in BotFather
- WhatsApp: Configure in Meta for Developers

They use different URLs (`/telegram/webhook` vs `/whatsapp/webhook`) so there's no conflict.

### History Not Shared
If conversation history seems separate:
1. Check both platforms are using the same `bot_id`
2. Verify `bot.env` has both sets of credentials
3. Check `~/.claudio/bots/<bot_id>/history.db` exists
4. Restart service: `./claudio restart`

### Model Settings Not Syncing
Model preference is shared - when you change it via `/sonnet` on Telegram, it also affects WhatsApp messages. If this isn't working:
1. Check `bot.env` has both platform credentials
2. Verify `MODEL="..."` is in the shared section (not duplicated)
3. Restart service after manual edits

## Best Practices

1. **Use Dual-Platform for Personal Bots**: Gives you flexibility to use either platform
2. **Separate Bots for Different Users**: If Telegram user A and WhatsApp user B need different bot configs
3. **Test Both Endpoints**: After setup, send test message on both platforms to verify routing
4. **Monitor Logs**: `./claudio log -f` shows which platform each message comes from
5. **Document Your Setup**: Keep notes on which phone numbers/chat IDs are authorized

## Migration Path

### From Single-Platform to Dual-Platform
```bash
# Day 1: Telegram only
./claudio install mybot  # Choose Telegram

# Day 30: Add WhatsApp support
./claudio install mybot
# Shows current Telegram config
# Choose "WhatsApp only"
# Answer Y to also configure

# Conversation history preserved!
```

### From Dual-Platform to Single-Platform
Edit `~/.claudio/bots/<bot_id>/bot.env` and remove one platform's credentials:

```bash
# Keep only Telegram
nano ~/.claudio/bots/mybot/bot.env
# Delete WHATSAPP_* lines, save

./claudio restart
```

Or completely remove a bot:
```bash
./claudio uninstall mybot
```

## Technical Details

### Webhook Authentication
- **Telegram**: Secret token in `X-Telegram-Bot-Api-Secret-Token` header
- **WhatsApp**: HMAC-SHA256 signature in `X-Hub-Signature-256` header

Both are validated before routing to the webhook handler.

### Queue Isolation
Messages are queued per bot per user:
- Telegram: `bot_id:chat_id`
- WhatsApp: `bot_id:phone_number`

This ensures serial processing while allowing concurrent handling of different users.

### Config Preservation
When running setup for one platform, the system:
1. Loads existing `bot.env` (if exists)
2. Updates only the target platform's variables
3. Writes back ALL credentials (preserving other platform)

This is why you can safely run `telegram setup` without losing WhatsApp config.
