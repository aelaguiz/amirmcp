"""Turn Claude Code and Codex transcript files into short, citable entries.

Both runtimes append one JSON record per line. An entry's ``cursor`` is the byte
offset of the line it came from, so a cursor is stable as the file grows and
lets a caller read "everything after this point" or check an entry against the
raw file.

Entry kinds:
  user       something Amir (or a parent agent) typed
  assistant  text the agent wrote
  tool       a tool call the agent made (with its outcome when the runtime
             records them together, as Codex does)
  result     the output of a tool call (Claude records these separately)
  event      a turn started or finished, a subagent started, and similar
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

TEXT_LIMIT = 2000
TOOL_LIMIT = 400
SYSTEM_TAGS = ("<command-name>", "<command-message>", "<local-command-", "<task-notification>", "<bash-")


def local_ts(ts: str) -> str:
    """ISO UTC timestamps from the transcript, in this Mac's local time."""
    if not ts:
        return ts
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ts


@dataclass
class Entry:
    cursor: int
    ts: str
    kind: str
    text: str
    is_error: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = {"cursor": self.cursor, "ts": local_ts(self.ts), "kind": self.kind, "text": self.text}
        if self.is_error:
            d["is_error"] = True
        return d


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" ...[{len(text) - limit} more chars]"


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"[{len(text) - limit} earlier chars] ..." + text[-limit:]


# ---------------------------------------------------------------- Claude Code


def _claude_tool_summary(name: str, args: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "url", "query", "pattern", "description", "prompt"):
        if isinstance(args.get(key), str):
            detail = args[key]
            break
    else:
        detail = json.dumps(args, ensure_ascii=False)
    return _clip(f"{name}: {detail}", TOOL_LIMIT)


def _claude_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def claude_entries(record: dict[str, Any], cursor: int) -> list[Entry]:
    kind = record.get("type")
    ts = record.get("timestamp", "")
    attachment = record.get("attachment") or {}
    if kind == "attachment" and attachment.get("type") == "queued_command" and attachment.get("prompt"):
        # A message that arrived while the agent was mid-turn: Amir's typing, or
        # a background task reporting back.
        origin = (attachment.get("origin") or {}).get("kind", "human")
        if origin != "human":
            return [Entry(cursor, ts, "event", f"{origin} delivered: {_clip(str(attachment['prompt']), TOOL_LIMIT)}")]
        return [Entry(cursor, ts, "user", _clip(str(attachment["prompt"]), TEXT_LIMIT))]
    if kind not in ("user", "assistant") or record.get("isMeta") or record.get("isSidechain"):
        return []
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        if not content.strip():
            return []
        if kind == "user" and content.lstrip().startswith(SYSTEM_TAGS):
            # Slash commands, their output, and background-task reports arrive
            # as user records but are not something Amir typed.
            return [Entry(cursor, ts, "event", _clip(content, TOOL_LIMIT))]
        return [Entry(cursor, ts, kind, _clip(content, TEXT_LIMIT))]
    entries = []
    for block in content or []:
        btype = block.get("type")
        if btype == "text" and block.get("text", "").strip():
            entries.append(Entry(cursor, ts, kind, _clip(block["text"], TEXT_LIMIT)))
        elif btype == "tool_use":
            entries.append(Entry(cursor, ts, "tool", _claude_tool_summary(block.get("name", "?"), block.get("input") or {})))
        elif btype == "tool_result":
            entries.append(
                Entry(
                    cursor,
                    ts,
                    "result",
                    _tail(_claude_result_text(block.get("content")), TOOL_LIMIT),
                    is_error=bool(block.get("is_error")),
                )
            )
    return entries


# ---------------------------------------------------------------------- Codex


def _codex_text(content: list[dict[str, Any]] | None) -> str:
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def _codex_item(item: dict[str, Any], cursor: int, ts: str) -> Entry | None:
    itype = item.get("type")
    if itype == "UserMessage":
        text = _codex_text(item.get("content"))
        return Entry(cursor, ts, "user", _clip(text, TEXT_LIMIT)) if text.strip() else None
    if itype == "AgentMessage":
        text = _codex_text(item.get("content"))
        return Entry(cursor, ts, "assistant", _clip(text, TEXT_LIMIT)) if text.strip() else None
    if itype == "CommandExecution":
        command = item.get("command") or []
        shown = command[-1] if command else ""
        output = item.get("aggregated_output") or item.get("stdout") or ""
        code = item.get("exit_code")
        text = f"$ {_clip(shown, TOOL_LIMIT)}\n-> exit {code}: {_tail(output, TOOL_LIMIT)}"
        return Entry(cursor, ts, "tool", text, is_error=code not in (0, None))
    if itype == "McpToolCall":
        args = json.dumps(item.get("arguments") or {}, ensure_ascii=False)
        text = _clip(f"{item.get('server')}.{item.get('tool')}: {args}", TOOL_LIMIT)
        if item.get("error"):
            return Entry(cursor, ts, "tool", text + f"\n-> error: {_clip(str(item['error']), TOOL_LIMIT)}", is_error=True)
        return Entry(cursor, ts, "tool", text)
    if itype == "FileChange":
        changes = item.get("changes") or {}
        listed = ", ".join(f"{v.get('type', 'edit')} {path}" for path, v in changes.items())
        return Entry(cursor, ts, "tool", _clip(f"file changes: {listed}", TOOL_LIMIT))
    if itype == "Extension":
        detail = item.get("query") or json.dumps(item.get("action") or {}, ensure_ascii=False)
        return Entry(cursor, ts, "tool", _clip(f"{item.get('kind', 'extension')}: {detail}", TOOL_LIMIT))
    if itype == "SubAgentActivity":
        return Entry(cursor, ts, "event", f"subagent {item.get('kind', '')}: {item.get('agent_path', '')}")
    return None


