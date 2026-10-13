"""Status briefs for one session, written by Claude Haiku 5.5.

A brief is cached by transcript position and live state, so asking again about a
session that has not moved costs nothing.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from . import transcripts
from .sessions import Session, ago, local_iso

MODEL = "claude-haiku-5-5"
WINDOW_CHARS = 150_000
REQUEST_CHARS = 30_000
CACHE_PATH = os.path.expanduser("~/.cache/amirmcp/briefs.json")

SYSTEM_PROMPT = """\
You are writing a status brief about one of Amir's coding-agent sessions. Amir \
is the founder and CEO of Fun Country (Poker Skill) and an engineer; he runs \
dozens of Claude Code, Codex and Antigravity CLI (agy) agents at once in Herdr \
terminal spaces. He is away from his desk and has asked his assistant how this \
piece of work is going. \
Your brief is what the assistant answers from, and Amir acts on it: he leaves the \
agent alone, unblocks it, or redirects it. A wrong "done" or a missed question \
costs him real time, so accuracy matters more than polish.

You are given facts observed just now (where the session runs, its live state, \
when it last wrote), every request Amir made in the session, and the most recent \
stretch of its transcript. Each transcript line starts with a cursor such as \
[c1234567]. Kinds: user (Amir, or the system, speaking to the agent), assistant \
(the agent's own words), tool (a command or tool call, with its outcome when \
recorded), result (a tool's output), event (turns starting and finishing, \
background reports, slash commands).

How to read it:
- The live state in the facts is authoritative for whether the agent is working \
right now. An agent idle at its prompt has finished a turn, which is not the same \
as the work being done. Say which it is: it completed what was asked, stopped to \
ask something, gave up, or handed back a partial result.
- Keep three things apart, because Amir treats them differently: what the agent \
said it would do, what it actually ran, and what an observed result verified. \
"I'll run the tests" is intent; a test command is action; a test command whose \
output shows passes is verification. Agents sometimes claim success that their \
tool output does not support; when you see that, say so plainly.
- Cite cursors for every claim so the assistant can check it, preferring the \
entry that shows the fact (a command's output) over the agent's description of it.
- If the window starts mid-story, take the objective from Amir's requests and say \
what you could not see. If the last write was long ago, say how stale the picture is.
- Name things the way the transcript does: real paths, PR and issue numbers with \
what they are, commands. Copy URLs exactly. Never invent numbers or links.

Write plain English in short, complete sentences. Lead each field with the answer.\
"""


class SessionBrief(BaseModel):
    status: str = Field(
        description="One or two sentences Amir reads first: what state the session is in and what it is doing or did last, with the time."
    )
    objective: str = Field(description="What Amir asked this session to do, including later redirections.")
    progress: list[str] = Field(
        description="Meaningful steps taken, oldest first, each marked as said / did / verified and ending with cursor citations like [c123]."
    )
    latest_result: str = Field(description="The most recent concrete outcome, with cursor citations.")
    remaining: list[str] = Field(description="Work still left toward the objective, as far as the transcript shows.")
    needs_amir: str = Field(
        description="The decision, question, approval, or credential the agent is waiting on from Amir, quoted closely with a cursor; 'Nothing' if none."
    )
    blocker: str = Field(description="What is stopping progress other than Amir, or 'None'.")
    links: list[str] = Field(description="PRs, commits, issues, docs or other URLs from the transcript that matter, each with what it is.")
    gaps: list[str] = Field(description="What the brief could not see or verify: missing history, stale data, unverified claims.")


_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] | None = None
_client: anthropic.Anthropic | None = None


def _client_once() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3, timeout=120)
    return _client


def _load_cache() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH) as fh:
                _cache = json.load(fh)
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(_cache, fh)
    os.replace(tmp, CACHE_PATH)


def _line(entry: transcripts.Entry) -> str:
    flag = " (error)" if entry.is_error else ""
    return f"[c{entry.cursor}] {transcripts.local_ts(entry.ts)} {entry.kind}{flag}: {entry.text}"


def build_input(session: Session, now: float, window_chars: int = WINDOW_CHARS) -> tuple[str, int]:
    """The facts, Amir's requests, and the recent transcript, as one message."""
    path = session.transcript
    facts = {
        "machine": os.environ.get("AMIRMCP_MACHINE") or socket.gethostname(),
        "observed_at": local_iso(now),
        "runtime": session.runtime,
        "account": session.account,
        "title": session.title,
        "project_dir": session.cwd,
        "branch": session.branch,
        "where": session.pane.location() if session.pane else "not in a Herdr pane",
        "live_state": session.state,
        "live_state_reason": session.state_reason,
        "last_write": f"{local_iso(session.last_activity)} ({ago(session.last_activity, now)})",
    }
    requests = transcripts.user_requests(path, session.runtime)
    request_lines: list[str] = []
    used = 0
    for entry in reversed(requests):  # keep the newest requests if there are many
        line = _line(entry)
        if used + len(line) > REQUEST_CHARS:
            request_lines.append(f"[{len(requests) - len(request_lines)} earlier requests not shown]")
            break
        request_lines.append(line)
        used += len(line)
    request_lines.reverse()

    entries, more = transcripts.tail_entries(path, session.runtime, 3000)
    window: list[str] = []
    used = 0
    for entry in reversed(entries):
        line = _line(entry)
        if used + len(line) > window_chars:
            more = True
            break
        window.append(line)
        used += len(line)
    window.reverse()
    first_cursor = entries[-len(window)].cursor if window else 0
    body = (
        "Facts observed now:\n"
        + json.dumps(facts, indent=1)
        + "\n\nAmir's requests in this session, oldest first:\n"
        + ("\n".join(request_lines) or "(none found)")
        + "\n\nRecent transcript"
        + (" (earlier history exists but is not shown)" if more else " (complete)")
        + ":\n"
        + "\n".join(window)
    )
    return body, first_cursor


def brief(session: Session, now: float, refresh: bool = False, window_chars: int = WINDOW_CHARS) -> dict[str, Any]:
    if not session.transcript:
        return {"error": "This session has no transcript yet, so there is nothing to summarize.", "state": session.state}
    position = transcripts.last_cursor(session.transcript, session.runtime)
    key = f"{session.transcript}|{position}|{session.state}"
    with _lock:
        cached = _load_cache().get(key)
    # A brief written from a wider window can stand in for a narrower one
    # (summarize_session's can serve the overview), but not the other way round.
    if cached and not refresh and cached.get("window_chars", 0) >= window_chars:
        return {**cached, "cached": True}

    body, first_cursor = build_input(session, now, window_chars)
    started = time.time()
    response = _client_once().messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": body}],
        output_format=SessionBrief,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return {"error": f"Haiku did not return a brief (stop reason {response.stop_reason})."}
    result = {
        "session": session.short_id,
        "brief": response.parsed_output.model_dump(),
        "covers": {
            "transcript": session.transcript,
            "from_cursor": first_cursor,
            "to_cursor": position,
            "written_at": local_iso(time.time()),
            "state_when_written": session.state,
        },
        "model": MODEL,
        "window_chars": window_chars,
        "seconds": round(time.time() - started, 1),
    }
    with _lock:
        cache = _load_cache()
        cache[key] = result
        # Keep only the newest brief per transcript.
        for old in [k for k in cache if k.startswith(session.transcript + "|") and k != key]:
            del cache[old]
        _save_cache()
    return {**result, "cached": False}
