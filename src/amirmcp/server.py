"""The MCP server Amir's dot connects to.

Most tools only read: Herdr's pane list, the agents' own session records, and
their transcript files, with summaries from Claude Haiku 5.5. Three let the dot
act for Amir in an agent's pane: send_message types a message into the agent's
input box, press_keys answers a prompt, and read_screen shows what the pane
shows. They never start, stop or restart agents, and never change a file.

The public endpoint is ``https://<host>/<AMIRMCP_SECRET>/mcp``. Requests whose
path does not start with the secret get a bare 404. That one secret is the whole
access control, as the capability brief asked. A second secret,
``AMIRMCP_READ_SECRET``, serves the same reading tools without the three that
act, for readers Amir has not trusted to type into his agents (Grok Bot).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.applications import Starlette

from . import control, sessions, summarize, transcripts
from .sessions import Session, Snapshot, local_iso

log = logging.getLogger("amirmcp")


def load_env_file(path: str) -> None:
    """KEY=value lines from the repo's .env, without overriding the environment."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Machine settings (secret, key, machine name, Herdr hosts) live in the repo's
# .env, read before the server's description is built.
load_env_file(os.environ.get("AMIRMCP_ENV_FILE") or os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

MACHINE = os.environ.get("AMIRMCP_MACHINE") or socket.gethostname().split(".")[0]

LOG_FILE = os.path.expanduser(
    "~/Library/Logs/amirmcp/amirmcp.log" if sys.platform == "darwin" else "~/.local/state/amirmcp/amirmcp.log"
)

READ_INSTRUCTIONS = f"""\
Amir's coding agents on {MACHINE}: every Claude Code and Codex session running \
there, with the Herdr pane each one is in, plus ones that stopped recently. Each \
of Amir's machines has its own Coding sessions plugin; ask each one when he asks \
about all his work. For "how's X going", call summarize_session; for "what's going \
on" or "what needs me", call overview; list_sessions finds ids. States: working, \
blocked (waiting on Amir), idle (finished its turn; not necessarily done), stopped \
(no process), unknown. Cursors like c123 point into a transcript; cite them and \
check details with read_history or search_history.\
"""
INSTRUCTIONS = (
    READ_INSTRUCTIONS
    + " When Amir wants to tell an agent something, send_message types it into the agent's input box as his"
    " words; when an agent is showing a question or approval, read_screen shows it and press_keys answers it."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
SENDS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
PRESSES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

mcp = MCPServer("amirmcp", title=f"Coding sessions on {MACHINE}", instructions=INSTRUCTIONS)
# The same reading tools without send_message and press_keys, served under the
# read secret.
mcp_read = MCPServer("amirmcp", title=f"Coding sessions on {MACHINE}", instructions=READ_INSTRUCTIONS)


def read_tool(fn):
    """Register a tool that only reads on both servers."""
    for server in (mcp, mcp_read):
        server.tool(annotations=READ_ONLY)(fn)
    return fn

_snap_lock = threading.Lock()
_snaps: dict[bool, Snapshot] = {}
SNAPSHOT_TTL = 5.0


def _snapshot(include_stopped: bool) -> Snapshot:
    """A fresh view of the sessions, reused for a few seconds across calls."""
    with _snap_lock:
        snap = _snaps.get(include_stopped)
        if snap and time.time() - snap.observed_at < SNAPSHOT_TTL:
            return snap
        snap = sessions.snapshot(recent_hours=72 if include_stopped else 0)
        _snaps[include_stopped] = snap
        return snap


def _candidates(found: list[Session], now: float) -> list[dict[str, Any]]:
    return [
        {"id": s.short_id, "title": s.title, "state": s.state, "where": s.pane.location() if s.pane else "not in a Herdr pane"}
        for s in found[:15]
    ]


def _resolve(ref: str) -> tuple[Session, Snapshot]:
    """One session for an id or a loose name, looking at live sessions first."""
    for include_stopped in (False, True):
        snap = _snapshot(include_stopped)
        found = sessions.find(snap, ref)
        if len(found) == 1:
            return found[0], snap
        if len(found) > 1:
            raise ToolError(
                f"{len(found)} sessions match {ref!r}; call again with one of these ids: "
                + "; ".join(f"{c['id']} ({c['title'][:80]}, {c['state']})" for c in _candidates(found, snap.observed_at))
            )
    raise ToolError(f"No session matches {ref!r}. Call list_sessions to see ids and names.")


def _need_transcript(s: Session) -> str:
    if not s.transcript:
        raise ToolError(f"{s.short_id} has no transcript yet ({s.state_reason}).")
    return s.transcript


@read_tool
def list_sessions(query: str | None = None, include_stopped: bool = False) -> dict[str, Any]:
    """List Amir's coding sessions: each Claude Code or Codex agent, which Herdr
    session, workspace and pane it runs in, its AI Manager account, project
    directory, title, live state, and when it last wrote.

    query: optional loose name to filter by ("seo", "psagentspace bugs", a pane
    id like wH2:p1, or an id). Matching also looks at sessions that stopped in
    the last 72 hours.
    include_stopped: also list sessions with no running process that were active
    in the last 72 hours (often many automated runs).
    """
    log.info("tool list_sessions query=%r include_stopped=%s", query, include_stopped)
    snap = _snapshot(include_stopped or bool(query))
    found = sessions.find(snap, query) if query else [s for s in snap.sessions if include_stopped or s.state != "stopped"]
    counts: dict[str, int] = {}
    for s in snap.sessions:
        counts[s.state] = counts.get(s.state, 0) + 1
    out = {
        "machine": MACHINE,
        "observed_at": local_iso(snap.observed_at),
        "counts_by_state": counts,
        "sessions": [s.as_dict(snap.observed_at) for s in found],
        "gaps": snap.gaps,
    }
    if snap.ssh_panes and not query:
        out["panes_running_ssh"] = {
            "note": "These panes hold SSH sessions into other machines; agents there are listed by that machine's Coding sessions plugin.",
            "panes": snap.ssh_panes,
        }
    return out


@read_tool
def get_session(session: str, recent: int = 15) -> dict[str, Any]:
    """Everything about one session right now: where it runs, live state and the
    reason, Amir's requests in it, and its most recent transcript entries.

    session: an id from list_sessions (e.g. claude:d8e7a6ac) or a loose name.
    recent: how many recent transcript entries to include (max 100).
    """
    log.info("tool get_session session=%r", session)
    s, snap = _resolve(session)
    out: dict[str, Any] = {"observed_at": local_iso(snap.observed_at), "session": s.as_dict(snap.observed_at)}
    if s.transcript:
        entries, more = transcripts.tail_entries(s.transcript, s.runtime, min(max(recent, 1), 100))
        out["requests"] = [e.as_dict() for e in transcripts.user_requests(s.transcript, s.runtime)[-10:]]
        out["recent"] = [e.as_dict() for e in entries]
        out["earlier_history"] = more
    return out


@read_tool
def read_history(
    session: str,
    after_cursor: int | None = None,
    before_cursor: int | None = None,
    limit: int = 40,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    """Read a bounded slice of one session's transcript, oldest first.

    With no cursor: the last `limit` entries. after_cursor: entries after that
    point (use next_cursor from a previous call to follow along). before_cursor:
    the `limit` entries before that point (page backwards).
    kinds: keep only these entry kinds: user, assistant, tool, result, event.
    Each entry has a cursor, local timestamp, kind and shortened text.
    """
    log.info("tool read_history session=%r after=%s before=%s", session, after_cursor, before_cursor)
    s, snap = _resolve(session)
    path = _need_transcript(s)
    limit = min(max(limit, 1), 200)
    if after_cursor is not None:
        entries, next_cursor, more = transcripts.entries_after(path, s.runtime, after_cursor, limit)
        result = {"entries": entries, "next_cursor": next_cursor, "more_after": more}
    else:
        entries, more = transcripts.tail_entries(path, s.runtime, limit, before=before_cursor)
        result = {"entries": entries, "more_before": more, "next_cursor": os.path.getsize(path)}
    wanted = set(kinds) if kinds else None
    result["entries"] = [e.as_dict() for e in result["entries"] if wanted is None or e.kind in wanted]
    return {"session": s.short_id, "state": s.state, "transcript": path, **result}


@read_tool
def search_history(query: str, session: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Find a task, error, decision, test, PR or file in transcripts, with the
    entry just before and after each hit for context.

    query: plain text, case-insensitive.
    session: search one session (id or name); omit to search every live session
    and every session active in the last 72 hours.
    """
    log.info("tool search_history query=%r session=%r", query, session)
    limit = min(max(limit, 1), 60)
    if session:
        s, _ = _resolve(session)
        targets = [s]
    else:
        targets = [s for s in _snapshot(True).sessions if s.transcript]
    results = []
    for s in targets:
        hits = transcripts.search(s.transcript, s.runtime, query, limit - len(results))
        for hit in hits:
            results.append({"session": s.short_id, "title": s.title, **hit})
        if len(results) >= limit:
            break
    return {"query": query, "searched_sessions": len(targets), "hits": results, "truncated": len(results) >= limit}


@read_tool
def summarize_session(session: str, refresh: bool = False) -> dict[str, Any]:
    """A status brief for one session, written by Claude Haiku 5.5 from its live
    state, Amir's requests and recent transcript: status, objective, progress
    (marked said / did / verified, with cursors), latest result, remaining work,
    what it needs from Amir, blockers, links, and what could not be verified.

    session: an id from list_sessions or a loose name ("the SEO plan").
    refresh: ignore a cached brief. Briefs are cached until the transcript or
    state changes, so this is rarely needed.
    """
    log.info("tool summarize_session session=%r refresh=%s", session, refresh)
    s, snap = _resolve(session)
    out = summarize.brief(s, snap.observed_at, refresh=refresh)
    return {"where": s.pane.location() if s.pane else "not in a Herdr pane", "state": s.state, "state_reason": s.state_reason, **out}


@read_tool
def overview(query: str | None = None) -> dict[str, Any]:
    """One line per running session on what it is doing and whether it needs
    Amir, sessions needing him first. Uses cached Haiku briefs where nothing
    changed; the first call can take a minute.

    query: optional loose name to narrow it ("growth", "bugs", "cratejoy").
    """
    log.info("tool overview query=%r", query)
    snap = _snapshot(False)
    targets = sessions.find(snap, query) if query else [s for s in snap.sessions if s.state != "stopped"]
    targets = [s for s in targets if s.transcript]

    def one(s: Session) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": s.short_id,
            "title": s.title,
            "where": s.pane.location() if s.pane else "not in a Herdr pane",
            "state": s.state,
            "last_activity_ago": sessions.ago(s.last_activity, snap.observed_at),
        }
        try:
            b = summarize.brief(s, snap.observed_at, window_chars=40_000)
        except Exception as exc:  # one failed brief should not sink the overview
            log.exception("brief failed for %s", s.short_id)
            row["error"] = f"brief failed: {exc}"
            return row
        if "brief" in b:
            row["status"] = b["brief"]["status"]
            row["needs_amir"] = b["brief"]["needs_amir"]
        else:
            row["error"] = b.get("error")
        return row

    with ThreadPoolExecutor(max_workers=48) as pool:
        rows = list(pool.map(one, targets))

    def needs(row: dict[str, Any]) -> bool:
        text = (row.get("needs_amir") or "").strip().lower()
        return bool(text) and not text.startswith(("nothing", "none", "no "))

    rows.sort(key=lambda r: (not needs(r), r["state"] != "blocked", r["state"] != "working"))
    return {
        "machine": MACHINE,
        "observed_at": local_iso(snap.observed_at),
        "sessions": rows,
        "needs_amir_count": sum(1 for r in rows if needs(r)),
        "gaps": snap.gaps,
    }


# ------------------------------------------------------- acting in a pane

CONFIRM_SECONDS = 10


def _in_foreground(pid: int) -> bool:
    """Whether a process is in the foreground of its own terminal (here, an SSH login)."""
    out = subprocess.run(["ps", "-o", "pgid=,tpgid=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.split()
    return len(out) == 2 and out[0] == out[1]


def _target(s: Session) -> tuple[str | None, control.Target]:
    """Where to type for this session, after checking the agent is still there."""
    if s.state == "stopped" or not s.pid:
        raise ToolError(f"{s.short_id} is not running ({s.state_reason}); there is nothing to type into.")
    if not s.pane:
        raise ToolError(f"{s.short_id} is not in a Herdr pane, so there is no screen to type into.")
    if s.pane.herdr_host:
        client = sessions._ssh_client(s.pid)
        if not client or not _in_foreground(s.pid):
            raise ToolError(f"{s.short_id} is no longer in the front of its SSH session on {MACHINE}; nothing was typed.")
        return s.pane.herdr_host, control.Target(s.pane.session, s.pane.pane_id, s.runtime, ssh_client=client)
    return None, control.Target(s.pane.session, s.pane.pane_id, s.runtime, pid=s.pid)


def _fresh(ref: str) -> tuple[Session, Snapshot]:
    """Resolve a session from a new snapshot: about to type, so nothing cached."""
    with _snap_lock:
        _snaps.clear()
    s, snap = _resolve(ref)
    if s.state == "stopped":
        raise ToolError(f"{s.short_id} is not running ({s.state_reason}); there is nothing to type into.")
    return s, snap


def _transcript_now(s: Session) -> str | None:
    if s.transcript or s.runtime != "claude" or not s.session_id:
        return s.transcript
    for home in sessions.claude_homes():  # a new session writes its file on the first message
        found = sessions.claude_transcript(home, s.session_id)
        if found:
            return found
    return None


def _squash(text: str) -> str:
    return " ".join(text.split())


def _arrived(s: Session, cursor: int, text: str) -> transcripts.Entry | None:
    path = _transcript_now(s)
    if not path:
        return None
    snippet = _squash(text)[:60]
    entries, _, _ = transcripts.entries_after(path, s.runtime, cursor, 100)
    return next((e for e in entries if e.kind == "user" and snippet in _squash(e.text)), None)


def _tail_rows(screen: str, n: int = 25) -> str:
    rows = [r.rstrip() for r in screen.split("\n")]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[-n:])


def _pane_result(reply: dict[str, Any]) -> dict[str, Any]:
    if reply.get("ok"):
        return reply
    if reply.get("refused"):
        return {"result": "refused", "reason": reply["refused"], "screen": _tail_rows(reply.get("screen", ""))}
    raise ToolError(reply.get("error") or "Unknown error reaching the pane.")


@read_tool
def read_screen(session: str) -> dict[str, Any]:
    """What one agent's Herdr pane shows right now, as text, and whether its
    input box is ready for a message. Use it to see a question, menu, approval
    or error the agent is showing before answering it with press_keys, or to
    check what a message did.

    session: an id from list_sessions or a loose name.
    """
    log.info("tool read_screen session=%r", session)
    s, snap = _fresh(session)
    host, target = _target(s)
    reply = _pane_result(control.run_on(host, {"op": "screen", "target": target.__dict__}))
    return {
        "session": s.short_id,
        "where": s.pane.location() if s.pane else None,
        "state": s.state,
        "state_reason": s.state_reason,
        "input_box": reply.get("input_box"),
        "unsent_text": reply.get("draft") or None,
        "screen": _tail_rows(reply.get("screen", ""), 60),
    }


@mcp.tool(annotations=SENDS)
def send_message(session: str, text: str) -> dict[str, Any]:
    """Send Amir's message to a coding agent, typed into its input box as if he
    had typed it at his desk, prefixed "From Amir, via ChatGPT:". The agent
    takes it as his words and acts on it, so send what Amir asked you to send,
    and say it the way he would.

    It works whether the agent is idle or mid-turn (a mid-turn message is read
    at the agent's next step). A Codex agent that asked a question takes the
    message as the answer. It refuses, without typing anything, when the agent
    is showing a prompt or menu (answer that with press_keys after read_screen),
    when Amir has unsent text in the input box, or when the agent is no longer
    running in its pane.

    Result: "delivered" when the agent's own transcript shows the message
    (with its cursor), "queued" when the screen shows it waiting for the
    agent's next step, "refused" with the reason and screen, or "not
    confirmed" with the screen. Never resend because a result was not
    confirmed; read the screen and history first, since a second copy would
    reach the agent too.

    session: an id from list_sessions or a loose name.
    text: the message. Several lines and long messages are fine.
    """
    log.info("tool send_message session=%r chars=%d", session, len(text))
    if not text.strip():
        raise ToolError("The message is empty.")
    s, snap = _fresh(session)
    host, target = _target(s)
    if s.state == "blocked" and not (s.runtime == "codex" and s.transcript and sessions._codex_pending_question(s.transcript)):
        reply = control.run_on(host, {"op": "screen", "target": target.__dict__})
        return {
            "session": s.short_id,
            "result": "refused",
            "reason": f"The agent is waiting on a prompt ({s.state_reason}). Answer it with press_keys; nothing was typed.",
            "screen": _tail_rows(reply.get("screen", "")),
        }
    path = _transcript_now(s)
    # entries_after skips the line its cursor is on, so point at the last byte
    # already written: the next line is the first one checked.
    cursor = max(os.path.getsize(path) - 1, 0) if path else 0
    reply = _pane_result(control.run_on(host, {"op": "send", "target": target.__dict__, "text": text}))
    if reply.get("result") == "refused":
        return {"session": s.short_id, **reply}
    deadline = time.time() + CONFIRM_SECONDS
    while time.time() < deadline:
        time.sleep(1)
        entry = _arrived(s, cursor, text)
        if entry:
            log.info("send_message delivered to %s at c%d", s.short_id, entry.cursor)
            return {"session": s.short_id, "result": "delivered", "cursor": f"c{entry.cursor}", "at": transcripts.local_ts(entry.ts)}
    look = control.run_on(host, {"op": "screen", "target": target.__dict__})
    screen = look.get("screen", "")
    lead = control.LEAD_IN.strip()
    if look.get("input_box") == "draft" and lead in (look.get("draft") or ""):
        result, detail = "not confirmed", "The message is in the input box but was not submitted."
    elif lead in screen and look.get("input_box") == "ready":
        result, detail = "queued", "The agent is mid-step; the screen shows the message waiting for its next step."
    else:
        result, detail = "not confirmed", f"Typed, but the transcript does not show it after {CONFIRM_SECONDS} s."
    log.info("send_message %s to %s", result, s.short_id)
    return {"session": s.short_id, "result": result, "detail": detail, "screen": _tail_rows(screen)}


@mcp.tool(annotations=PRESSES)
def press_keys(session: str, keys: list[str], text: str | None = None) -> dict[str, Any]:
    """Press keys in an agent's pane, then optionally type text and press
    Enter, to answer a question, menu, permission or approval prompt the way
    Amir would at his keyboard, or to stop a turn with esc. Read the screen
    first (read_screen) so you know what each key will do, and press only what
    Amir chose. Returns the screen afterwards.

    Typical answers: a Claude question or approval takes the option's number
    ("2"); its "Type something" option takes that option's number and then
    text; a Claude review screen takes "1" to submit. A Codex command approval
    takes "y", "p" or "esc". Menus also take "up", "down" and "enter". A
    Codex question needs no keys: send_message answers it.

    session: an id from list_sessions or a loose name.
    keys: key names in order, e.g. ["2"], ["down", "enter"], ["esc"],
    ["shift+tab"]. May be empty when only text is needed.
    text: typed after the keys, followed by Enter.
    """
    log.info("tool press_keys session=%r keys=%r text_chars=%d", session, keys, len(text or ""))
    if not keys and not text:
        raise ToolError("Give keys, text, or both.")
    s, snap = _fresh(session)
    host, target = _target(s)
    reply = _pane_result(control.run_on(host, {"op": "keys", "target": target.__dict__, "keys": keys, "text": text}))
    if reply.get("result") == "refused":
        return {"session": s.short_id, **reply}
    return {"session": s.short_id, "pressed": keys, "typed": bool(text), "screen": _tail_rows(reply.get("screen", ""), 40)}


class SecretPath:
    """Serve each MCP app only under /<its secret>/; everything else is a 404."""

    def __init__(self, apps: dict[str, Any]):
        self.apps = {f"/{secret}": app for secret, app in apps.items()}

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        for prefix, app in self.apps.items():
            if path == prefix or path.startswith(prefix + "/"):
                scope = dict(scope, path=path[len(prefix) :] or "/", root_path=scope.get("root_path", "") + prefix)
                return await app(scope, receive, send)
        await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"not found"})


class LogCalls:
    """Log each request: who sent it, the JSON-RPC method (and tool), status, time."""

    def __init__(self, app, endpoint: str):
        self.app = app
        self.endpoint = endpoint

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.time()
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
        chunks: list[bytes] = []
        status: dict[str, int] = {}

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)
        method = ""
        try:
            body = json.loads(b"".join(chunks) or b"null")
            items = body if isinstance(body, list) else [body]
            method = ",".join(
                f"{m.get('method')}" + (f"[{m['params'].get('name')}]" if m.get("method") == "tools/call" else "")
                for m in items
                if isinstance(m, dict)
            )
        except (ValueError, AttributeError):
            method = "?"
        log.info(
            "%s %s %s -> %s in %.2fs from=%s ua=%s endpoint=%s",
            scope.get("method"),
            scope.get("path"),
            method,
            status.get("code"),
            time.time() - started,
            headers.get("x-forwarded-for") or (scope.get("client") or ["?"])[0],
            headers.get("user-agent", "")[:60],
            self.endpoint,
        )


def build_app(secret: str, read_secret: str | None = None):
    """The full tools under the secret, and the reading tools under the read secret."""
    servers = {secret: (mcp, "full")}
    if read_secret:
        servers[read_secret] = (mcp_read, "read-only")
    apps = {
        key: LogCalls(
            server.streamable_http_app(
                stateless_http=True,
                json_response=True,
                # The secret path is the access control; Host/Origin checks would
                # only reject the public hostname the dot uses.
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            ),
            endpoint,
        )
        for key, (server, endpoint) in servers.items()
    }

    # Each app's own lifespan starts its session manager, but a mounted app's
    # lifespan never runs, so start them all here.
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            for server, _ in servers.values():
                await stack.enter_async_context(server.session_manager.run())
            yield

    app = Starlette(lifespan=lifespan)
    app.mount("/", SecretPath(apps))
    return app


def setup_logging() -> str:
    """Log to a rotating file. The service runs for months, so the log is capped
    at about 30 MB: the current 5 MB file plus five older ones."""
    path = os.environ.get("AMIRMCP_LOG_FILE") or LOG_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    return path


def main() -> None:
    log_path = setup_logging()
    secret = os.environ.get("AMIRMCP_SECRET", "")
    if len(secret) < 16:
        raise SystemExit("AMIRMCP_SECRET must be set to at least 16 characters (see .env.example)")
    read_secret = os.environ.get("AMIRMCP_READ_SECRET", "")
    if read_secret and (len(read_secret) < 16 or read_secret == secret):
        raise SystemExit("AMIRMCP_READ_SECRET must be at least 16 characters and differ from AMIRMCP_SECRET")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY must be set (see .env.example)")
    port = int(os.environ.get("AMIRMCP_PORT", "8787"))
    log.info(
        "starting on 127.0.0.1:%d for %s, read-only endpoint %s, logging to %s",
        port,
        MACHINE,
        "on" if read_secret else "off",
        log_path,
    )
    # log_config=None sends uvicorn's own messages through the rotating file too.
    uvicorn.run(build_app(secret, read_secret), host="127.0.0.1", port=port, log_level="warning", log_config=None)


if __name__ == "__main__":
    main()
