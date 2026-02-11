#!/usr/bin/env python3
"""PostToolUse hook: appends a compact summary line to $CLAUDIO_TOOL_LOG.

Receives PostToolUse JSON on stdin from Claude Code.  Exits immediately
(no-op) when CLAUDIO_TOOL_LOG is not set — so interactive Claude Code
sessions are unaffected.
"""

import json
import os
import sys
from urllib.parse import urlparse


def truncate(text, limit=300):
    """Truncate text to approximately *limit* characters."""
    if not text or len(text) <= limit:
        return text or ""
    return text[:limit] + "..."


def extract_path_basename(input_data):
    """Extract basename from common path fields."""
    for key in ("file_path", "path", "notebook_path"):
        val = input_data.get(key)
        if val:
            return os.path.basename(val)
    return None


def summarize(event):
    """Return a one-line summary string, or None to skip."""
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    tool_output = event.get("tool_output", "")

    # Skip MCP tools — already captured by the notifier system
    if tool.startswith("mcp__"):
        return None

    if tool in ("Read", "Edit", "Write"):
        name = extract_path_basename(tool_input)
        return f"{tool} {name}" if name else tool

    if tool == "Bash":
        cmd = tool_input.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:80] + "..."
        return f'Bash "{cmd}"'

    if tool == "Glob":
        pattern = tool_input.get("pattern", "")
        return f'Glob "{pattern}"'

    if tool == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        if path:
            return f'Grep "{pattern}" in {path}'
        return f'Grep "{pattern}"'

    if tool == "Task":
        subtype = tool_input.get("subagent_type", "")
        prompt = truncate(tool_input.get("prompt", ""), 80)
        label = f"Task({subtype})" if subtype else "Task"
        return f'{label} "{prompt}"' if prompt else label

    if tool == "WebSearch":
        query = tool_input.get("query", "")
        return f'WebSearch "{query}"'

    if tool == "WebFetch":
        url = tool_input.get("url", "")
        try:
            domain = urlparse(url).netloc or url
        except Exception:
            domain = url
        return f"WebFetch {domain}"

    # Other tools: just the tool name
    return tool


def main():
    log_file = os.environ.get("CLAUDIO_TOOL_LOG", "")
    if not log_file:
        return

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    try:
        line = summarize(event)
    except Exception as e:
        print(f"post-tool-use: Error summarizing event: {e}", file=sys.stderr)
        return
    if not line:
        return

    # Replace newlines to keep one-line-per-entry invariant
    line = line.replace("\n", " ").replace("\r", "")

    try:
        with open(log_file, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"post-tool-use: Error writing to tool log: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
