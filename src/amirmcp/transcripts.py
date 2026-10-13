"""Turn Claude Code, Codex and agy transcripts into short, citable entries.

Claude Code and Codex append one JSON record per line; an entry's ``cursor`` is
the byte offset of the line it came from. agy keeps one SQLite row per step,
and its cursor is the step index. Either way a cursor is stable as the
transcript grows and lets a caller read "everything after this point" or check
an entry against the raw transcript.

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
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
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


# ------------------------------------------------------------------------ agy
#
# Antigravity CLI keeps each conversation in its own SQLite file, one row per
# step, and the step itself as a protobuf record (``gemini_coder.Step``). The
# field numbers below come from the definitions embedded in the agy binary;
# protobuf numbers do not change between versions. An agy cursor is the step
# index, not a byte offset. See docs/AGY_SUPPORT.md.

AGY_USER_INPUT, AGY_REPLY, AGY_TOOL, AGY_ERROR, AGY_SYSTEM, AGY_SUBAGENT, AGY_QUESTION = 14, 15, 132, 17, 101, 127, 138
STEP_ERROR, STEP_WAITING = 7, 9  # CortexStepStatus


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, i
        shift += 7


def _fields(buf: bytes | None) -> dict[int, list[Any]]:
    """A protobuf record's fields: number -> values (ints, or bytes for anything length-delimited)."""
    out: dict[int, list[Any]] = {}
    i = 0
    buf = buf or b""
    try:
        while i < len(buf):
            key, i = _varint(buf, i)
            number, wire = key >> 3, key & 7
            if wire == 0:
                value, i = _varint(buf, i)
            elif wire == 1:
                value, i = int.from_bytes(buf[i : i + 8], "little"), i + 8
            elif wire == 2:
                size, i = _varint(buf, i)
                value, i = buf[i : i + size], i + size
            elif wire == 5:
                value, i = int.from_bytes(buf[i : i + 4], "little"), i + 4
            else:
                break
            out.setdefault(number, []).append(value)
    except IndexError:
        pass
    return out


def _sub(fields: dict[int, list[Any]], number: int) -> dict[int, list[Any]]:
    value = (fields.get(number) or [b""])[0]
    return _fields(value) if isinstance(value, bytes) else {}


def _str(fields: dict[int, list[Any]], number: int) -> str:
    value = (fields.get(number) or [b""])[0]
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


def _int(fields: dict[int, list[Any]], number: int) -> int:
    value = (fields.get(number) or [0])[0]
    return value if isinstance(value, int) else 0


def _agy_ts(meta: dict[int, list[Any]]) -> str:
    created = _sub(meta, 1)  # metadata.created_at, a Timestamp
    seconds = _int(created, 1)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if seconds else ""


def agy_questions(raw: str) -> str:
    """The questions an ask_question call puts to Amir, with their options."""
    try:
        asked = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return raw
    return " / ".join(
        f"{q.get('question', '')} ({', '.join(str(o) for o in q.get('options') or [])})" if q.get("options") else str(q.get("question", ""))
        for q in asked
        if isinstance(q, dict)
    )


def _agy_tool_text(name: str, args: dict[str, str], output: str) -> str:
    if name == "ask_question" and args.get("questions"):
        args = {"Question": agy_questions(args["questions"])}
    for key in ("CommandLine", "AbsolutePath", "Url", "Query", "Prompt", "Question", "toolSummary"):
        if args.get(key):
            detail = args[key]
            break
    else:
        detail = json.dumps(args, ensure_ascii=False)
    text = _clip(f"{name}: {detail}", TOOL_LIMIT)
    return text + (f"\n-> {_tail(output, TOOL_LIMIT)}" if output.strip() else "")


def agy_step_entries(idx: int, payload: bytes) -> list[Entry]:
    step = _fields(payload)
    kind = _int(step, 1)
    meta = _sub(step, 5)
    ts = _agy_ts(meta)
    if kind == AGY_USER_INPUT:
        user = _sub(step, 19)
        text = _str(user, 2) or "\n".join(_str(_fields(item), 1) for item in user.get(3, []))
        return [Entry(idx, ts, "user", _clip(text, TEXT_LIMIT))] if text.strip() else []
    if kind == AGY_REPLY:
        reply = _sub(step, 20)
        text = _str(reply, 8) or _str(reply, 1)  # modified_response, else response
        return [Entry(idx, ts, "assistant", _clip(text, TEXT_LIMIT))] if text.strip() else []
    if kind == AGY_TOOL:
        tool = _sub(step, 140)
        args = {}
        for entry in tool.get(1, []):
            pair = _fields(entry)
            args[_str(pair, 1)] = _str(pair, 2)
        output = _str(_sub(tool, 2), 1)
        name = _str(_sub(meta, 4), 2) or "tool"  # metadata.tool_call.name
        exit_code = re.search(r"exited with code (-?\d+)", output[:200])
        failed = _int(step, 4) == STEP_ERROR or 31 in step or bool(exit_code and exit_code.group(1) != "0")
        return [Entry(idx, ts, "tool", _agy_tool_text(name, args, output), is_error=failed)]
    if kind == AGY_ERROR:
        error = _sub(_sub(step, 24), 3)
        return [Entry(idx, ts, "tool", _clip(f"error: {_str(error, 1) or _str(error, 2)}", TOOL_LIMIT), is_error=True)]
    if kind == AGY_QUESTION:
        asked = [_str(_fields(q), 1) for q in _sub(step, 154).get(1, [])]
        return [Entry(idx, ts, "tool", _clip("ask_question: " + " / ".join(asked), TOOL_LIMIT))]
    if kind == AGY_SUBAGENT:
        call = _sub(step, 143)
        return [Entry(idx, ts, "event", _clip(f"subagent {_str(call, 1)}: {_str(call, 2)}", TOOL_LIMIT))]
    if kind == AGY_SYSTEM:
        text = _str(_sub(step, 114), 1)
        return [Entry(idx, ts, "event", _clip(text, TOOL_LIMIT))] if text.strip() else []
    return []


