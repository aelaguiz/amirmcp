"""Which coding sessions exist, where they run, and what state they are in.

The pane-to-session link always comes from the running process, never from a
guess by directory, because Amir often runs several agents in the same repo:

* Claude Code writes ``<home>/sessions/<pid>.json`` for each running process,
  naming its session id and whether it is busy or idle. Each AI Manager account
  has its own home under ``~/.aimgr/claude-homes/<account>/.claude``.
* Codex logs every line with ``pid:<pid>:<uuid>`` and the thread it belongs to in
  ``~/.codex/logs_*.sqlite``; ``state_*.sqlite`` maps a thread to its rollout
  file. All Codex accounts share that one home.

Sessions that are no longer running but were active recently are listed too, so
"what happened with X this morning" has an answer.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import herdr, transcripts

CLAUDE_HOMES_GLOB = os.path.expanduser("~/.aimgr/claude-homes/*/.claude")
DEFAULT_CLAUDE_HOME = os.path.expanduser("~/.claude")
CODEX_HOME = os.path.expanduser("~/.codex")
AIMGR_STATE = os.path.expanduser("~/.aimgr/local-state.json")

QUESTION_TOOLS = ("AskUserQuestion", "ExitPlanMode")


def local_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def ago(epoch: float | None, now: float) -> str:
    if epoch is None:
        return "unknown"
    minutes = int((now - epoch) // 60)
    if minutes < 1:
        return "under a minute ago"
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min ago"
    return f"{hours // 24} days ago"


@dataclass
class Session:
    runtime: str  # "claude" or "codex"
    session_id: str | None
    transcript: str | None
    account: str | None
    cwd: str
    title: str
    branch: str | None = None
    pid: int | None = None
    pane: herdr.Pane | None = None
    state: str = "unknown"
    state_reason: str = ""
    last_activity: float | None = None
    first_request: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.session_id:
            return f"{self.runtime}:{self.session_id}"
        return f"{self.runtime}:pane:{self.pane.session}:{self.pane.pane_id}" if self.pane else f"{self.runtime}:pid:{self.pid}"

    @property
    def short_id(self) -> str:
        # Claude ids are random from the first character; Codex ids are
        # time-ordered, so threads started the same minute share a prefix and
        # only the tail tells them apart.
        if self.session_id:
            part = self.session_id[:8] if self.runtime == "claude" else self.session_id[-8:]
            return f"{self.runtime}:{part}"
        return self.key

    def labels(self) -> list[str]:
        out = [self.title, self.cwd, os.path.basename(self.cwd), self.account or "", self.first_request or ""]
        if self.pane:
            out += [self.pane.session, self.pane.workspace_label, self.pane.tab_label, self.pane.title, self.pane.pane_id]
        return [o for o in out if o]

    def as_dict(self, now: float) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.short_id,
            "full_id": self.key,
            "runtime": self.runtime,
            "account": self.account,
            "title": self.title,
            "project_dir": self.cwd,
            "branch": self.branch,
            "where": self.pane.location() if self.pane else "not in a Herdr pane",
            "state": self.state,
            "state_reason": self.state_reason,
            "last_activity": local_iso(self.last_activity),
            "last_activity_ago": ago(self.last_activity, now),
            "transcript": self.transcript,
        }
        if self.first_request and self.first_request != self.title:
            d["first_request"] = self.first_request[:300]
        if self.notes:
            d["notes"] = self.notes
        return d


# ------------------------------------------------------------------ Claude


def claude_homes() -> list[str]:
    homes = sorted(glob.glob(CLAUDE_HOMES_GLOB))
    if os.path.isdir(DEFAULT_CLAUDE_HOME):
        homes.append(DEFAULT_CLAUDE_HOME)
    return homes


def account_of(home: str) -> str:
    parts = home.rstrip("/").split("/")
    if len(parts) >= 3 and parts[-3] == "claude-homes":
        return parts[-2]
    return "default ~/.claude"


def claude_process_records() -> dict[int, tuple[str, dict[str, Any]]]:
    """pid -> (claude home, the session record Claude Code keeps for that pid)."""
    found: dict[int, tuple[str, dict[str, Any]]] = {}
    for home in claude_homes():
        for path in glob.glob(os.path.join(home, "sessions", "*.json")):
            try:
                with open(path) as fh:
                    record = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and isinstance(record.get("pid"), int):
                found[record["pid"]] = (home, record)
    return found


def claude_transcript(home: str, session_id: str) -> str | None:
    matches = glob.glob(os.path.join(home, "projects", "*", f"{session_id}.jsonl"))
    return matches[0] if matches else None


def _read_edges(path: str, head: int = 64 * 1024, tail: int = 256 * 1024) -> list[dict[str, Any]]:
    """Parsed records from the start and end of a transcript (titles, branch)."""
    size = os.path.getsize(path)
    chunks = []
    with open(path, "rb") as fh:
        chunks.append(fh.read(min(head, size)))
        if size > head:
            fh.seek(max(head, size - tail))
            chunks.append(fh.read())
    records = []
    for chunk in chunks:
        for raw in chunk.split(b"\n"):
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def claude_details(path: str) -> dict[str, Any]:
    title = None
    branch = None
    first = None
    for rec in _read_edges(path):
        rtype = rec.get("type")
        if rtype == "custom-title" and rec.get("customTitle"):
            title = rec["customTitle"]
        elif rtype == "ai-title" and rec.get("aiTitle") and not title:
            title = rec["aiTitle"]
        if rec.get("gitBranch"):
            branch = rec["gitBranch"]
        if first is None:
            for e in transcripts.claude_entries(rec, 0):
                if e.kind == "user":
                    first = e.text
                    break
    return {"title": title, "branch": branch, "first_request": first}


def _pending_question(path: str) -> str | None:
    """The question the agent asked Amir, if its last tool call is one still unanswered."""
    entries, _ = transcripts.tail_entries(path, "claude", 6)
    for entry in reversed(entries):
        if entry.kind == "result":
            return None
        if entry.kind == "tool":
            return entry.text if entry.text.startswith(QUESTION_TOOLS) else None
    return None


def claude_session(home: str, record: dict[str, Any], pane: herdr.Pane | None, now: float) -> Session:
    session_id = record.get("sessionId")
    path = claude_transcript(home, session_id) if session_id else None
    details = claude_details(path) if path else {}
    s = Session(
        runtime="claude",
        session_id=session_id,
        transcript=path,
        account=account_of(home),
        cwd=record.get("cwd") or (pane.cwd if pane else ""),
        title=details.get("title") or (pane.title if pane else "") or record.get("name") or "",
        branch=details.get("branch"),
        pid=record.get("pid"),
        pane=pane,
        first_request=details.get("first_request"),
    )
    s.last_activity = os.path.getmtime(path) if path else None
    status = record.get("status")
    status_since = (record.get("statusUpdatedAt") or record.get("updatedAt") or 0) / 1000 or None
    question = _pending_question(path) if path else None
    if question:
        s.state, s.state_reason = "blocked", f"Waiting for Amir to answer: {question}"
    elif status == "waiting":
        waiting_for = record.get("waitingFor") or "Amir's input"
        s.state, s.state_reason = "blocked", f"Waiting on {waiting_for} since {ago(status_since, now)}"
    elif status == "busy":
        s.state, s.state_reason = "working", f"Claude reports busy since {ago(status_since, now)}"
    elif status == "shell":
        # Claude's own name for "idle at the prompt, but background jobs it
        # started are still running"; it resumes when they report back.
        s.state, s.state_reason = "working", f"Turn ended {ago(status_since, now)}, but background jobs it started are still running"
    elif status == "idle":
        s.state, s.state_reason = "idle", f"Finished its turn and is waiting at the prompt ({ago(status_since, now)})"
    else:
        s.state, s.state_reason = "unknown", f"Claude reports status {status!r}"
    if not path:
        s.notes.append("No transcript file found for this session yet.")
    return s


# ------------------------------------------------------------------- Codex


def _latest_db(prefix: str) -> str | None:
    found = sorted(glob.glob(os.path.join(CODEX_HOME, f"{prefix}_*.sqlite")))
    return found[-1] if found else None


def _connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)


def codex_account_labels() -> dict[str, str]:
    """Codex account id -> AI Manager label, from AI Manager's selection history."""
    try:
        with open(AIMGR_STATE) as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    labels: dict[str, str] = {}
    for item in ((state.get("pool") or {}).get("openaiCodex") or {}).get("history") or []:
        if item.get("accountId") and item.get("label"):
            labels[item["accountId"]] = item["label"]
    current = (state.get("targets") or {}).get("codexCli") or {}
    if current.get("expectedAccountId") and current.get("activeLabel"):
        labels[current["expectedAccountId"]] = current["activeLabel"]
    return labels


