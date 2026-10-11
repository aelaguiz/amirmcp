import asyncio
import json
import time

from amirmcp import herdr, server, sessions
from amirmcp.server import SecretPath
from amirmcp.sessions import Session, Snapshot


def make_home(tmp_path, session_id, records):
    home = tmp_path / "claude-homes" / "pro5" / ".claude"
    project = home / "projects" / "-Users-x-repo"
    project.mkdir(parents=True)
    with open(project / f"{session_id}.jsonl", "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return str(home)


def record(status, **extra):
    return {"pid": 4242, "sessionId": "abcd1234-0000", "cwd": "/Users/x/repo", "status": status, "updatedAt": time.time() * 1000, **extra}


SIMPLE = [
    {"type": "user", "timestamp": "2026-10-09T20:00:00Z", "message": {"content": "ship it"}},
    {"type": "ai-title", "aiTitle": "Ship the thing"},
    {"type": "assistant", "timestamp": "2026-10-09T20:00:02Z", "gitBranch": "main", "message": {"content": [{"type": "text", "text": "Done."}]}},
]


def test_claude_states(tmp_path):
    home = make_home(tmp_path, "abcd1234-0000", SIMPLE)
    now = time.time()
    expected = {"busy": "working", "shell": "working", "waiting": "blocked", "idle": "idle", "odd": "unknown"}
    for status, state in expected.items():
        s = sessions.claude_session(home, record(status), None, now)
        assert s.state == state, status
    s = sessions.claude_session(home, record("idle"), None, now)
    assert s.account == "pro5" and s.title == "Ship the thing" and s.branch == "main"
    assert s.first_request == "ship it" and s.transcript.endswith("abcd1234-0000.jsonl")


def test_unanswered_question_is_blocked(tmp_path):
    asking = SIMPLE + [
        {"type": "assistant", "timestamp": "2026-10-09T20:01:00Z", "message": {"content": [
            {"type": "tool_use", "name": "AskUserQuestion", "input": {"description": "Merge now?"}}]}},
    ]
    home = make_home(tmp_path, "abcd1234-0000", asking)
    s = sessions.claude_session(home, record("busy"), None, time.time())
    assert s.state == "blocked" and "AskUserQuestion" in s.state_reason


def pane(pane_id="wH2:p1", workspace="SEO 10x plan"):
    return herdr.Pane("growth", pane_id, "wH2", workspace, "wH2:t1", "1", "title", "/repo", None, "unknown")


def test_codex_short_ids_differ_for_same_minute_threads():
    a = Session("codex", "01a12297-3cd5-7cf1-8a2e-c7fa2ec4d46e", None, None, "/r", "a")
    b = Session("codex", "01a12297-a8b0-74d0-a48f-5064ddcb2046", None, None, "/r", "b")
    assert a.short_id != b.short_id
    assert Session("claude", "d8e7a6ac-1111-2222", None, None, "/r", "c").short_id == "claude:d8e7a6ac"


def test_find_by_id_pane_and_name():
    seo = Session("claude", "d8e7a6ac-1111-2222", None, "pro5", "/Users/x/psagentspace", "10x organic and AI referral", pane=pane())
    bugs = Session("codex", "01a12297-3cd5-7cf1-8a2e-c7fa2ec4d46e", None, None, "/Users/x/psagentspace", "root cause 7249", pane=pane("wF7:p1", "7249"))
    snap = Snapshot([seo, bugs], [], time.time())
    assert sessions.find(snap, "claude:d8e7a6ac") == [seo]
    assert sessions.find(snap, "d8e7a6ac") == [seo]
    assert sessions.find(snap, bugs.short_id) == [bugs]
    assert sessions.find(snap, "wF7:p1") == [bugs]
    assert sessions.find(snap, "the seo plan") == [seo]
    assert sessions.find(snap, "psagentspace") == [seo, bugs]  # ambiguous: both returned
    assert sessions.find(snap, "nothing like this") == []


def test_secret_path_gate():
    seen = []

    def app(name):
        async def serve(scope, receive, send):
            seen.append((name, scope["path"]))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        return serve

    gate = SecretPath({"s3cret-value-long-enough": app("full"), "read-secret-long-enough": app("read")})

    def call(path):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        asyncio.run(gate({"type": "http", "path": path, "headers": []}, receive, send))
        return sent[0]["status"]

    assert call("/mcp") == 404
    assert call("/wrong/mcp") == 404
    assert call("/s3cret-value-long-enoughX/mcp") == 404
    assert call("/s3cret-value-long-enough/mcp") == 200
    assert call("/read-secret-long-enough/mcp") == 200
    assert seen == [("full", "/mcp"), ("read", "/mcp")]


def test_read_secret_reaches_only_the_reading_tools():
    """Grok Bot's endpoint lists the reading tools and nothing that types into a pane."""
    full = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    read = {t.name: t for t in asyncio.run(server.mcp_read.list_tools())}
    assert set(full) - set(read) == {"send_message", "press_keys"}
    assert all(t.annotations.read_only_hint for t in read.values())
    assert "send_message" not in server.mcp_read.instructions


def test_agent_in_ssh_session_gets_the_remote_pane(tmp_path, monkeypatch):
    """On amir-server, an agent's SSH_CONNECTION names the Mac pane it runs in."""
    home = make_home(tmp_path, "abcd1234-0000", SIMPLE)
    mac_pane = herdr.Pane("rustai", "w1A:p1", "w1A", "SNG bots", "w1A:t1", "1", "SNG backend", "/home/x", None, "unknown",
                          processes=[{"name": "ssh", "pid": 8996, "cmdline": "ssh home", "tcp_local": ["100.72.74.74:50370"]}],
                          herdr_host="amir-m5")
    monkeypatch.setenv("AMIRMCP_HERDR_HOSTS", "amir-m5")
    monkeypatch.setattr(herdr, "available", lambda: False)
    monkeypatch.setattr(herdr, "remote_panes", lambda host: ([mac_pane], []))
    monkeypatch.setattr(sessions, "claude_process_records", lambda: {4242: (home, record("busy"))})
    monkeypatch.setattr(sessions, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(sessions, "_codex_pids", lambda: [])
    monkeypatch.setattr(sessions, "codex_account_labels", lambda: {})
    monkeypatch.setattr(sessions, "_process_env", lambda pid: {"SSH_CONNECTION": "100.72.74.74 50370 100.66.11.7 22"})
    snap = sessions.snapshot(recent_hours=0)
    [s] = snap.sessions
    assert s.pane is mac_pane and s.state == "working" and not s.notes
    assert s.as_dict(time.time())["where"]["herdr_on"] == "amir-m5"

    # A different SSH client (another terminal) is not in any Herdr pane.
    monkeypatch.setattr(sessions, "_process_env", lambda pid: {"SSH_CONNECTION": "100.72.74.74 59999 100.66.11.7 22"})
    [s] = sessions.snapshot(recent_hours=0).sessions
    assert s.pane is None and s.notes == ["Running, but not in any Herdr pane."]


def test_pane_survives_the_json_dump():
    from dataclasses import asdict

    p = herdr.Pane("rustai", "w15:p1", "w15", "x", "w15:t1", "1", "t", "/", None, "unknown", processes=[{"name": "ssh"}])
    again = herdr.Pane(**{**json.loads(json.dumps(asdict(p))), "herdr_host": "amir-m5"})
    assert again.location()["herdr_on"] == "amir-m5" and again.processes == [{"name": "ssh"}]