def agy_query(path: str, sql: str, params: tuple = ()) -> tuple[sqlite3.Connection, sqlite3.Cursor]:
    """Run a query on an agy database without writing to it; the caller closes it.

    While agy has the file open (its WAL index exists) read through the WAL, so
    the newest steps show. An agy stopped mid-write (suspended with ctrl+z)
    keeps the database locked; then, and when agy is not running, read the file
    as last written.
    """
    uris = [f"file:{path}?immutable=1"]
    if os.path.exists(path + "-shm"):
        uris.insert(0, f"file:{path}?mode=ro")
    for n, uri in enumerate(uris):
        db = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            return db, db.execute(sql, params)
        except sqlite3.OperationalError as exc:
            db.close()
            if "locked" not in str(exc) or n == len(uris) - 1:
                raise
    raise AssertionError("unreachable")


def _agy_steps(path: str, where: str = "", params: tuple = (), order: str = "asc") -> Iterator[tuple[int, bytes]]:
    db, rows = agy_query(path, f"select idx, step_payload from steps {where} order by idx {order}", params)
    try:
        yield from rows
    finally:
        db.close()


def _agy_entries(path: str, where: str = "", params: tuple = (), order: str = "asc") -> Iterator[Entry]:
    for idx, payload in _agy_steps(path, where, params, order):
        yield from agy_step_entries(idx, payload)


def agy_waiting(path: str) -> str | None:
    """What agy is waiting on Amir for: a step it holds open for his answer or approval."""
    for idx, payload in _agy_steps(path, "where status = ?", (STEP_WAITING,), order="desc"):
        for entry in agy_step_entries(idx, payload):
            asked = entry.text.split("\n")[0]
            return asked[len("ask_question: ") :] if asked.startswith("ask_question: ") else f"approval for {asked}"
        return "a step waiting on him"
    return None


def last_cursor(path: str, runtime: str) -> int:
    """A cursor at the end of the transcript: entries_after it returns only what is written later."""
    if runtime == "agy":
        db, rows = agy_query(path, "select max(idx) from steps")
        try:
            row = rows.fetchone()
        finally:
            db.close()
        return row[0] if row and row[0] is not None else -1
    # entries_after skips the line its cursor points at, so point at the last
    # byte already written: the next line is the first one returned.
    return max(os.path.getsize(path) - 1, 0)


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
    if runtime == "agy":
        found: list[Entry] = []
        next_cursor = cursor
        for idx, payload in _agy_steps(path, "where idx > ?", (cursor,)):
            step = agy_step_entries(idx, payload)
            if step and len(found) + len(step) > limit and found:
                return found, next_cursor, True
            found.extend(step)
            next_cursor = idx
        return found, next_cursor, False
    parse = parser_for(runtime)
    start = max(cursor, 0)  # -1 means from the start, as for agy
    entries: list[Entry] = []
    next_cursor = cursor
    with open(path, "rb") as fh:
        fh.seek(start)
        if cursor > 0:
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
    For agy, ``before`` is a step index.
    """
    if runtime == "agy":
        found: list[Entry] = []
        where, params = ("where idx < ?", (before,)) if before is not None else ("", ())
        for entry in _agy_entries(path, where, params, order="desc"):
            if len(found) == limit:
                return found[::-1], True
            found.append(entry)
        return found[::-1], False
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
    if runtime == "agy":
        yield from (e for e in _agy_entries(path) if kinds is None or e.kind in kinds)
        return
    parse = parser_for(runtime)
    for offset, raw in iter_lines(path):
        for entry in _parse_line(raw, offset, parse):
            if kinds is None or entry.kind in kinds:
                yield entry


def user_requests(path: str, runtime: str) -> list[Entry]:
    """Every user message in the session, oldest first: the session's objectives."""
    if runtime == "agy":
        return [e for e in _agy_entries(path, "where step_type = ?", (AGY_USER_INPUT,)) if e.kind == "user"]
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
    if runtime == "agy":
        return _agy_search(path, query, limit)
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


def _agy_search(path: str, query: str, limit: int) -> list[dict[str, Any]]:
    needle = query.lower()
    hits: list[dict[str, Any]] = []
    previous: Entry | None = None
    pending: dict[str, Any] | None = None
    for entry in _agy_entries(path):
        if pending is not None:
            pending["after"] = entry.as_dict()
            pending = None
            if len(hits) >= limit:
                return hits
        if needle in entry.text.lower():
            hit = {"match": entry.as_dict(), "before": previous.as_dict() if previous else None, "after": None}
            hits.append(hit)
            pending = hit
        previous = entry
    return hits
