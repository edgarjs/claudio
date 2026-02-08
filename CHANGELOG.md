# Changelog

## [1.2.1] - 2026-02-08

### Added

- Persistent memory daemon to eliminate per-webhook ONNX cold-start latency (#40)
- Enrich no-caption image/document history with response summary (#42)
- `MAX_HISTORY_LINES` configurable variable restored (#34)

### Changed

- Replace `--dangerously-skip-permissions` with explicit tool allowlisting
- Replace PATH wrapper safeguards with Claude Code PreToolUse hook
- Remove custom subagents in favor of Claude Code's built-in Task tool (#38)
- Remove safeguard hook and `CLAUDIO_WEBHOOK_ACTIVE` env var
- Reduce `MAX_QUEUE_SIZE` from 100 to 5 (#43)

### Security

- Health check: validate response body, not just HTTP status (#44)
- TTS: verify ElevenLabs API response before writing audio file (#44)
- Service: harden launchd plist permissions and cloudflared install (#44)
- Server: atomic PID file writes with `os.replace` (#44)

### Fixed

- Memory retry loop, token tracking, and SQLite consistency (#33)

## [1.2.0] - 2026-02-07

### Added

- Cognitive memory system with ACT-R activation scoring, embedding-based retrieval, and LLM consolidation (#19)
- Automated backup management with rsync-based hourly/daily rotating backups
- loginctl linger enabled on install/update for headless systemd operation (#30)

### Changed

- Isolate Claude CLI in its own process group to prevent exit 143 (#26)
- Switch embedding model to sentence-transformers/all-MiniLM-L6-v2 and harden memory system (#27)
- Skip text follow-up when voice response succeeds (#29)

### Security

- Parameterized SQL queries, bot token hiding, auth enforcement, cloudflared lifecycle management (#28)
- Safe env file parsing — reject arbitrary code in service.env
- Pass prompt via stdin to avoid process list exposure
- Broader XML tag sanitization for prompt injection prevention
- Parameterized SQL in agent management (removed in favor of Claude Code's built-in Task tool)
- Hide bot token from `ps` in all curl commands
- Cap LLM deduplication calls, batch activation queries

### Fixed

- Memory system: missing embedding column in SELECT, stale lock recovery, batched re-embedding
- Server: close leaked cloudflared log fd, safe UTF-8 decode, monotonic clock for health cache
- Connection management in db.py (try/finally on all sqlite3.connect)
- Typing indicator capped at 15 minutes, openssl error handling, armv7l architecture support

## [1.1.1] - 2026-02-05

### Added

- Health check auto-restart: service automatically restarts when health check fails (max 3 attempts with 3-minute throttle)
- Telegram alert when service fails to recover after 3 restart attempts
- Health check state files (`.last_restart_attempt`, `.restart_fail_count`) for tracking restart history

### Changed

- Health check cron interval from every 5 minutes to every 1 minute
- Progress messages replaced with typing indicator only (no more "Still working on it..." text spam)
- `IS_SANDBOX` now auto-sets to `1` only when running as root; non-root users don't need it

### Fixed

- Off-by-one in retry count log message (said "4 retries" when there were 5 attempts)
- Fallback message send now verifies API response (previously silent on failure)
- `_env_quote` now escapes newlines (prevents malformed service.env)
- `server.py` OSError handler calls `proc.wait()` to prevent zombie processes
- `claude.sh` exits with error if HOME cannot be determined
- Unclosed code blocks in TTS text cleaning no longer delete to EOF

### Security

- Magic byte validation for voice messages (OGG header `4f676753`)
- Timeouts on voice file downloads (`--connect-timeout 10 --max-time 60`)
- Voice temp files cleaned up in RETURN trap (prevents orphan files)

## [1.1.0] - 2026-02-04

### Added

- Voice messages: send a voice message and Claudio transcribes it (STT), processes it, and responds with both audio (TTS) and text
- Speech-to-text transcription via ElevenLabs STT (`lib/stt.sh`)
- `ELEVENLABS_STT_MODEL` environment variable for configuring the STT model
- Image upload support: send photos (compressed) or image documents (lossless) to the bot for Claude analysis
- Per-chat message queue size limit (100) to prevent unbounded memory growth
- Queue capacity warning when reaching 80% per chat
- Typing indicator self-terminates if parent process is killed (prevents orphan processes)

### Removed

- `/voice` toggle command — voice responses are now automatic when you send a voice message

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
