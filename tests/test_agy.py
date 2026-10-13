"""agy (Antigravity CLI) conversations: step records, cursors, and sessions.

agy keeps one SQLite row per step with the step as a protobuf record. These
tests build such records by field number, the same numbers the reader uses
(from the definitions embedded in agy 1.3.4; see docs/AGY_SUPPORT.md), and
check what the connector makes of them.
"""

import json
import sqlite3

from amirmcp import herdr, sessions, transcripts


def varint(n):
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def msg(*parts):
    return b"".join(parts)


def step(kind, status, seconds, body_field, body, tool=None):
    meta = field(1, msg(field(1, seconds)))  # metadata.created_at
    if tool:
        meta += field(4, msg(field(2, tool)))  # metadata.tool_call.name
    return msg(field(1, kind), field(4, status), field(5, meta), field(body_field, body))


def args(**kv):
    return b"".join(field(1, msg(field(1, k), field(2, v))) for k, v in kv.items())


T0 = 1791930000
QUESTIONS = json.dumps([{"question": "Which fruit?", "options": ["Apple", "Banana"]}])
STEPS = [
    (0, 14, 3, step(14, 3, T0, 19, msg(field(2, "From Amir, via ChatGPT: run the tests")))),
    (1, 15, 3, step(15, 3, T0 + 5, 20, msg(field(7, msg(field(2, "run_command")))))),  # tool call only, no text
    (2, 132, 3, step(132, 3, T0 + 6, 140, msg(args(CommandLine="pytest -q", toolSummary="Run tests"), field(2, msg(field(1, "The command exited with code 1.\nOutput:\n2 failed")))), tool="run_command")),
    (3, 14, 3, step(14, 3, T0 + 7, 19, msg(field(2, "Also fix the failures"), field(6, 1)))),  # queued mid-turn
    (4, 15, 3, step(15, 3, T0 + 9, 20, msg(field(1, "Two tests fail in test_calc.py.")))),
    (5, 101, 3, step(101, 3, T0 + 10, 114, msg(field(1, "[Notice] background task finished")))),
    (6, 132, 9, step(132, 9, T0 + 11, 140, msg(args(questions=QUESTIONS, toolSummary="Ask which fruit")), tool="ask_question")),
]


def make_db(tmp_path, steps=STEPS, name="conv"):
    path = str(tmp_path / f"{name}.db")
    db = sqlite3.connect(path)
    db.execute("create table steps (idx integer primary key, step_type integer, status integer, step_payload blob)")
    db.executemany("insert into steps values (?, ?, ?, ?)", steps)
    db.commit()
    db.close()
    return path


def test_steps_become_entries(tmp_path):
    path = make_db(tmp_path)
    entries = list(transcripts.all_entries(path, "agy"))
    assert [(e.cursor, e.kind) for e in entries] == [(0, "user"), (2, "tool"), (3, "user"), (4, "assistant"), (5, "event"), (6, "tool")]
    assert entries[0].ts == "2026-10-13T22:20:00Z"
    tool = entries[1]
    assert tool.text.startswith("run_command: pytest -q\n-> ") and tool.is_error  # exit code 1
    assert entries[5].text.startswith("ask_question: Which fruit? (Apple, Banana)")
    assert [e.text for e in transcripts.user_requests(path, "agy")] == ["From Amir, via ChatGPT: run the tests", "Also fix the failures"]


def test_cursors_are_step_indexes(tmp_path):
    path = make_db(tmp_path)
    assert transcripts.last_cursor(path, "agy") == 6
    after, next_cursor, more = transcripts.entries_after(path, "agy", 2, 2)
    assert [e.cursor for e in after] == [3, 4] and next_cursor == 4 and more
    assert [e.cursor for e in transcripts.entries_after(path, "agy", -1, 100)[0]][:1] == [0]
    tail, earlier = transcripts.tail_entries(path, "agy", 2, before=5)
    assert [e.cursor for e in tail] == [3, 4] and earlier
    hits = transcripts.search(path, "agy", "fail", 10)
    assert [h["match"]["cursor"] for h in hits] == [2, 3, 4]
    assert hits[0]["before"]["cursor"] == 0 and hits[0]["after"]["cursor"] == 3


def test_a_step_held_open_is_a_question_for_amir(tmp_path):
    assert transcripts.agy_waiting(make_db(tmp_path)) == "Which fruit? (Apple, Banana)"
    assert transcripts.agy_waiting(make_db(tmp_path, STEPS[:6], name="answered")) is None


def test_reading_never_writes_to_agys_files(tmp_path):
    path = make_db(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    list(transcripts.all_entries(path, "agy"))
    transcripts.last_cursor(path, "agy")
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_agy_in_a_pane_is_its_conversation(tmp_path, monkeypatch):
    home = tmp_path / "antigravity-cli"
    (home / "conversations").mkdir(parents=True)
    conv = "ea82a8ca-434d-4066-b02a-bafad511ec6a"
    make_db(home / "conversations")
    (home / "conversations" / "conv.db").rename(home / "conversations" / f"{conv}.db")
    summaries = sqlite3.connect(home / "conversation_summaries.db")
    summaries.execute(
        "create table conversation_summaries (conversation_id text, title text, preview text, workspace_uris text, "
        "status text, last_modified_time text, parent_conversation_id text)"
    )
    summaries.execute(
        "insert into conversation_summaries values (?, ?, '', ?, 'CASCADE_RUN_STATUS_IDLE', '2026-10-13 22:45:00+00:00', '')",
        (conv, "Lab tests", json.dumps(["file:///tmp/lab"])),
    )
    summaries.commit()
    summaries.close()
    monkeypatch.setattr(sessions, "AGY_HOME", str(home))
    monkeypatch.setattr(sessions, "agy_conversation_for_pid", lambda pid: conv if pid == 9134 else None)
    pane = herdr.Pane("amirmcp-lab", "w1:p1", "w1", "agylab", "w1:t1", "1", "", "/tmp/lab", "agy", "idle",
                      processes=[{"name": "agy", "pid": 9134}])
    s = sessions._agy_for_pid(9134, pane, now=T0 + 60)
    assert (s.short_id, s.title, s.cwd, s.pane, s.account) == ("agy:ea82a8ca", "Lab tests", "/tmp/lab", pane, None)
    # The ask_question step is still open, so agy is waiting on Amir even though Herdr says idle.
    assert s.state == "blocked" and "Which fruit?" in s.state_reason
    fresh = sessions._agy_for_pid(4242, pane, now=T0)
    assert fresh.transcript is None and fresh.state == "idle" and "not started a conversation" in fresh.state_reason
