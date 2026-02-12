# WhatsApp Business API Integration - Feature Comparison

## Complete Feature Parity with Telegram

| Feature | Telegram | WhatsApp | Notes |
|---------|----------|----------|-------|
| **Text Messages** | ✅ | ✅ | Full support with 4096 char chunking |
| **Single Images** | ✅ | ✅ | Downloads and passes to Claude |
| **Multiple Images** | ✅ (media groups) | ✅ (separate messages) | WhatsApp doesn't batch images like Telegram, but each is processed |
| **Image Captions** | ✅ | ✅ | Full support |
| **Documents** | ✅ | ✅ | Full support with mime type detection |
| **Voice Messages** | ✅ | ✅ | Transcription via ElevenLabs STT |
| **Audio Messages** | ✅ | ✅ | Same as voice messages |
| **Voice Responses** | ✅ | ✅ | TTS via ElevenLabs when user sends audio |
| **Reply Context** | ✅ (fetches original) | ✅ (notes it's a reply) | WhatsApp API doesn't provide original text |
| **Commands** | ✅ | ✅ | `/opus`, `/sonnet`, `/haiku`, `/start` |
| **Model Switching** | ✅ | ✅ | Persisted per-bot |
| **Typing Indicators** | ✅ | ⚠️ | WhatsApp: sends "..." text message (no native typing API) |
| **Recording Indicator** | ✅ | ⚠️ | WhatsApp: sends "..." text message (same as typing) |
| **Read Receipts** | ✅ (👀 reaction) | ✅ (mark as read) | Different APIs, same purpose |
| **Conversation History** | ✅ | ✅ | SQLite per-bot |
| **Memory System** | ✅ | ✅ | Full ACT-R cognitive memory |
| **Multi-Bot Support** | ✅ | ✅ | Unlimited bots per platform |
| **Per-Bot Config** | ✅ | ✅ | Separate `bot.env` files |
| **Per-Bot CLAUDE.md** | ✅ | ✅ | Custom instructions per bot |
| **Tool Summaries** | ✅ | ✅ | Appended to history |
| **Notifier Messages** | ✅ | ✅ | MCP tool notifications |
| **Text Chunking** | ✅ | ✅ | Long responses split automatically |
| **Security** | ✅ | ✅ | Secret token / HMAC-SHA256 signature |

## WhatsApp-Specific Implementation Details

### Authentication
- **Webhook Verification**: GET request with `hub.verify_token` challenge
- **Message Verification**: HMAC-SHA256 signature in `X-Hub-Signature-256` header
- **Per-Bot Secrets**: Each bot has unique verify token and app secret

### API Endpoints
- **Messages**: `https://graph.facebook.com/v21.0/{phone_number_id}/messages`
- **Media**: `https://graph.facebook.com/v21.0/{media_id}` (two-step download)
- **Upload**: `https://graph.facebook.com/v21.0/{phone_number_id}/media`

### Message Format
WhatsApp uses a different JSON structure:
```json
{
  "entry": [{
    "changes": [{
      "value": {
        "messages": [{
          "from": "1234567890",
          "id": "wamid.xxx",
          "type": "text",
          "text": { "body": "Hello" }
        }]
      }
    }]
  }]
}
```

### Media Handling
- Images: Direct download via media API
- Documents: Same as images
- Audio: OGG, MP3 support with magic byte validation
- Upload: Required for sending audio (TTS responses)

### Limitations vs Telegram
1. **No Media Groups**: WhatsApp Business API doesn't support receiving multiple images in a single webhook like Telegram's media groups. Each image arrives as a separate webhook and is processed individually.
2. **Reply Context**: Can detect replies (via `context.id` field) but WhatsApp API doesn't provide the original message text, only the message ID. Implementation adds `[Replying to a previous message]` prefix.
3. **No Built-in Markdown**: WhatsApp uses different formatting (bold: `*text*`, italic: `_text_`)
4. **16 MB Limit**: Smaller than Telegram's 20 MB
5. **No Native Typing Indicator**: WhatsApp Business API doesn't expose a typing indicator endpoint like Telegram. Implementation sends "..." as a text message as a workaround.

## Setup Process

### Interactive Wizard
When running `claudio install <bot_name>`, the wizard now asks:
```
Which platform do you want to use?
  1) Telegram
  2) WhatsApp Business API

Enter choice [1-2]:
```

### WhatsApp Setup Requirements
1. **Meta Business Account** with WhatsApp Business API access
2. **Phone Number ID** from Meta Business Suite
3. **Access Token** (permanent token, not temporary)
4. **App Secret** from Meta for Developers app settings
5. **Authorized Phone Number** (your personal WhatsApp number for testing)

### Webhook Configuration
After running setup, configure in Meta for Developers:
```
Callback URL: https://<your-tunnel-hostname>/whatsapp/webhook
  (e.g., https://claudio.example.com/whatsapp/webhook for named tunnels)
Verify Token: [provided by setup wizard]
Subscribe to: messages
```

## Architecture

### Multi-Platform Bot Loading
The server now loads both Telegram and WhatsApp bots on startup:

```python
bots = {}              # Telegram bots by bot_id
bots_by_secret = []    # Telegram dispatch by secret token
whatsapp_bots = {}     # WhatsApp bots by bot_id
whatsapp_bots_by_verify = []  # WhatsApp dispatch by verify token
```

### Webhook Routing
```
POST /telegram/webhook   → match by X-Telegram-Bot-Api-Secret-Token
GET  /whatsapp/webhook   → verify token challenge
POST /whatsapp/webhook   → match by X-Hub-Signature-256 HMAC
```

### Queue Isolation
- Telegram: `bot_id:chat_id`
- WhatsApp: `bot_id:phone_number`

Each queue ensures serial processing per user per bot.

## Testing

### Basic Flow
1. **Setup**: `./claudio install mybot` → Choose WhatsApp
2. **Configure**: Enter credentials, get verify token
3. **Register**: Add webhook in Meta for Developers
4. **Test**: Send "Hello" from authorized phone number
5. **Verify**: Check response and conversation history

### Feature Testing
- ✅ Text messages with replies
- ✅ Send image with caption
- ✅ Send document/PDF
- ✅ Send voice message (gets transcribed and responds with voice)
- ✅ Commands: `/sonnet`, `/haiku`, `/opus`
- ✅ Long responses (>4096 chars) split into chunks
- ✅ Multiple messages queued properly
- ✅ Model switching persists

## Migration from Telegram

Claudio now supports running both platforms simultaneously:

```bash
# Keep existing Telegram bot
./claudio status  # Shows "telegram-bot: active"

# Add WhatsApp bot
./claudio install whatsapp-bot
# Choose option 2 (WhatsApp)

# Both run in same service
./claudio status
# Shows:
#   telegram-bot: active (Telegram)
#   whatsapp-bot: active (WhatsApp)
```

## Troubleshooting

### Webhook Not Receiving Messages
1. Check webhook registration in Meta for Developers
2. Verify verify token matches bot.env
3. Check cloudflared tunnel is running: `ps aux | grep cloudflared`
4. Test verification endpoint: `curl https://<your-tunnel-hostname>/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=YOUR_TOKEN&hub.challenge=test`
   (Replace `<your-tunnel-hostname>` with your actual tunnel URL, e.g., `claudio.example.com`)

### Signature Verification Failed
1. Ensure `WHATSAPP_APP_SECRET` matches your Meta app
2. Check webhook is configured with correct app
3. Verify no proxy/CDN is modifying request body

### Media Download Fails
1. Verify `WHATSAPP_ACCESS_TOKEN` has proper permissions
2. Check token hasn't expired (use permanent token)
3. Ensure media under 16 MB limit

### Voice Response Not Working
1. Verify `ELEVENLABS_API_KEY` is configured
2. Check audio upload succeeded: `grep "Failed to upload audio" ~/.claudio/claudio.log`
3. Test TTS separately: `./claudio` → source lib/tts.sh → `tts_convert "test" /tmp/test.mp3`
