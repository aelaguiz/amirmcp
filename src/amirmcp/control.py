"""Typing into an agent's Herdr pane for Amir, without the ways it goes wrong.

Herdr can type into any pane (``pane send-text``, ``pane run``, ``pane
send-keys``). What it cannot know is whether the agent is ready to read what
arrives. Experiments on 2026-10-10 (docs/SENDING_TO_AGENTS.md) found four ways
a message goes wrong, and each check here answers one of them:

* A dialog takes the keystrokes (Enter picked "No, exit" in Claude's trust
  dialog): type only when the agent's input box is on screen.
* The pane is a shell because the agent exited: type only when the agent
  process itself is in the foreground.
* Amir's unsent draft gets glued to the message: refuse when the input box
  holds text that is not dim placeholder or suggestion text.
* Claude treats a long paste as data, not instructions: type a short lead-in
  before pasting the message.

These functions run on the machine whose Herdr shows the pane. For an agent on
amir-server that is the Mac, reached over SSH through ``amirmcp-pane``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from . import herdr

LEAD_IN = "From Amir, via ChatGPT: "
KEY_GAP_SECONDS = 0.4
SETTLE_SECONDS = 1.5


class Refused(Exception):
    """Nothing was typed; ``screen`` shows what the pane looks like instead."""

    def __init__(self, reason: str, screen: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.screen = screen


# ----------------------------------------------------------- reading screens

_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_OTHER_ESCAPE = re.compile(r"\x1b\[[0-9;:?<=>]*[A-Za-z@`~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")


def _cells(row: str) -> list[tuple[str, bool]]:
    """Each visible character of an ANSI row, and whether it is drawn dim."""
    out: list[tuple[str, bool]] = []
    dim = False
    i = 0
    while i < len(row):
        if row[i] == "\x1b":
            m = _SGR.match(row, i)
            if m:
                params = [p for p in re.split("[;:]", m.group(1))] or [""]
                j = 0
                while j < len(params):
                    p = params[j]
                    if p in ("", "0", "22"):
                        dim = False
                    elif p == "2":
                        dim = True
                    elif p in ("38", "48", "58"):
                        # extended colour: 5;n or 2;r;g;b
                        j += 2 if j + 1 < len(params) and params[j + 1] == "5" else 4
                    j += 1
                i = m.end()
                continue
            m = _OTHER_ESCAPE.match(row, i)
            i = m.end() if m else i + 1
            continue
        out.append((row[i], dim))
        i += 1
    return out


def plain(text: str) -> str:
    return "\n".join("".join(c for c, _ in _cells(row)) for row in text.split("\n"))


PROMPT_GLYPH = {"claude": "❯", "codex": "›", "agy": ">"}


def input_box(screen_ansi: str, runtime: str) -> tuple[str, str]:
    """Whether the agent's input box is on screen and what Amir has typed in it.

    Returns ("ready", ""), ("draft", text) or ("absent", ""). Claude draws its
    input box as a ❯ line straight under a rule; Codex as a › line straight
    above its status footer; agy as a > line between two rules, which can sit
    above a list of background tasks. All draw placeholder and suggestion text
    dim, so only undimmed text is a draft. agy marks the chosen option of its
    menus with > too, but never between rules.
    """
    glyph = PROMPT_GLYPH[runtime]
    rows = screen_ansi.split("\n")
    texts = ["".join(c for c, _ in _cells(r)) for r in rows]
    filled = [i for i, t in enumerate(texts) if t.strip()]
    bottom = set(filled[-12:] if runtime == "agy" else filled[-6:])
    width = max((len(t.rstrip()) for t in texts), default=0)
    found, box = None, []
    for n, i in enumerate(filled):
        if i not in bottom or not texts[i].startswith(glyph):
            continue
        above = texts[filled[n - 1]].strip() if n > 0 else ""
        if runtime == "claude":
            if above.startswith("──"):
                found, box = i, [i]
        elif runtime == "agy":
            # The box's rules span the pane; the shorter rules above Amir's
            # earlier messages in the history do not. The box grows a line per
            # line of a draft and ends at the next full rule.
            def full_rule(j: int) -> bool:
                t = texts[j].strip()
                return t.startswith("──") and len(t) >= 0.9 * width

            following = filled[n + 1 : n + 6]
            end = next((j for j in following if full_rule(j)), None)
            if n > 0 and full_rule(filled[n - 1]) and end is not None:
                found, box = i, [j for j in filled[n:] if j < end]
        else:
            below = texts[filled[n + 1]] if n + 1 < len(filled) else ""
            if " · " in below:
                found, box = i, [i]
    if found is None:
        return "absent", ""
    typed = " ".join(
        "".join(c for c, dim in _cells(rows[j])[1 if j == found else 0 :] if not dim).replace("\xa0", " ").strip() for j in box
    ).strip()
    return ("draft", typed) if typed else ("ready", "")


# ------------------------------------------------------------- the pane side


@dataclass
class Target:
    """An agent's pane in this machine's Herdr, and how to know it is still there.

    ``pid`` is the agent process for an agent running here. ``ssh_client`` is
    the SSH connection (``ip:port`` on this side) for an agent running on the
    machine that pane SSHes into.
    """

    session: str
    pane_id: str
    runtime: str
    pid: int | None = None
    ssh_client: str | None = None


def _socket(session: str) -> str:
    for s in herdr.running_sessions():
        if s["name"] == session:
            return s["socket_path"]
    raise Refused(f"Herdr session {session!r} is not running.")


def _herdr(target: Target, *args: str) -> str:
    return herdr.run_text(list(args), _socket(target.session))


def screen(target: Target) -> str:
    """The pane's visible screen, with colours (needed to tell drafts from hints)."""
    return _herdr(target, "pane", "read", target.pane_id, "--source", "visible", "--format", "ansi")


