"""The MCP server Amir's dot connects to.

Everything here is read-only: it reads Herdr's pane list, the agents' own
session records, and their transcript files, and asks Claude Haiku 5.5 to
summarize. It never types into a pane or changes a file.

The public endpoint is ``https://<host>/<AMIRMCP_SECRET>/mcp``. Requests whose
path does not start with the secret get a bare 404. That one secret is the whole
access control, as the capability brief asked.
"""

from __future__ import annotations

import json
import logging
import os
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

from . import sessions, summarize, transcripts
from .sessions import Session, Snapshot, local_iso

log = logging.getLogger("amirmcp")

LOG_FILE = os.path.expanduser("~/Library/Logs/amirmcp/amirmcp.log")

INSTRUCTIONS = """\
Read-only view of Amir's coding agents on his Mac (Amir-M5): every Claude Code \
and Codex session in his Herdr spaces, plus ones that stopped recently. For "how's \
X going", call summarize_session; for "what's going on" or "what needs me", call \
overview; list_sessions finds ids. States: working, blocked (waiting on Amir), \
idle (finished its turn; not necessarily done), stopped (no process), unknown. \
Cursors like c123 point into a transcript; cite them and check details with \
read_history or search_history.\
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer("amirmcp", title="Amir's coding sessions", instructions=INSTRUCTIONS)

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


@mcp.tool(annotations=READ_ONLY)
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
    return {
        "observed_at": local_iso(snap.observed_at),
        "counts_by_state": counts,
        "sessions": [s.as_dict(snap.observed_at) for s in found],
        "gaps": snap.gaps,
    }


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
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
        "observed_at": local_iso(snap.observed_at),
        "sessions": rows,
        "needs_amir_count": sum(1 for r in rows if needs(r)),
        "gaps": snap.gaps,
    }


class SecretPath:
    """Serve the MCP app only under /<secret>/; everything else is a 404."""

    def __init__(self, app, secret: str):
        self.app = app
        self.prefix = f"/{secret}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path != self.prefix and not path.startswith(self.prefix + "/"):
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"not found"})
            return
        scope = dict(scope, path=path[len(self.prefix) :] or "/", root_path=scope.get("root_path", "") + self.prefix)
        return await self.app(scope, receive, send)


class LogCalls:
    """Log each request: who sent it, the JSON-RPC method (and tool), status, time."""

    def __init__(self, app):
        self.app = app

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
            "%s %s %s -> %s in %.2fs from=%s ua=%s",
            scope.get("method"),
            scope.get("path"),
            method,
            status.get("code"),
            time.time() - started,
            headers.get("x-forwarded-for") or (scope.get("client") or ["?"])[0],
            headers.get("user-agent", "")[:60],
        )


def build_app(secret: str):
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # The secret path is the access control; Host/Origin checks would only
        # reject the public hostname the dot uses.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return SecretPath(LogCalls(app), secret)


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
    load_env_file(os.environ.get("AMIRMCP_ENV_FILE") or os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    log_path = setup_logging()
    secret = os.environ.get("AMIRMCP_SECRET", "")
    if len(secret) < 16:
        raise SystemExit("AMIRMCP_SECRET must be set to at least 16 characters (see .env.example)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY must be set (see .env.example)")
    port = int(os.environ.get("AMIRMCP_PORT", "8787"))
    log.info("starting on 127.0.0.1:%d, logging to %s", port, log_path)
    # log_config=None sends uvicorn's own messages through the rotating file too.
    uvicorn.run(build_app(secret), host="127.0.0.1", port=port, log_level="warning", log_config=None)


if __name__ == "__main__":
    main()