def codex_entries(record: dict[str, Any], cursor: int) -> list[Entry]:
    if record.get("type") != "event_msg":
        return []
    payload = record.get("payload") or {}
    ts = record.get("timestamp", "")
    ptype = payload.get("type")
    if ptype == "item_completed":
        entry = _codex_item(payload.get("item") or {}, cursor, ts)
        return [entry] if entry else []
    if ptype == "task_started":
        return [Entry(cursor, ts, "event", "turn started")]
    if ptype == "task_complete":
        return [Entry(cursor, ts, "event", "turn finished")]
    if ptype == "turn_aborted":
        return [Entry(cursor, ts, "event", f"turn aborted: {payload.get('reason', '')}")]
    return []


# ------------------------------------------------------------------- reading


def parser_for(runtime: str):
    return claude_entries if runtime == "claude" else codex_entries


def _parse_line(raw: bytes, cursor: int, parse) -> list[Entry]:
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(record, dict):
        return []
    return parse(record, cursor)


def iter_lines(path: str, start: int = 0) -> Iterator[tuple[int, bytes]]:
    """Yield (offset, raw line) for every complete line at or after ``start``."""
    with open(path, "rb") as fh:
        fh.seek(start)
        offset = start
        for raw in fh:
            if not raw.endswith(b"\n"):
                return  # a line still being written
            yield offset, raw
            offset += len(raw)


def entries_after(path: str, runtime: str, cursor: int, limit: int) -> tuple[list[Entry], int, bool]:
    """Entries on lines after ``cursor``; returns (entries, next_cursor, more)."""
    parse = parser_for(runtime)
    start = cursor
    entries: list[Entry] = []
    next_cursor = cursor
    with open(path, "rb") as fh:
        fh.seek(start)
        if start:
            fh.readline()  # skip the line the cursor points at
        offset = fh.tell()
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            found = _parse_line(raw, offset, parse)
            if found and len(entries) + len(found) > limit and entries:
                return entries, next_cursor, True
            entries.extend(found)
            next_cursor = offset
            offset += len(raw)
    return entries, next_cursor, False


def tail_entries(path: str, runtime: str, limit: int, before: int | None = None) -> tuple[list[Entry], bool]:
    """The last ``limit`` entries before byte offset ``before`` (default: end of file).

    Returns (entries in chronological order, whether earlier entries exist).
    """
    parse = parser_for(runtime)
    end = os.path.getsize(path) if before is None else before
    window = 256 * 1024
    while True:
        start = max(0, end - window)
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read(end - start)
        # The last piece is empty (the blob ended on a newline) or a line still
        # being written; the first piece may begin mid-line. Drop both.
        pieces = blob.split(b"\n")[:-1]
        offset = start
        if start > 0 and pieces:
            offset += len(pieces[0]) + 1
            pieces = pieces[1:]
        parsed: list[Entry] = []
        for raw in pieces:
            parsed.extend(_parse_line(raw, offset, parse))
            offset += len(raw) + 1
        if len(parsed) >= limit or start == 0:
            return parsed[-limit:], start > 0 or len(parsed) > limit
        window *= 4


def all_entries(path: str, runtime: str, kinds: set[str] | None = None) -> Iterator[Entry]:
    parse = parser_for(runtime)
    for offset, raw in iter_lines(path):
        for entry in _parse_line(raw, offset, parse):
            if kinds is None or entry.kind in kinds:
                yield entry


def user_requests(path: str, runtime: str) -> list[Entry]:
    """Every user message in the session, oldest first: the session's objectives."""
    parse = parser_for(runtime)
    # A cheap byte check before parsing; whitespace-agnostic on purpose.
    markers = (b'"user"', b'"queued_command"') if runtime == "claude" else (b'"UserMessage"',)
    found = []
    for offset, raw in iter_lines(path):
        if any(m in raw for m in markers):
            found.extend(e for e in _parse_line(raw, offset, parse) if e.kind == "user")
    return found


def search(path: str, runtime: str, query: str, limit: int) -> list[dict[str, Any]]:
    """Entries whose text contains ``query`` (case-insensitive), with the entry
    just before and just after each one for context."""
    parse = parser_for(runtime)
    needle = query.lower()
    raw_needle = needle.encode()
    hits: list[dict[str, Any]] = []
    recent_lines: deque[tuple[int, bytes]] = deque(maxlen=6)
    last_entry: Entry | None = None
    pending: dict[str, Any] | None = None
    for offset, raw in iter_lines(path):
        if pending is None and raw_needle not in raw.lower():
            recent_lines.append((offset, raw))
            last_entry = None
            continue
        for entry in _parse_line(raw, offset, parse):
            if pending is not None:
                pending["after"] = entry.as_dict()
                pending = None
            if needle in entry.text.lower():
                before = last_entry or _last_entry_of(recent_lines, parse)
                hit = {"match": entry.as_dict(), "before": before.as_dict() if before else None, "after": None}
                hits.append(hit)
                if len(hits) >= limit:
                    return hits
                pending = hit
            last_entry = entry
        recent_lines.clear()
    return hits


def _last_entry_of(lines: deque[tuple[int, bytes]], parse) -> Entry | None:
    for offset, raw in reversed(lines):
        found = _parse_line(raw, offset, parse)
        if found:
            return found[-1]
    return None
