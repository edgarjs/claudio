# Changelog

## [1.1.0] - 2026-02-04

### Added

- Voice mode: `/voice` command toggles TTS audio responses using ElevenLabs API
- Image upload support: send photos (compressed) or image documents (lossless) to the bot for Claude analysis
- Per-chat message queue size limit (100) to prevent unbounded memory growth
- Queue capacity warning when reaching 80% per chat
- Typing indicator self-terminates if parent process is killed (prevents orphan processes)

### Changed

- Threaded HTTP server to prevent health checks from blocking during long webhook processing
- Health check caching (30s TTL); only healthy results are cached so recovery is detected immediately
- Switched conversation history from TSV parsing to SQLite JSON mode + jq (fixes multiline content)
- Use unit separator delimiter for webhook field parsing (fixes empty field collapse)

### Fixed

- Multiline message content breaking conversation history retrieval
- SIGTERM deadlock by using `threading.Event` instead of direct `server.shutdown()` in signal handler
- Literal `\n` in prompt concatenation (use `printf -v`)
- `echo` to `printf` in log module to prevent flag misinterpretation
- systemd `EnvironmentFile` compatibility (use double-quoted format instead of `printf %q`)
- Subprocess proc reference guard in timeout handler
- HOME fallback uses tilde expansion instead of hardcoded `/root`

### Security

- Image file path whitelist regex (path traversal prevention)
- Magic byte validation for uploaded images (JPEG, PNG, GIF, WebP)
- 20 MB file size limit with cleanup on validation failure
- `curl --max-redirs 0` for image downloads (redirect attack prevention)
- `chmod 600` on downloaded image files
- Chat ID validation in Python server (defense in depth)

## [1.0.0] - 2026-02-04

### Added

- `claudio log` command with `-f` (follow) and `-n` (line count) options
- Environment variables documentation in README

### Changed

- Set `IS_SANDBOX=1` in service env by default
- Remove unused `TUNNEL_TYPE` variable; tunnel mode is now determined by `TUNNEL_NAME`

### Fixed

- Claude CLI command resolution in webhook handler and cron job
- PATH and environment variables in systemd unit for claude CLI
- Call `init` before `install` to ensure dependencies are available

### Security

- Stop logging conversation message content

## [1.0.0-beta] - 2026-02-03

### Added

- Telegram bot integration with webhook support
- Claude Code CLI integration for processing messages
- Cloudflare named tunnel support for secure webhook delivery
- Conversation history with SQLite storage
- System service management (launchd on macOS, systemd on Linux)
- Auto-install dependencies (sqlite3, jq, cloudflared)
- Health check endpoint with automatic webhook re-registration
- Cron-based health monitoring (every 5 minutes)
- Model switching via Telegram commands (/opus, /sonnet, /haiku)
- Progress feedback for long-running requests
- Rate limit handling with exponential backoff
- `claudio status` command for service and webhook status
- `claudio update` command for easy updates via git pull
- PATH integration via symlink to ~/.local/bin

### Security

- Webhook secret token validation
- Chat ID restriction to authorized users only
- Constant-time comparison for webhook authentication
