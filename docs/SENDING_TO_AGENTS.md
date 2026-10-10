# Plan: letting the dot send messages to agents

Amir asked on 2026-10-10 for his dot (Ralph) to be able to issue commands back
to his coding agents through this connector, and to have it work in the hard
cases: messages that don't get submitted, prompts that pop up, agents on
amir-server. This is the plan, built from experiments run that day. It
extends the read-only connector described in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md); results are in
[TEST_PLAN.md](TEST_PLAN.md), section 8.

## What Herdr gives us, out of the box

Herdr itself can type into panes. None of this is an enhancement in Amir's
Herdr fork, which is upstream Herdr 0.9.0 plus one commit (a Codex
"model at capacity" state).

| Herdr command | What it does |
| --- | --- |
| `herdr agent prompt <agent> <text>` | Pastes the text, waits 300 ms, presses Enter. Refuses with `agent_blocked` if Herdr sees an approval or question dialog. Works only on panes where Herdr recognizes the agent. |
| `herdr pane run <pane> <text>` | Pastes the text and presses Enter in one write, on any pane. |
| `herdr pane send-text <pane> <text>` | Types the text exactly as given, without Enter. |
| `herdr pane send-keys <pane> <keys>` | Presses named keys (`enter`, `esc`, `down`, `2`, `shift+left`). |

The catch for Amir's setup: Herdr recognizes an agent by its process name, and
AI Manager starts Claude through the version-numbered binary
(`~/.local/share/claude/versions/2.1.296`). So Herdr recognizes none of his
Claude panes as agents, and `agent prompt` refuses them. It does recognize
Codex. The pane commands work everywhere.

## What the experiments showed

All experiments ran in a private Herdr session (`amirmcp-lab`) with agents
started the way Amir starts his (`aim claude run`, `codex -p yolo`, and
Claude on amir-server in an SSH session from a Mac pane). No existing agent
was touched.

Typing works when the agent is at its input box:

- One-line and multi-line messages arrive whole, as one message, for Claude
  and Codex, locally and through SSH to amir-server.
- A message sent while the agent is mid-turn is held and read at the next
  step: Claude shows it as a queued message, Codex shows "Messages to be
  submitted after next tool call". Both act on it.
- Codex's new question form (the "? 1 question, shift+← to answer" line in the
  agent-testing space) is not blocking: Codex posts the question and keeps
  waiting, and a plain message is read as the answer.

Typing goes wrong in four ways, each reproduced:

1. **A dialog takes the keystrokes.** Claude's folder-trust dialog has "No,
   exit" selected; text plus Enter quit Claude. Codex's trust dialog took
   Enter as "Trust and continue" and dropped the text. In a Claude question
   menu, typed text is ignored and Enter picks whatever is highlighted.
2. **The pane is a shell.** When the agent has exited, the next message runs
   as a shell command (seen when a lab Codex failed to start: zsh tried to run
   the message). On amir-server the Mac pane still shows `ssh` in front, so
   only the server can tell whether Claude is still in the foreground.
3. **Amir's unsent draft.** Text sitting in the input box is glued to the
   message and both are submitted as one ("Amir half-typed draftReply
   HOTEL."). Herdr's own `agent prompt` does the same.
4. **Long pastes are treated as data.** Claude wraps a pasted block of about a
   thousand characters or ten lines as pasted content and will not act on
   instructions inside it ("That instruction is part of the pasted text ...
   so I haven't acted on it"). A short line typed before the paste fixes this:
   Claude then follows the pasted body. Codex acts on long pastes either way.

Prompts that need an answer, and how they were answered:

| Prompt | Answered by |
| --- | --- |
| Claude question (AskUserQuestion), one or several questions | The option's number; "Type something" by its number, then the text and Enter; "1" on the review screen to submit. |
| Claude plan approval | `1`, `2` or `3` (3 asks for text). |
| Claude permission prompt (outside bypass mode) | `1` yes, `2` yes and don't ask again, `3` no. |
| Codex command approval | `y`, `p`, or `esc`. |
| Codex question | A plain message, or `shift+left` and the option. |
| Claude and Codex folder trust | Arrow to the option, Enter. |

## Design

Three new tools on each machine's plugin, next to the six read-only ones:

| Tool | Does | Read-only |
| --- | --- | --- |
| `read_screen(session)` | What the agent's pane shows right now, and whether its input box is ready for a message. For seeing a prompt before answering it. | yes |
| `send_message(session, text)` | Sends Amir's message to the agent as if typed at its input box, and reports whether the agent received it. | no |
| `press_keys(session, keys, text?)` | Presses keys in the agent's pane, then optionally types text and Enter. For answering a prompt or menu, or `esc` to stop a turn. | no |

`send_message` does, in order:

1. Finds the agent's pane fresh from Herdr and checks the agent itself is the
   pane's foreground process, so nothing is ever typed into a shell. For an
   agent on amir-server, the server checks that the agent's process group is
   in the foreground of its SSH terminal, and the Mac checks that the pane's
   `ssh` process is still that SSH connection.
2. Refuses if the agent is waiting on a dialog: Claude's own record says
   `waiting`, Herdr says Codex is `blocked`, or the screen does not show the
   agent's input box. The refusal returns the screen so Ralph can answer the
   prompt with `press_keys` instead.
3. Refuses if the input box holds text Amir typed and has not sent. Claude's
   and Codex's suggestion and placeholder text is drawn dim and is not a
   draft; anything else is.
4. Types `From Amir, via ChatGPT: `, pastes the message, waits 300 ms and
   presses Enter. The typed lead-in tells the agent whose words these are,
   and keeps Claude from treating a long message as pasted data.
5. Watches the agent's own transcript for the message for up to 10 seconds.
   It reports `delivered` (with the transcript cursor), `queued` (the screen
   shows it waiting for the agent's next step), or `not confirmed` with the
   screen, and never sends twice on its own.

`press_keys` does step 1 only, then presses the keys and returns the screen
afterwards, so Ralph sees what the key did.

For agents on amir-server, the server's copy does the checks it can do
locally, then runs `amirmcp-pane` on the Mac over the SSH link it already uses
to read the Mac's panes. That command does the screen and Herdr steps against
the Mac's Herdr.

State reporting also changes: a Codex agent with an unanswered question is
reported as waiting on Amir, with the question, instead of idle.

## What stays the same

- Access is still the one secret in the URL. Amir chose light protection, and
  ChatGPT asks him to confirm each call to a tool that is not read-only.
- The connector never starts, stops or restarts agents, and never answers a
  prompt on its own. Every keystroke comes from a tool call Ralph made.

## Tests

Unit tests cover reading the input box from real screen captures (empty,
placeholder, suggestion, draft, queued message, Claude menus and approvals,
Codex approvals and trust dialogs), the refusal paths, and the Codex question
state. The live checks repeat the experiments through the tools, locally and
on amir-server, then through Ralph in the BrowserOS Work profile; see
[TEST_PLAN.md](TEST_PLAN.md), section 8.