def check_front(target: Target) -> None:
    """Refuse unless the agent (or its SSH connection) is the pane's foreground."""
    info = json.loads(_herdr(target, "pane", "process-info", "--pane", target.pane_id))["result"]["process_info"]
    procs = info.get("foreground_processes", [])
    if target.pid is not None:
        if any(p.get("pid") == target.pid for p in procs):
            return
    elif target.ssh_client:
        for p in procs:
            if p.get("name") == "ssh" and p.get("pid") and target.ssh_client in herdr._tcp_local_addresses(p["pid"]):
                return
    names = ", ".join(p.get("name", "?") for p in procs) or "nothing"
    raise Refused(f"The agent is no longer in the front of pane {target.pane_id} (it shows {names}); nothing was typed.")


def clean(text: str) -> str:
    """Message text with terminal control characters removed (they could end the paste early)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))


def type_message(target: Target, text: str) -> dict[str, Any]:
    check_front(target)
    shown = screen(target)
    state, draft = input_box(shown, target.runtime)
    if state == "absent":
        raise Refused(
            "The agent is not at its input box; it is showing something else, likely a prompt or menu. "
            "Read the screen and answer it with press_keys first.",
            plain(shown),
        )
    if state == "draft":
        raise Refused(f"Amir has unsent text in the agent's input box ({draft[:120]!r}); sending now would merge with it.", plain(shown))
    _herdr(target, "pane", "send-text", target.pane_id, LEAD_IN)
    _herdr(target, "pane", "run", target.pane_id, clean(text))
    return {"typed": True}


def press(target: Target, keys: list[str], text: str | None) -> dict[str, Any]:
    check_front(target)
    for key in keys:
        _herdr(target, "pane", "send-keys", target.pane_id, key)
        time.sleep(KEY_GAP_SECONDS)
    if text:
        _herdr(target, "pane", "run", target.pane_id, clean(text))
    time.sleep(SETTLE_SECONDS)
    return {"screen": plain(screen(target))}


def look(target: Target) -> dict[str, Any]:
    check_front(target)
    shown = screen(target)
    state, draft = input_box(shown, target.runtime)
    return {"screen": plain(shown), "input_box": state, "draft": draft}


OPS = {
    "screen": lambda t, req: look(t),
    "send": lambda t, req: type_message(t, req["text"]),
    "keys": lambda t, req: press(t, req.get("keys") or [], req.get("text")),
}


def run(request: dict[str, Any]) -> dict[str, Any]:
    target = Target(**request["target"])
    try:
        return {"ok": True, **OPS[request["op"]](target, request)}
    except Refused as exc:
        return {"ok": False, "refused": exc.reason, "screen": exc.screen}
    except herdr.HerdrError as exc:
        return {"ok": False, "error": f"Herdr: {exc}"}


def pane_main() -> None:
    """``amirmcp-pane``: one request as JSON on stdin, the answer as JSON on stdout."""
    print(json.dumps(run(json.loads(sys.stdin.read()))))


def run_on(host: str | None, request: dict[str, Any]) -> dict[str, Any]:
    """Run a request here, or on the machine whose Herdr shows the pane."""
    if not host:
        return run(request)
    command = os.environ.get("AMIRMCP_REMOTE_PANE_CMD", "~/workspace/amirmcp/.venv/bin/amirmcp-pane")
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, command],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timed out reaching Herdr on {host}; whether anything was typed is unknown."}
    if proc.returncode != 0:
        return {"ok": False, "error": f"Could not reach Herdr on {host}: {(proc.stderr or proc.stdout).strip()[-300:]}"}
    return json.loads(proc.stdout)
