import json

from amirmcp import transcripts as t


def write_jsonl(path, records, partial_tail=None):
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        if partial_tail:
            fh.write(partial_tail)  # a line still being written, no newline
    return str(path)


def claude_records():
    return [
        {"type": "user", "timestamp": "2026-10-09T20:00:00Z", "message": {"role": "user", "content": "fix the login bug"}},
        {"type": "user", "isMeta": True, "timestamp": "2026-10-09T20:00:01Z", "message": {"content": "meta noise"}},
        {"type": "assistant", "timestamp": "2026-10-09T20:00:02Z", "message": {"content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": "I'll run the tests."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}},
        {"type": "user", "timestamp": "2026-10-09T20:00:09Z", "message": {"content": [
            {"type": "tool_result", "content": "3 passed", "is_error": False},
        ]}},
        {"type": "attachment", "timestamp": "2026-10-09T20:00:10Z", "attachment": {
            "type": "queued_command", "prompt": "also update the docs", "origin": {"kind": "human"}}},
        {"type": "attachment", "timestamp": "2026-10-09T20:00:11Z", "attachment": {
            "type": "queued_command", "prompt": "<task-notification>done</task-notification>", "origin": {"kind": "task-notification"}}},
        {"type": "user", "timestamp": "2026-10-09T20:00:12Z", "message": {"content": "<command-name>/effort</command-name>"}},
        {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-09T20:00:13Z", "message": {"content": [{"type": "text", "text": "subagent chatter"}]}},
        {"type": "assistant", "timestamp": "2026-10-09T20:00:14Z", "message": {"content": [{"type": "text", "text": "Tests pass; login is fixed."}]}},
    ]


def codex_records():
    item = lambda ts, it: {"timestamp": ts, "type": "event_msg", "payload": {"type": "item_completed", "item": it}}
    return [
        {"timestamp": "2026-10-09T20:00:00Z", "type": "session_meta", "payload": {"id": "01a1-x", "creator_account_id": "acct-1"}},
        {"timestamp": "2026-10-09T20:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
        item("2026-10-09T20:00:01Z", {"type": "UserMessage", "content": [{"type": "text", "text": "root cause 6925"}]}),
        item("2026-10-09T20:00:02Z", {"type": "AgentMessage", "content": [{"type": "Text", "text": "Looking at the issue."}]}),
        item("2026-10-09T20:00:03Z", {"type": "CommandExecution", "command": ["/bin/zsh", "-lc", "make test"], "exit_code": 2, "aggregated_output": "1 failed"}),
        item("2026-10-09T20:00:04Z", {"type": "McpToolCall", "server": "db", "tool": "bq_execute_sql", "arguments": {"sql": "select 1"}}),
        item("2026-10-09T20:00:05Z", {"type": "FileChange", "changes": {"/repo/a.py": {"type": "update"}}}),
        item("2026-10-09T20:00:06Z", {"type": "Reasoning"}),
        {"timestamp": "2026-10-09T20:00:07Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    ]


def test_claude_entries(tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", claude_records())
    entries = list(t.all_entries(path, "claude"))
    kinds = [(e.kind, e.text) for e in entries]
    assert kinds[0] == ("user", "fix the login bug")
    assert ("assistant", "I'll run the tests.") in kinds
    assert ("tool", "Bash: pytest -q") in kinds
    assert ("result", "3 passed") in kinds
    assert ("user", "also update the docs") in kinds
    assert not any("meta noise" in text or "subagent chatter" in text for _, text in kinds)
    # Slash commands and background reports are events, not Amir's requests.
    assert [e.text for e in t.user_requests(path, "claude")] == ["fix the login bug", "also update the docs"]
    assert sum(1 for k, _ in kinds if k == "event") == 2


def test_codex_entries(tmp_path):
    path = write_jsonl(tmp_path / "x.jsonl", codex_records())
    entries = list(t.all_entries(path, "codex"))
    assert [e.kind for e in entries] == ["event", "user", "assistant", "tool", "tool", "tool", "event"]
    command = entries[3]
    assert command.text.startswith("$ make test") and "exit 2" in command.text and command.is_error
    assert entries[4].text.startswith("db.bq_execute_sql")
    assert "update /repo/a.py" in entries[5].text
    assert [e.text for e in t.user_requests(path, "codex")] == ["root cause 6925"]


def test_cursors_page_both_ways_and_skip_partial_line(tmp_path):
    path = write_jsonl(tmp_path / "x.jsonl", codex_records(), partial_tail='{"timestamp": "2026')
    tail, more = t.tail_entries(path, "codex", 3)
    assert [e.kind for e in tail] == ["tool", "tool", "event"] and more
    earlier, more_before = t.tail_entries(path, "codex", 10, before=tail[0].cursor)
    assert all(e.cursor < tail[0].cursor for e in earlier)
    assert [e.text for e in earlier][:2] == ["turn started", "root cause 6925"]
    assert not more_before
    after, next_cursor, more_after = t.entries_after(path, "codex", earlier[1].cursor, 2)
    assert [e.cursor for e in after] == [earlier[2].cursor, earlier[3].cursor] and more_after
    rest, _, more_after = t.entries_after(path, "codex", next_cursor, 50)
    assert rest[0].cursor > after[-1].cursor and not more_after
    assert rest[-1].text == "turn finished"  # the half-written line is never parsed


def test_search_returns_neighbours(tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", claude_records())
    hits = t.search(path, "claude", "PYTEST", 5)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["match"]["text"] == "Bash: pytest -q"
    assert hit["before"]["text"] == "I'll run the tests."
    assert hit["after"]["text"] == "3 passed"


def test_timestamps_are_local(tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", claude_records()[:1])
    entry = next(t.all_entries(path, "claude")).as_dict()
    assert not entry["ts"].endswith("Z") and ("+" in entry["ts"] or "-" in entry["ts"][19:])
