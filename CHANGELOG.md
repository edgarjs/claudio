# Changelog

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