def codex_thread_for_pid(pid: int) -> str | None:
    """The top-level thread a running Codex process is working on."""
    logs, state = _latest_db("logs"), _latest_db("state")
    if not logs or not state:
        return None
    with _connect(logs) as db:
        row = db.execute(
            "select process_uuid from logs where process_uuid >= ? and process_uuid < ? order by ts desc limit 1",
            (f"pid:{pid}:", f"pid:{pid};"),
        ).fetchone()
        if not row:
            return None
        row = db.execute(
            "select thread_id from logs where process_uuid = ? and thread_id is not null order by ts desc limit 1",
            (row[0],),
        ).fetchone()
    if not row:
        return None
    thread = row[0]
    with _connect(state) as db:
        for _ in range(10):  # walk subagent threads up to the one Amir started
            parent = db.execute(
                "select parent_thread_id from thread_spawn_edges where child_thread_id = ?", (thread,)
            ).fetchone()
            if not parent or not parent[0]:
                break
            thread = parent[0]
    return thread


def codex_threads(where: str, params: tuple) -> list[dict[str, Any]]:
    state = _latest_db("state")
    if not state:
        return []
    with _connect(state) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "select id, rollout_path, cwd, title, git_branch, first_user_message, updated_at_ms, thread_source "
            f"from threads where {where}",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _codex_creator(path: str) -> str | None:
    with open(path, "rb") as fh:
        try:
            first = json.loads(fh.readline())
        except json.JSONDecodeError:
            return None
    return (first.get("payload") or {}).get("creator_account_id")


