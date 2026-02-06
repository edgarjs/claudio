# Session Rotation: Hybrid `--resume` with Context Rotation

## Problem

Currently, every webhook invocation spawns a fresh `claude -p` process with `--no-session-persistence`. Conversation continuity is maintained by loading the last 100 messages from SQLite and injecting them as a text prefix into every prompt:

```
Here is the recent conversation history for context:

H: ...
A: ...
---

Now respond to this new message:

<actual message>
```

This has several drawbacks:

1. **Token waste**: The full history is re-sent on every invocation, consuming input tokens even when context hasn't changed.
2. **Lost internal state**: Claude's reasoning, tool use results, and code analysis from prior turns are discarded — only the final text response survives.
3. **Lossy format**: Rich multi-turn conversation is flattened to `H:/A:` text blocks, losing structure.

Simply switching to `--resume` with a persistent session would introduce **context rot** — after many compactions in a long-lived session, early instructions and conversation details degrade progressively.

**Relationship with the cognitive memory system (PR #19):** Session rotation and cognitive memory solve different layers of the context problem. Session rotation handles **short-term continuity** (last ~20 messages, native multi-turn). Cognitive memory handles **long-term knowledge** (extracted facts, preferences, and procedures from all past conversations, retrieved by embedding similarity). They are complementary — session rotation improves conversation *feel*, cognitive memory improves conversation *depth*. Neither replaces the other.

```
Layer 1: Session rotation    → "what did we just talk about?" (last 20 msgs, native --resume)
Layer 2: History bootstrap   → "what happened recently?" (last 100 msgs, text on rotation)
Layer 3: Cognitive memory    → "what do I know about this user/topic?" (all time, embedding retrieval)
```

## Solution: Rotating Sessions with History Bootstrap

Use Claude's native `--resume` for multi-turn continuity within a **bounded session window**, then rotate to a fresh session and bootstrap it with recent history from our SQLite database.

```
Session 1 (messages 1-20):
  msg 1:  claude -p "prompt" --output-format json  → capture session_id
  msg 2:  claude -p "prompt" --resume $session_id
  ...
  msg 20: claude -p "prompt" --resume $session_id

Session 2 (messages 21-40):
  msg 21: claude -p "history context + prompt" --output-format json  → new session_id
  msg 22: claude -p "prompt" --resume $session_id
  ...
```

Benefits:
- Native multi-turn within each window (Claude remembers tool use, reasoning)
- No long-lived session rot — sessions are short-lived
- SQLite history remains the durable memory across rotations
- Lighter prompts for messages 2-N in each window (no history re-injection)

## Architecture

### New config variable

```bash
SESSION_WINDOW_SIZE="${SESSION_WINDOW_SIZE:-20}"  # messages per session before rotation
```

Added to `lib/config.sh` and persisted in `service.env`.

### Database changes (`lib/db.sh`)

Add a `sessions` table to track session lifecycle per chat:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,              -- Claude session_id (UUID)
    chat_id TEXT NOT NULL,            -- Telegram chat_id
    message_count INTEGER DEFAULT 0,  -- messages processed in this session
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_chat_id ON sessions(chat_id);
```

New functions:

```bash
db_get_active_session(chat_id)       # Returns the most recent session_id (by updated_at) if message_count < SESSION_WINDOW_SIZE
db_create_session(chat_id, session_id)  # Creates new session record
db_increment_session(session_id)     # Increments message_count and updates updated_at
db_expire_session(session_id)        # Deletes session record (forces rotation on next message)
db_cleanup_sessions()                # Deletes sessions where updated_at is older than 24h
```

### Changes to `lib/claude.sh`

`claude_run()` gains a second parameter: the chat_id (for session lookup).

**New flow:**

```
claude_run(prompt, chat_id):
  1. session_id = db_get_active_session(chat_id)
     # Selects most recent session (ORDER BY updated_at DESC LIMIT 1)
     # where message_count < SESSION_WINDOW_SIZE
  2. if session_id exists:
       # Resume existing session — no history injection needed
       args = [..., --resume $session_id, -p "$prompt", --output-format json]
  3. else:
       # New session — inject history + memories as bootstrap context
       context = history_get_context()    # last 100 messages (hardcoded)
       memories = memory_retrieve(prompt)  # if cognitive memory is enabled (PR #19)
       full_prompt = context + memories + "---\n\nNow respond:\n\n" + prompt
       args = [..., -p "$full_prompt", --output-format json]
  4. Run claude with args, capture JSON output
  5. Parse JSON: extract result text, session_id, is_error from output
  6. if is_error and was resuming:
       # Resume failed — expire broken session, retry as fresh
       db_expire_session(session_id)
       log warning
       # Re-run steps 3-5 as a new session (no --resume)
  7. if JSON parsing fails (jq error, missing fields):
       # Expire current session to prevent stale state
       if session_id: db_expire_session(session_id)
       log raw output for debugging
       # Return raw stdout as response (best-effort)
  8. if new session:
       db_create_session(chat_id, extracted_session_id)
  9. db_increment_session(extracted_session_id)
  10. Return result text
```

**Concurrency note:** Steps 1 and 9 are not wrapped in a transaction because `server.py` already guarantees serial-per-chat processing (one thread per `chat_id`, sequential dequeue). Two messages from the same chat will never run `claude_run` concurrently. If the concurrency model changes in the future, these operations should be made atomic (e.g., `UPDATE ... WHERE message_count < N RETURNING`).

Key changes:
- Remove `--no-session-persistence` (sessions must persist to disk for `--resume`)
- Add `--output-format json` (needed to capture `session_id`)
- Parse JSON response instead of using raw stdout
- History injection only happens on the **first message** of each session window

### Changes to `lib/telegram.sh`

- Pass `$WEBHOOK_CHAT_ID` to `claude_run` as the second argument
- Response is now extracted from JSON (`result` field) instead of raw stdout

### Error handling

**Resume failure**: If `--resume` fails (session file deleted, corrupted, etc.), fall back to starting a fresh session with history injection. The `is_error` field in the JSON output signals this.

**Session cleanup**: `db_cleanup_sessions()` deletes sessions where `updated_at` is older than 24h. Called from `agent_cleanup_if_needed` (which already runs periodically) to reuse existing cleanup infrastructure.

**Session file cleanup**: Claude stores session files locally at `~/.claude/projects/<project-path>/<session-id>.jsonl` (with optional `<session-id>/` checkpoint directories). These are **local only** — not synced to Anthropic's servers — and persist on disk up to 30 days by default. When `db_cleanup_sessions()` deletes expired session records, it should also delete the corresponding session files and checkpoint directories from disk. This prevents unbounded disk growth from rotated sessions. On server migration, any orphaned session files are harmless — the next message simply starts a fresh session window, and the SQLite history (the real source of truth) is unaffected.

### What does NOT change

- `lib/agent.sh` — Agents remain stateless one-shot invocations with `--no-session-persistence`. They're independent tasks, not conversational turns.
- `lib/history.sh` — History add/get remain unchanged. Note: PR #19 removes `history_trim()` and `db_trim()` — messages are kept forever for the consolidation pipeline. Session rotation is compatible with this: `history_get_context()` already hardcodes a limit of 100 for the bootstrap window regardless of total message count.
- `lib/server.py` — No changes needed. The subprocess interface is identical.
- `lib/telegram.sh` — Webhook parsing, file handling, TTS/STT — all unchanged. Only the `claude_run` call site changes (adding chat_id argument). Note: PR #19 adds a `memory_consolidate` call after the response — this is independent of session rotation and works regardless of whether `--resume` is used.
- `lib/memory.sh` / `lib/memory.py` (PR #19) — Memory retrieval integrates naturally: `memory_retrieve()` is called during bootstrap (new session) to inject long-term knowledge alongside recent history. Within a resumed session, memories are not re-injected (they'd already be in Claude's context from the bootstrap or prior turns).

## File-by-file changes

### `lib/config.sh`
- Add `SESSION_WINDOW_SIZE="${SESSION_WINDOW_SIZE:-20}"`
- Add to `claudio_save_env()` output

### `lib/db.sh`
- Add `sessions` table creation to `db_init()`
- Add `db_get_active_session()`, `db_create_session()`, `db_increment_session()`, `db_expire_session()`
- Add `db_cleanup_sessions()` — deletes sessions with `updated_at` older than 24h and removes corresponding session files from `~/.claude/projects/` (both `.jsonl` and checkpoint directories)

### `lib/claude.sh`
- Accept `chat_id` as second parameter
- Look up active session via `db_get_active_session()`
- Conditionally use `--resume` or fresh invocation with history bootstrap
- On new session: inject `history_get_context()` (last 100 messages) + `memory_retrieve()` (if cognitive memory is enabled) as bootstrap context
- Add `--output-format json`, remove `--no-session-persistence`
- Parse JSON response, extract `result` and `session_id`
- Create/increment session records
- Fallback: if `--resume` returns `is_error: true`, retry as fresh session

### `lib/telegram.sh`
- Pass `$WEBHOOK_CHAT_ID` as second arg to `claude_run()`
- No other changes

### `tests/db.bats`
- Tests for `db_get_active_session`, `db_create_session`, `db_increment_session`, `db_expire_session`
- Test session rotation: after N increments, `db_get_active_session` returns empty

### `tests/claude.bats` (new)
- Test that `claude_run` uses `--resume` when active session exists
- Test that `claude_run` injects history on new session
- Test fallback on resume failure
- Test JSON parsing of response

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| `--resume` session files accumulate on disk | `db_cleanup_sessions()` deletes both DB records and on-disk session files (`.jsonl` + checkpoint dirs) for sessions older than 24h. Runs via existing `agent_cleanup_if_needed` periodic hook |
| Server migration loses active sessions | Non-issue: sessions are ephemeral. Next message starts a fresh window bootstrapped from SQLite history, which is the durable source of truth |
| JSON parsing failure (jq unavailable, malformed output) | Expire current session to prevent stale state, log raw output for debugging, return raw stdout as best-effort response |
| `--output-format json` changes `result` format across claude versions | Pin to extracting `.result` field; log warning if schema changes |
| Session file corruption prevents resume | `is_error` check triggers fresh session fallback |
| Multi-chat support: sessions must be per-chat | `sessions` table keyed on `chat_id`; current single-chat setup works, scales to multi-chat |

## Migration

- **Backwards compatible**: Existing `messages` table is unchanged. First message after upgrade creates a new session automatically.
- **Rollback**: Remove `sessions` table, revert `claude.sh` to use `--no-session-persistence`. History DB is unaffected.
- **No data migration needed**: Sessions are ephemeral by design.
- **Ordering with PR #19**: Either can land first. If PR #19 lands first, `db_trim()`/`history_trim()`/`MAX_HISTORY_LINES` will already be gone — session rotation simply uses `history_get_context()` (hardcoded to 100) and `memory_retrieve()` for bootstrap. If session rotation lands first, it references `history_get_context()` which still works with or without trimming.

## Future considerations

- **Session compaction control**: If Claude CLI adds `/compact` support in `-p` mode, we could compact mid-session instead of rotating.
- **Adaptive window size**: Could track token usage from JSON output and rotate based on token count rather than message count.
- **Per-chat window config**: Different chats could have different window sizes via a config table.
- **Memory-aware rotation**: With the cognitive memory system (PR #19), session rotation could trigger a consolidation pass on rotation boundaries — extracting knowledge from the expiring session window before it's lost. Currently consolidation runs post-response; running it on rotation would ensure nothing slips through the cracks.
