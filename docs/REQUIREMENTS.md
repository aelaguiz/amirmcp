# Requirements: coding-session connector for Amir's OpenAI dot

Amir's dot runs on OpenAI's servers. From anywhere, he wants to ask it "how's the
work on X going?" and get a current, specific answer about the Claude and Codex
sessions running in his Herdr sessions on his Mac (Amir-M5). This repo is the
connector that makes that possible. The source brief is
`/Users/aelaguiz/Downloads/coding-session-capabilities.md`, written by the dot
itself on 2026-10-09; this document turns it into checkable requirements.

The first version is read-only and on demand: the dot asks, the connector
answers from what is on disk and in Herdr at that moment.

## Who and where

- **Reader:** Amir's dot, using tools exposed by a custom MCP plugin installed in
  the dot from the BrowserOS Work profile.
- **Machine:** Amir-M5 (this Mac). Herdr runs several named sessions here
  (brain, bugs, content, cratejoy, growth, logan, rustai, snap, tools as of
  2026-10-09), each with its own workspaces, tabs and panes.
- **Agents:** Claude Code and Codex, almost all launched through AI Manager
  (`aim`), on many accounts. Claude transcripts live under each account's home in
  `~/.aimgr/claude-homes/<account>/.claude/`; Codex state is shared in
  `~/.codex` (reached through `~/.aimgr/codex-cli`).

## What the dot must be able to do

1. **Find the right work.**
   - List the coding sessions running in any Herdr pane right now, across every
     running Herdr session, plus sessions active recently that are no longer in
     a pane (for example a finished run or a headless `aim` routine).
   - For each: the Herdr session, workspace, tab and pane it lives in; runtime
     (Claude or Codex); AI Manager account; project directory and git branch;
     the agent's own session id and transcript file; a human label (session
     name, thread title, or first request).
   - Resolve loose names ("the SEO work", "psagentspace bugs", "growth") to
     sessions by matching those labels, the directory and the request text.
   - The mapping from pane to transcript must come from the running process,
     not from guessing by directory, so two sessions in the same repo are not
     confused. When a session is resumed or replaced, the newest one wins and
     the id changes visibly.

2. **Check its actual state.**
   - Working, waiting for input, blocked on a question or permission, or
     unknown, with the time of the last transcript activity and the time the
     state was observed.
   - Give the reason when one is known (for example, the agent asked a
     question and stopped).
   - An idle pane or an old last message is reported as idle since a time,
     never as "finished".

3. **Read just enough history.**
   - A bounded recent slice of the conversation (user requests, assistant
     messages, tool calls and their results, shortened).
   - Everything after a cursor the dot got from an earlier call.
   - A text search inside one session or across all recent sessions, returning
     each hit with a little surrounding context.
   - No terminal scraping as the primary source; the transcript files are.

4. **Ground the answer in evidence.**
   - Every entry carries its source: transcript path, line number (the cursor)
     and timestamp.
   - Summaries keep three things apart: what the agent said it would do, what
     it actually ran, and what was verified (a test run and its result).
   - Stale or missing information is said out loud (no transcript found, pane
     process unreadable, transcript not written for hours).

5. **Explain what happens next.**
   - A summary per session with: current objective, meaningful progress,
     latest result, remaining work, blocker, and any decision needed from
     Amir.
   - Links that appear in the transcript (pull requests, commits, issues)
     surface in the summary.
   - Summaries are written by Claude Haiku 5.5 (`claude-haiku-5-5`) and cached
     by transcript position, so asking twice costs nothing new.

## Machines

Added 2026-10-10 at Amir's request: amir-server ("home") runs its own copy,
connected to the dot as a second plugin. Its agents run in SSH sessions opened
from Herdr panes on the Mac, and each must be shown with that pane.

## Connection

- A public HTTPS MCP endpoint (streamable HTTP) the dot can reach, served from
  this Mac, stable across restarts.
- Installed in the dot as a custom MCP plugin through the dot's UI in the
  BrowserOS Work profile, and verified by asking the dot real questions there.
- Light protection, per the brief: the endpoint is not open to anyone who finds
  the URL, but no elaborate security design.
- Runs as a background service that starts at login and restarts on failure.

## Out of scope for this version

- Remote control: prompting, interrupting or starting agents.
- Push notifications when a session finishes or blocks (MCP Events). A later
  addition.
- A permanently running local model. Summaries are on demand through the
  Anthropic API.

## Done means

- From the BrowserOS Work profile, the dot lists Amir's live sessions with
  correct Herdr locations, answers "how's X going" for a named session with a
  grounded summary, and quotes recent history with sources.
- The same tools work from a local MCP client on this Mac.
- Tests in this repo cover the pane-to-transcript mapping and transcript
  parsing against real files, and pass.
- Code is committed and pushed to https://github.com/aelaguiz/amirmcp. No
  secrets in the repo (it is public).
