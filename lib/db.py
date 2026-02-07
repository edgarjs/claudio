#!/usr/bin/env python3
"""SQLite helper for parameterized queries (no SQL injection risk).

Usage from bash:
    python3 lib/db.py add <db_path> <role> <content>
    python3 lib/db.py get_context <db_path> <limit>
    python3 lib/db.py init <db_path>
    python3 lib/db.py clear <db_path>
    python3 lib/db.py count <db_path>
    python3 lib/db.py exec <db_path> <sql> [param1 param2 ...]
    python3 lib/db.py query_json <db_path> <sql> [param1 param2 ...]
"""

import json
import sqlite3
import sys
import time

MAX_RETRIES = 5
INITIAL_DELAY = 0.1


def _retry(func, db_path, *args):
    """Execute a DB function with retry on lock contention."""
    delay = INITIAL_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(db_path, *args)
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < MAX_RETRIES:
                import random

                jitter = delay * (0.5 + random.random())
                time.sleep(jitter)
                delay *= 2
            else:
                raise


def cmd_init(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _do_add(db_path, role, content):
    if role not in ("user", "assistant"):
        print(f"db_add: invalid role '{role}'", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO messages (role, content) VALUES (?, ?)", (role, content)
        )
        conn.commit()
    finally:
        conn.close()


def cmd_add(db_path, role, content):
    _retry(_do_add, db_path, role, content)


def _do_get_context(db_path, limit):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT role, content FROM "
            "(SELECT role, content, id FROM messages ORDER BY id DESC LIMIT ?) "
            "ORDER BY id ASC",
            (limit,),
        ).fetchall()
        return rows
    finally:
        conn.close()


def cmd_get_context(db_path, limit_str):
    try:
        limit = int(limit_str)
        if limit <= 0:
            raise ValueError
    except ValueError:
        print(f"db_get_context: invalid limit '{limit_str}'", file=sys.stderr)
        sys.exit(1)

    rows = _retry(_do_get_context, db_path, limit)
    if not rows:
        return

    lines = []
    for role, content in rows:
        prefix = "H" if role == "user" else "A"
        lines.append(f"{prefix}: {content}")

    context = (
        "Here is the recent conversation history for context:\n\n"
        + "\n\n".join(lines)
        + "\n\n"
    )
    print(context)


def cmd_clear(db_path):
    def _do(db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM messages")
            conn.commit()
        finally:
            conn.close()

    _retry(_do, db_path)


def cmd_count(db_path):
    def _do(db_path):
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            return count
        finally:
            conn.close()

    print(_retry(_do, db_path))


def _do_exec(db_path, sql, params):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        conn.commit()
        if rows:
            for row in rows:
                print("|".join("" if col is None else str(col) for col in row))
    finally:
        conn.close()


def cmd_exec(db_path, sql, params):
    """Execute arbitrary SQL with parameterized values."""
    _retry(_do_exec, db_path, sql, params)


def _do_query_json(db_path, sql, params):
    """Execute a SELECT and return results as JSON array of objects."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        result = [dict(row) for row in rows]
        return json.dumps(result)
    finally:
        conn.close()


def cmd_query_json(db_path, sql, params):
    """Execute a SELECT with parameterized values and output JSON."""
    result = _retry(_do_query_json, db_path, sql, params)
    print(result)


def _do_agent_insert(db_path, agent_id, parent_id, prompt, model,
                     timeout_secs, max_concurrent, max_global):
    """Atomic check + insert for agent spawn with concurrency limits."""
    conn = sqlite3.connect(db_path, isolation_level="EXCLUSIVE")
    try:
        cursor = conn.execute(
            """INSERT INTO agents (id, parent_id, prompt, model, timeout_seconds)
               SELECT ?, ?, ?, ?, ?
               WHERE (SELECT COUNT(*) FROM agents
                      WHERE parent_id=? AND status IN ('pending', 'running')) < ?
               AND (SELECT COUNT(*) FROM agents
                    WHERE status IN ('pending', 'running')) < ?""",
            (agent_id, parent_id, prompt, model, timeout_secs,
             parent_id, max_concurrent, max_global),
        )
        changes = cursor.rowcount
        conn.commit()
        return changes
    finally:
        conn.close()


def cmd_agent_insert(db_path, args):
    """agent_insert <agent_id> <parent_id> <prompt> <model> <timeout> <max_concurrent> <max_global>"""
    if len(args) != 7:
        print("Usage: agent_insert <id> <parent> <prompt> <model> <timeout> <max_conc> <max_global>",
              file=sys.stderr)
        sys.exit(1)
    agent_id, parent_id, prompt, model = args[0], args[1], args[2], args[3]
    timeout_secs = int(args[4])
    max_concurrent = int(args[5])
    max_global = int(args[6])
    changes = _retry(_do_agent_insert, db_path, agent_id, parent_id, prompt,
                     model, timeout_secs, max_concurrent, max_global)
    print(changes)


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <command> <db_path> [args...]", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    db_path = sys.argv[2]
    args = sys.argv[3:]

    if command == "init":
        cmd_init(db_path)
    elif command == "add":
        if len(args) != 2:
            print("Usage: add <role> <content>", file=sys.stderr)
            sys.exit(1)
        cmd_add(db_path, args[0], args[1])
    elif command == "get_context":
        limit = args[0] if args else "100"
        cmd_get_context(db_path, limit)
    elif command == "clear":
        cmd_clear(db_path)
    elif command == "count":
        cmd_count(db_path)
    elif command == "exec":
        if len(args) < 1:
            print("Usage: exec <sql> [param1 param2 ...]", file=sys.stderr)
            sys.exit(1)
        cmd_exec(db_path, args[0], tuple(args[1:]))
    elif command == "query_json":
        if len(args) < 1:
            print("Usage: query_json <sql> [param1 param2 ...]", file=sys.stderr)
            sys.exit(1)
        cmd_query_json(db_path, args[0], tuple(args[1:]))
    elif command == "agent_insert":
        cmd_agent_insert(db_path, args)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
