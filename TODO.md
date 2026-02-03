# TODO

## Critical (Priority 1)

- [x] **Webhook signature validation** — Anyone with the tunnel URL can send messages. Telegram sends an `X-Telegram-Bot-API-Secret-Token` header that should be verified.
- [x] **Race conditions** — Migrated from JSONL to SQLite which handles concurrency natively.
- [ ] **No tests** — Basic integration tests would prevent regressions.
- [ ] **Input validation** — Commands `/opus`, `/sonnet` don't validate the model before saving.

## Important (Priority 2)

- [ ] Cloudflared URL detection fails silently after 30 seconds
- [ ] No retries with backoff for Telegram API
- [ ] Health check (`/health`) doesn't verify actual system state
- [ ] Plain text logs — structured JSON would be better

## Minor Improvements

- [ ] Support for editing messages and reactions
- [ ] File uploads
- [ ] Rate limiting
- [ ] ShellCheck linting for scripts
- [ ] Environment variables documentation