def _codex_turn_open(path: str) -> bool | None:
    """True if the last turn started and has not finished, None if unknown."""
    entries, _ = transcripts.tail_entries(path, "codex", 200)
    for entry in reversed(entries):
        if entry.kind == "event" and entry.text == "turn finished":
            return False
        if entry.kind == "event" and entry.text.startswith(("turn started",)):
            return True
        if entry.kind == "event" and entry.text.startswith("turn aborted"):
            return False
    return None


def codex_session(thread: dict[str, Any] | None, pid: int | None, pane: herdr.Pane | None, labels: dict[str, str], now: float) -> Session:
    if thread is None:
        s = Session(
            runtime="codex",
            session_id=None,
            transcript=None,
            account=None,
            cwd=pane.cwd if pane else "",
            title="Codex session with no request yet",
            pid=pid,
            pane=pane,
        )
        s.state, s.state_reason = "idle", "Codex is open at its prompt and has not started a conversation"
        return s
    path = thread["rollout_path"]
    exists = bool(path) and os.path.exists(path)
    title = (thread.get("title") or thread.get("first_user_message") or "").strip()
    s = Session(
        runtime="codex",
        session_id=thread["id"],
        transcript=path if exists else None,
        account=labels.get(_codex_creator(path) or "") if exists else None,
        cwd=thread.get("cwd") or (pane.cwd if pane else ""),
        title=title[:200],
        branch=thread.get("git_branch"),
        pid=pid,
        pane=pane,
        first_request=(thread.get("first_user_message") or "")[:2000] or None,
    )
    s.last_activity = os.path.getmtime(path) if exists else (thread.get("updated_at_ms") or 0) / 1000 or None
    turn_open = _codex_turn_open(path) if exists else None
    herdr_status = pane.herdr_status if pane else None
    if herdr_status == "blocked":
        s.state, s.state_reason = "blocked", "Herdr sees Codex waiting on an approval or question"
    elif herdr_status == "working" or (turn_open and herdr_status not in ("idle", "done")):
        s.state, s.state_reason = "working", f"Turn in progress; last write {ago(s.last_activity, now)}"
    elif herdr_status in ("idle", "done") or turn_open is False:
        s.state, s.state_reason = "idle", f"Finished its turn and is waiting at the prompt (last write {ago(s.last_activity, now)})"
    else:
        s.state, s.state_reason = "unknown", "Could not tell from Herdr or the rollout whether a turn is running"
    if not exists:
        s.notes.append("Rollout file listed in Codex's index is missing on disk.")
    return s


