"""Read-only view of every running Herdr session on this Mac.

Herdr runs several named sessions (brain, bugs, growth, ...), each behind its own
unix socket. The ``herdr`` CLI talks to whichever socket ``HERDR_SOCKET_PATH``
names and answers with JSON, so every call here pins that variable explicitly
and never inherits the Herdr session this process happens to run in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

TIMEOUT_SECONDS = 10


class HerdrError(RuntimeError):
    pass


def _herdr_bin() -> str:
    return (
        os.environ.get("HERDR_BIN")
        or shutil.which("herdr")
        or os.path.expanduser("~/.cargo/bin/herdr")
    )


def _run(args: list[str], socket_path: str | None = None) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    if socket_path:
        env["HERDR_SOCKET_PATH"] = socket_path
    try:
        proc = subprocess.run(
            [_herdr_bin(), *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise HerdrError(f"herdr {' '.join(args)} timed out") from exc
    if proc.returncode != 0:
        raise HerdrError((proc.stderr or proc.stdout).strip()[-500:])
    data = json.loads(proc.stdout)
    if isinstance(data, dict) and data.get("error"):
        raise HerdrError(json.dumps(data["error"]))
    return data.get("result", data)


@dataclass
class Pane:
    """One Herdr pane and the processes in its foreground."""

    session: str
    pane_id: str
    workspace_id: str
    workspace_label: str
    tab_id: str
    tab_label: str
    title: str
    cwd: str
    herdr_agent: str | None
    herdr_status: str
    processes: list[dict[str, Any]] = field(default_factory=list)

    def location(self) -> dict[str, str]:
        return {
            "herdr_session": self.session,
            "workspace": self.workspace_label,
            "tab": self.tab_label,
            "pane_id": self.pane_id,
            "pane_title": self.title,
        }


def running_sessions() -> list[dict[str, Any]]:
    return [s for s in _run(["session", "list", "--json"])["sessions"] if s["running"]]


def _session_panes(session: dict[str, Any]) -> list[Pane]:
    sock = session["socket_path"]
    workspaces = {
        w["workspace_id"]: w.get("label") or ""
        for w in _run(["workspace", "list"], sock)["workspaces"]
    }
    tabs = {t["tab_id"]: t.get("label") or "" for t in _run(["tab", "list"], sock)["tabs"]}
    panes = []
    for p in _run(["pane", "list"], sock)["panes"]:
        info = _run(["pane", "process-info", "--pane", p["pane_id"]], sock)
        panes.append(
            Pane(
                session=session["name"],
                pane_id=p["pane_id"],
                workspace_id=p["workspace_id"],
                workspace_label=workspaces.get(p["workspace_id"], ""),
                tab_id=p["tab_id"],
                tab_label=tabs.get(p["tab_id"], ""),
                title=p.get("terminal_title_stripped") or "",
                cwd=p.get("foreground_cwd") or p.get("cwd") or "",
                herdr_agent=p.get("agent"),
                herdr_status=p.get("agent_status") or "unknown",
                processes=info["process_info"].get("foreground_processes", []),
            )
        )
    return panes


def all_panes() -> tuple[list[Pane], list[str]]:
    """Every pane in every running Herdr session, plus per-session read errors."""
    sessions = running_sessions()
    panes: list[Pane] = []
    errors: list[str] = []

    def load(session: dict[str, Any]) -> tuple[list[Pane], str | None]:
        try:
            return _session_panes(session), None
        except (HerdrError, json.JSONDecodeError, KeyError) as exc:
            return [], f"Herdr session {session['name']!r} could not be read: {exc}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        for found, error in pool.map(load, sessions):
            panes.extend(found)
            if error:
                errors.append(error)
    return panes, errors
