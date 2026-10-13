# Plan: Antigravity CLI (agy) sessions

Amir asked on 2026-10-13 for the connector to fully support Google's
Antigravity CLI, `agy`, which he now runs in Herdr panes next to Claude Code
and Codex (three panes that day: "lessons agy" and "bots agy" in the content
space, "subgame agy" in rustai, all on Gemini 4 Argon). Fully means the same as
Claude and Codex: the dot lists agy sessions with their panes, reads and
searches their history, summarizes them, and Ralph can send them messages and
answer their prompts. Grok Bot keeps reading only. Built and tested the same
day; results are in [TEST_PLAN.md](TEST_PLAN.md), section 9.

## What agy keeps on disk

Everything lives in `~/.gemini/antigravity-cli/` (agy 1.3.4):

| Need | Where | Notes |
| --- | --- | --- |
| Which conversation a pane runs | `presence/<conversation id>.lock` | A running agy holds its conversation's lock file open, so the link comes from the process (`lsof`), not from the folder. |
| Title, folder, run status | `conversation_summaries.db`, table `conversation_summaries` | One row per conversation: `title`, `workspace_uris`, `status` (`CASCADE_RUN_STATUS_IDLE` or `_RUNNING`), `last_modified_time`, `last_user_input_time`, and `parent_conversation_id` for subagents. |
| The conversation itself | `conversations/<id>.db`, table `steps` | One row per step: `idx`, `step_type`, `status`, and `step_payload`, a protobuf record. |

agy records its signed-in Google account only in its log
(`~/.gemini/google_accounts.json` belongs to the older Gemini CLI and names a
different account), and AI Manager does not launch it, so agy sessions show no
account.

The databases are SQLite in WAL mode and agy writes them while it runs. The
connector opens them read-only (`mode=ro`) while agy has them open, and as an
immutable file when it does not, so it never writes, checkpoints, or locks
them. An agy suspended with ctrl+z keeps its database locked; then the
connector reads the file as last written instead of failing.

Step records have no published schema, but agy's binary embeds its protobuf
definitions. Decoded with those, a step is `gemini_coder.Step`: field 1 is the
step type, 4 its status, 5 its metadata (5.1 is when it was created), and one
more field holds the step itself. The connector decodes the few it needs by
field number, without a protobuf dependency:

| Step type | Field | Becomes |
| --- | --- | --- |
| 14 user input | 19: `user_response` (2), `is_queued_message` (6) | `user` entry |
| 15 planner response | 20: `response` (1), `modified_response` (8) | `assistant` entry when it has text; its tool calls show up as the steps that follow |
| 132 generic tool step | 140: `args` (1, a map with `toolSummary`, `CommandLine`, `AbsolutePath`, ...), `result` (2) with its text (1); the tool's name is in the metadata (5.4.2) | `tool` entry with the command, file or question and the output's tail; an error when the step failed or the command's exit code is not 0 |
| 101 system message | 114: `message` (1) | `event` entry |
| 17 error message | 24: `error` (3) with `user_error_message` (1) / `short_error` (2) | `tool` entry marked as an error |
| 127 invoke subagent | 143: `subagent_name` (1), `prompt` (2) | `event` entry |

Every tool call agy 1.3.4 made in Amir's conversations was a generic tool
step: `run_command`, `view_file`, `manage_task`, `schedule`, `finish` and
`ask_question`. A question is an `ask_question` tool step whose `questions`
argument lists each question and its options; while Amir has not answered,
the step's status is 9, waiting, and the answer arrives as its result
(`A1: Tokyo`).

A cursor for agy is the step index (`c42` is step 42), so "everything after a
cursor" and paging work as they do for the other two.

## Sessions and state

- **In a pane.** Herdr already recognizes agy (`herdr_agent: agy`, status
  `working`, `idle`, `done` or `blocked`); that is upstream Herdr, not Amir's
  fork. The pane's foreground `agy` process gives the pid, its open lock file
  the conversation.
- **State.** A step waiting on Amir (status 9) makes the session blocked, with
  the question quoted. Otherwise Herdr's status, since it reads the live
  screen: `blocked` is a prompt, `working` a turn in progress, `idle` or
  `done` finished. Without Herdr, the summary row's run status.
- **A fresh agy** that has not been sent anything holds no conversation yet; it
  is listed as open at its prompt, and its first message starts one.
- **Not running.** Conversations written in the last 72 hours with no agy
  process are listed as stopped, like the other two. Subagent conversations are
  left out of the list; the parent's history shows the subagent call.
- **Out of scope:** the older one-file `.pb` conversations from May 2026 (a
  previous storage format, none active) and amir-server, which has no agy.
  The same code would serve agy there; sending through the Mac's pane would
  work as it does for Claude, but is untested.

## Sending

Same path as Claude and Codex (see [SENDING_TO_AGENTS.md](SENDING_TO_AGENTS.md)):
check the agy process is in front of its pane, check its input box is showing
and empty, type the lead-in and the message, confirm it from the conversation.

What the lab showed (agy 1.3.4, Gemini 4 Argon, 2026-10-13):

- **Input box.** A `>` row between two rules that span the pane, above a footer
  (`? for shortcuts`, `esc to cancel` or `Press up to edit queued messages`,
  then the model). A list of background tasks can sit between the box and the
  footer. A draft is plain text after the `>`.
- **Two look-alikes.** agy marks the chosen option of every menu with the same
  `>` (`> Yes, I trust this folder`, `> 1. Apple`), and draws a shorter, fixed
  60-character rule above each of Amir's earlier messages in the history, so
  an old `> From Amir ...` line sits between rules too. Only full-width rules
  count; the first version of the check read a question screen as a draft
  because of that history line.
- **Mid-turn.** A message sent while agy works is queued (`▸ From Amir ...`
  above the box) and read at the next step; it was in the conversation within
  ten seconds and agy acted on it.
- **Long messages.** 2,700 and 4,300 characters over 31 and 41 lines arrived
  whole and were acted on; agy does not treat a long paste as data.
- **Questions.** `ask_question` replaces the input box with a menu, and Herdr
  reports the pane as `blocked`. Each question takes its option's number; the
  last one submits. `Write-in...` takes its number and then text and Enter.
  `esc` skips.
- **No approvals.** Amir runs agy with `toolPermission: always-proceed`, so it
  never stops to ask before a command or file read. That setting is shared by
  every agy on the Mac, so the lab did not change it; if an approval does show
  up, it has no input box and `send_message` refuses.
- **Folder trust.** A new folder opens with a trust menu (`blocked`, no input
  box); accepting it adds the folder to `trustedWorkspaces` in agy's settings.
- **Stopping.** `esc` stops a turn ("Interrupted · What should Antigravity CLI
  do instead?"). ctrl+c twice quits agy and leaves a shell.

## Tests

- Unit (`tests/test_agy.py`, `tests/test_control.py`): step records built by
  field number become the right entries, cursors are step indexes, a waiting
  step blocks the session with its question, reading leaves agy's files
  untouched, a pane's agy is tied to its conversation, and ten agy screen
  captures from the lab read as ready, draft or absent.
- Live, read only, on Amir's running agy sessions: `list_sessions`,
  `get_session`, `read_history`, `search_history`, `summarize_session`.
- Lab, on agy sessions started for the test: every sending case, then Ralph
  sending a message to a lab agy and answering its question.
