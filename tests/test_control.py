"""Typing into panes: the checks that keep a message from going wrong.

The screens in tests/screens/ are real captures from Claude Code 2.1.296 and
Codex 0.161 in a lab Herdr session on 2026-10-10 (docs/SENDING_TO_AGENTS.md),
and from agy 1.3.4 on 2026-10-13 (docs/AGY_SUPPORT.md). agy_with_tasks and
claude_with_agents are the bottoms of real panes with their text swapped for
neutral words.
"""

import json
import os

import pytest

from amirmcp import control, server, sessions
from amirmcp.sessions import Session

SCREENS = os.path.join(os.path.dirname(__file__), "screens")

EXPECTED = {
    "claude_idle": "ready",
    "claude_fresh": "ready",  # dim "Try ..." placeholder
    "claude_busy": "ready",
    "claude_busy_queued": "ready",  # dim "Press up to edit queued messages"
    "claude_ssh_idle": "ready",  # Claude on amir-server, seen through ssh
    "claude_with_agents": "ready",  # background agents listed under the box; dim suggestion in it
    "claude_draft": "draft",
    "claude_question": "absent",
    "claude_permission": "absent",
    "claude_plan_approval": "absent",
    "claude_trust": "absent",  # "No, exit" is selected: Enter would quit Claude
    "codex_idle": "ready",
    "codex_fresh": "ready",
    "codex_working": "ready",
    "codex_working_queued": "ready",
    "codex_question": "ready",  # Codex's question waits; a message answers it
    "codex_draft": "draft",
    "codex_approval": "absent",
    "shell_after_exit": "absent",
    "agy_fresh": "ready",
    "agy_idle": "ready",
    "agy_working": "ready",
    "agy_working_queued": "ready",  # "▸ From Amir ..." waits above the box
    "agy_with_tasks": "ready",  # a background task listed under the box
    "agy_draft": "draft",
    "agy_question": "absent",
    "agy_question_second": "absent",
    "agy_question_after_history": "absent",  # Amir's earlier message has a short rule above it
    "agy_trust": "absent",  # "> Yes, I trust this folder" uses the same > glyph
}


def screen(name):
    with open(os.path.join(SCREENS, f"{name}.ansi")) as fh:
        return fh.read()


@pytest.mark.parametrize("name,expected", sorted(EXPECTED.items()))
def test_input_box_from_real_screens(name, expected):
    runtime = name.split("_")[0] if name.startswith(("codex", "agy")) else "claude"
    state, draft = control.input_box(screen(name), runtime)
    assert state == expected
    assert bool(draft) == (expected == "draft")


def test_drafts_are_read_back_and_shells_are_never_ready():
    assert control.input_box(screen("claude_draft"), "claude") == ("draft", "Amir half-typed draft")
    assert control.input_box(screen("codex_draft"), "codex") == ("draft", "Amir codex draft")
    assert control.input_box(screen("agy_draft"), "agy") == ("draft", "Amir half typed this")
    assert control.input_box(screen("shell_after_exit"), "codex")[0] == "absent"


def test_a_question_with_its_last_option_chosen_is_not_the_input_box():
    # "4. Chat about this" sits straight under a rule; Enter would choose it.
    chosen = screen("claude_question").replace("\n  4. Chat about this", "\n❯ 4. Chat about this")
    assert "❯ 4." in chosen
    assert control.input_box(chosen, "claude") == ("absent", "")


def test_clean_keeps_text_and_drops_terminal_controls():
    assert control.clean("a\r\nb\tc\x1b[201~d\x07") == "a\nb\tc[201~d"


class FakeHerdr:
    """Stands in for the herdr CLI: answers process-info and screen reads, records input."""

    def __init__(self, fg, shown):
        self.fg, self.shown, self.typed = fg, shown, []

    def __call__(self, args, socket_path=None):
        if args[:2] == ["pane", "process-info"]:
            return json.dumps({"result": {"process_info": {"foreground_processes": self.fg}}})
        if args[:2] == ["pane", "read"]:
            return self.shown
        self.typed.append(args)
        return ""


@pytest.fixture
def fake(monkeypatch):
    def make(fg, shown):
        f = FakeHerdr(fg, shown)
        monkeypatch.setattr(control.herdr, "run_text", f)
        monkeypatch.setattr(control, "_socket", lambda name: "/tmp/sock")
        monkeypatch.setattr(control.herdr, "_tcp_local_addresses", lambda pid: ["100.72.74.74:50370"])
        return f

    return make


CLAUDE = control.Target("lab", "w1:p1", "claude", pid=42)


def test_message_is_lead_in_then_paste(fake):
    f = fake([{"pid": 42, "name": "2.1.296"}], screen("claude_idle"))
    control.type_message(CLAUDE, "line one\nline two")
    assert f.typed == [
        ["pane", "send-text", "w1:p1", control.LEAD_IN],
        ["pane", "run", "w1:p1", "line one\nline two"],
    ]


@pytest.mark.parametrize("name", ["claude_draft", "claude_question", "claude_trust"])
def test_nothing_is_typed_unless_the_input_box_is_empty(fake, name):
    f = fake([{"pid": 42, "name": "2.1.296"}], screen(name))
    with pytest.raises(control.Refused):
        control.type_message(CLAUDE, "hello")
    assert f.typed == []


def test_nothing_is_typed_into_a_shell(fake):
    f = fake([{"pid": 99, "name": "zsh"}], screen("shell_after_exit"))
    for call in (lambda: control.type_message(CLAUDE, "rm -rf x"), lambda: control.press(CLAUDE, ["enter"], None)):
        with pytest.raises(control.Refused, match="no longer in the front"):
            call()
    assert f.typed == []


def test_agent_on_another_machine_is_matched_by_its_ssh_connection(fake):
    f = fake([{"pid": 8996, "name": "ssh"}], screen("claude_ssh_idle"))
    control.type_message(control.Target("rustai", "w1A:p1", "claude", ssh_client="100.72.74.74:50370"), "hi")
    assert len(f.typed) == 2
    with pytest.raises(control.Refused):
        control.type_message(control.Target("rustai", "w1A:p1", "claude", ssh_client="100.72.74.74:59999"), "hi")


def test_codex_question_counts_as_waiting_until_amir_answers(tmp_path):
    ask = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async",
                                                "arguments": json.dumps({"questions": [{"title": "North or south?", "options": ["North", "South"]}]})}}
    answer = {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "South"}]}}}
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps(ask) + "\n")
    assert sessions._codex_pending_question(str(path)) == "North or south?"
    path.write_text(json.dumps(ask) + "\n" + json.dumps(answer) + "\n")
    assert sessions._codex_pending_question(str(path)) is None


def test_delivery_check_sees_the_first_line_written_after_sending(tmp_path):
    path = tmp_path / "s.jsonl"
    before = {"type": "user", "timestamp": "2026-10-10T14:00:00Z", "message": {"content": "earlier"}}
    path.write_text(json.dumps(before) + "\n")
    cursor = max(os.path.getsize(path) - 1, 0)  # as send_message takes it
    sent = {"type": "user", "timestamp": "2026-10-10T14:01:00Z", "message": {"content": control.LEAD_IN + "Reply   BIRCH.\nNothing else."}}
    with open(path, "a") as fh:
        fh.write(json.dumps(sent) + "\n")
    s = Session("claude", "abc", str(path), "pro5", "/r", "t")
    entry = server._arrived(s, cursor, "Reply BIRCH.\nNothing else.")
    assert entry is not None and entry.cursor == len(json.dumps(before)) + 1