# ---------------------------------------------------------------- snapshot


def _is_claude(proc: dict[str, Any]) -> bool:
    argv0 = (proc.get("argv") or [""])[0]
    return "/claude/versions/" in argv0 or proc.get("name") == "claude"


def _is_codex(proc: dict[str, Any]) -> bool:
    return proc.get("name") == "codex"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_env(pid: int) -> dict[str, str]:
    """Another process's environment, where the OS lets us read it (Linux)."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _ssh_client(pid: int) -> str | None:
    """``ip:port`` of the SSH client whose login this process runs under."""
    conn = _process_env(pid).get("SSH_CONNECTION", "").split()
    return f"{conn[0]}:{conn[1]}" if len(conn) >= 2 else None


def _codex_pids() -> list[int]:
    proc = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=10)
    pids = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1].strip()) == "codex":
            pids.append(int(parts[0]))
    return pids


@dataclass
class Snapshot:
    sessions: list[Session]
    gaps: list[str]
    observed_at: float
    # Panes here whose foreground is an SSH session to another machine; the
    # agents in them run there and are covered by that machine's connector.
    ssh_panes: list[dict[str, Any]] = field(default_factory=list)


def snapshot(recent_hours: float = 24) -> Snapshot:
    now = time.time()
    panes, gaps = herdr.all_panes() if herdr.available() else ([], [])
    # Panes in another machine's Herdr (Amir's Mac) that SSH into this one,
    # keyed by the SSH client address an agent here sees in SSH_CONNECTION.
    via_ssh: dict[str, herdr.Pane] = {}
    for host in [h.strip() for h in os.environ.get("AMIRMCP_HERDR_HOSTS", "").split(",") if h.strip()]:
        remote, errors = herdr.remote_panes(host)
        gaps.extend(errors)
        for pane in remote:
            for proc in pane.processes:
                for addr in proc.get("tcp_local") or []:
                    via_ssh[addr] = pane
    claude_records = claude_process_records()
    labels = codex_account_labels()
    sessions: list[Session] = []
    ssh_panes: list[dict[str, Any]] = []
    seen_claude_pids: set[int] = set()
    seen_codex_pids: set[int] = set()
    seen_ids: set[str] = set()

    for pane in panes:
        for proc in pane.processes:
            pid = proc.get("pid")
            if _is_claude(proc):
                if pid in claude_records:
                    home, record = claude_records[pid]
                    sessions.append(claude_session(home, record, pane, now))
                    seen_claude_pids.add(pid)
                else:
                    gaps.append(f"Claude process {pid} in {pane.session} {pane.pane_id} has no session record.")
            elif _is_codex(proc) and pid:
                sessions.append(_codex_for_pid(pid, pane, labels, now))
                seen_codex_pids.add(pid)
            elif proc.get("name") == "ssh":
                ssh_panes.append({"where": pane.location(), "command": proc.get("cmdline") or "ssh"})
    seen_ids.update(s.key for s in sessions)

    # Agents running outside this machine's Herdr: in an SSH session from
    # another machine's Herdr pane, or headless (routines, other terminals).
    for pid, (home, record) in claude_records.items():
        if pid in seen_claude_pids or not _pid_alive(pid):
            continue
        pane = via_ssh.get(_ssh_client(pid) or "")
        s = claude_session(home, record, pane, now)
        if s.key not in seen_ids:
            if pane is None:
                s.notes.append("Running, but not in any Herdr pane.")
            sessions.append(s)
            seen_ids.add(s.key)
    for pid in _codex_pids():
        if pid in seen_codex_pids:
            continue
        pane = via_ssh.get(_ssh_client(pid) or "")
        if pane is None and not codex_thread_for_pid(pid):
            # Outside a pane, a Codex process with no conversation is plumbing
            # (the ChatGPT app's app-server and exec-server), not Amir's work.
            continue
        s = _codex_for_pid(pid, pane, labels, now)
        if s.key not in seen_ids:
            if pane is None:
                s.notes.append("Running, but not in any Herdr pane.")
            sessions.append(s)
            seen_ids.add(s.key)

    # Recently active sessions with no running process.
    cutoff = now - recent_hours * 3600
    for home in claude_homes() if recent_hours > 0 else []:
        for path in glob.glob(os.path.join(home, "projects", "*", "*.jsonl")):
            mtime = os.path.getmtime(path)
            session_id = os.path.basename(path)[: -len(".jsonl")]
            if mtime < cutoff or f"claude:{session_id}" in seen_ids:
                continue
            if os.path.exists(path[: -len(".jsonl")] + ".aimgr-staged-fork"):
                continue
            details = claude_details(path)
            s = Session(
                runtime="claude",
                session_id=session_id,
                transcript=path,
                account=account_of(home),
                cwd=_claude_cwd(path) or "",
                title=details.get("title") or (details.get("first_request") or "")[:120],
                branch=details.get("branch"),
                first_request=details.get("first_request"),
                last_activity=mtime,
                state="stopped",
                state_reason=f"No Claude process is running this session; last written {ago(mtime, now)}",
            )
            sessions.append(s)
            seen_ids.add(s.key)
    stopped_threads = codex_threads("updated_at_ms >= ? and thread_source = 'user'", (int(cutoff * 1000),)) if recent_hours > 0 else []
    for thread in stopped_threads:
        if f"codex:{thread['id']}" in seen_ids:
            continue
        s = codex_session(thread, None, None, labels, now)
        s.state = "stopped"
        s.state_reason = f"No Codex process is running this thread; last written {ago(s.last_activity, now)}"
        sessions.append(s)
        seen_ids.add(s.key)

    order = {"blocked": 0, "working": 1, "idle": 2, "unknown": 3, "stopped": 4}
    sessions.sort(key=lambda s: (order.get(s.state, 5), -(s.last_activity or 0)))
    return Snapshot(sessions=sessions, gaps=gaps, observed_at=now, ssh_panes=ssh_panes)


def _codex_for_pid(pid: int, pane: herdr.Pane | None, labels: dict[str, str], now: float) -> Session:
    thread_id = codex_thread_for_pid(pid)
    rows = codex_threads("id = ?", (thread_id,)) if thread_id else []
    return codex_session(rows[0] if rows else None, pid, pane, labels, now)


def _claude_cwd(path: str) -> str | None:
    for rec in _read_edges(path, head=32 * 1024, tail=0):
        if rec.get("cwd"):
            return rec["cwd"]
    return None


def find(snap: Snapshot, ref: str) -> list[Session]:
    """Sessions matching an id, id prefix, pane id, or loose name."""
    ref = ref.strip()
    low = ref.lower()
    exact = [
        s
        for s in snap.sessions
        if low in (s.key.lower(), s.short_id.lower(), (s.session_id or "").lower())
        or (s.pane and low in (s.pane.pane_id.lower(), f"{s.pane.session}:{s.pane.pane_id}".lower()))
    ]
    if exact:
        return exact
    bare = low.split(":")[-1]
    prefix = [
        s
        for s in snap.sessions
        if s.session_id and len(bare) >= 6 and (s.session_id.lower().startswith(bare) or s.session_id.lower().endswith(bare))
    ]
    if prefix:
        return prefix
    words = [w for w in low.replace("/", " ").split() if w]
    scored = []
    for s in snap.sessions:
        hay = " ".join(s.labels()).lower()
        hits = sum(1 for w in words if w in hay)
        if hits:
            scored.append((hits, s))
    if not scored:
        return []
    best = max(h for h, _ in scored)
    return [s for h, s in scored if h == best]
