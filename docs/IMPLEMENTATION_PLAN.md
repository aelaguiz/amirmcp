# Implementation plan: coding-session connector

Status on 2026-10-10: running on Amir-M5 (since 2026-10-09) and on amir-server
(since 2026-10-10), connected to Amir's dot (Ralph) as the "Coding sessions" and
"Coding sessions (home server)" plugins in the BrowserOS Work profile. This plan
records how it is built and why, so the next change starts from the real design.
Requirements are in [REQUIREMENTS.md](REQUIREMENTS.md); tests in
[TEST_PLAN.md](TEST_PLAN.md).

## How a question travels

1. Amir asks the dot (Ralph) "how's the SEO plan going?" from his phone or browser.
2. The dot, on OpenAI's servers, calls a tool on the "Coding sessions" plugin.
3. The call goes to `https://amir-m5.fairy-salmon.ts.net/<secret>/mcp`.
   Tailscale Funnel answers that public HTTPS address and forwards it to
   `127.0.0.1:8787` on this Mac. No router port is opened.
4. The connector checks the secret path, then answers from what is on disk and
   in Herdr at that moment. For a summary it calls Claude Haiku 5.5.
5. The dot turns the result into its reply.

## Components

| Piece | File | What it does |
| --- | --- | --- |
| Herdr reader | `src/amirmcp/herdr.py` | Lists every running Herdr session (`herdr session list --json`), then each session's workspaces, tabs, panes and each pane's foreground processes (`herdr pane process-info`). Pins `HERDR_SOCKET_PATH` per session so it never reads only the session it happens to run in. |
| Session map | `src/amirmcp/sessions.py` | Ties each pane to the exact agent session from the running process, works out live state, and adds sessions running outside Herdr or stopped recently. |
| Transcript reader | `src/amirmcp/transcripts.py` | Turns Claude Code JSONL and Codex rollout JSONL into short entries with a byte-offset cursor, reads tails, pages backwards and forwards, and searches. |
| Summaries | `src/amirmcp/summarize.py` | Builds the brief input (live facts, every request Amir made, recent transcript) and asks `claude-haiku-5-5` for a structured brief. Caches by transcript size and state in `~/.cache/amirmcp/briefs.json`. |
| MCP server | `src/amirmcp/server.py` | Six read-only tools over streamable HTTP (stateless, JSON responses), the secret-path gate, and request logging. |
| Service | `deploy/com.amir.amirmcp.plist`, `deploy/amirmcp.service`, `deploy/amirmcp-tailscale.service`, `scripts/install-service.sh` | On the Mac a launchd agent that starts at login; on amir-server a systemd user service that starts at boot (lingering is on). Both restart on failure and run the project's own `.venv/bin/amirmcp`, so starting needs no network; the install script runs `uv sync` first and turns on Tailscale Funnel. |
| Pane dump | `amirmcp-panes` (`herdr.dump`) | Prints this machine's Herdr panes as JSON, adding the client address (`ip:port`) of every SSH connection in a pane. amir-server runs it on the Mac over SSH. |

## Pane to session: the part that has to be right

Two agents in the same repo are common, so the link never comes from the
directory. It comes from the process in the pane:

- **Claude Code.** The pane's foreground process is the Claude binary
  (`~/.local/share/claude/versions/<version>`; Herdr does not recognise it as an
  agent because the process name is the version number). Claude writes
  `<home>/sessions/<pid>.json` with `sessionId`, `cwd`, and `status`. Each AI
  Manager account has its own home (`~/.aimgr/claude-homes/<account>/.claude`),
  so the home the record sits in is the account. The transcript is
  `<home>/projects/*/<sessionId>.jsonl`. A cross-account resume changes the
  session id, and the record follows it.
- **Codex.** The pane's foreground process is named `codex`. Codex logs every line
  with `pid:<pid>:<uuid>` and the thread id in `~/.codex/logs_*.sqlite`; the
  newest thread for that process, walked up `thread_spawn_edges` past subagents,
  is the session. `~/.codex/state_*.sqlite` gives its rollout file, title, branch.
  All Codex accounts share `~/.codex`; the account label comes from matching the
  rollout's `creator_account_id` to AI Manager's selection history in
  `~/.aimgr/local-state.json`, and is blank when that history has no match.

## Agents on amir-server, panes on the Mac

On amir-server ("home" on the tailnet) the agents run in SSH sessions that Amir
opens from Herdr panes on his Mac; the server has no Herdr. Each SSH connection
has a client port, and the agent's process on the server carries the same
address in `SSH_CONNECTION` (for example `100.72.74.74 50370 100.66.11.7 22`).
So the server's connector:

1. runs `ssh amir-m5 ~/workspace/amirmcp/.venv/bin/amirmcp-panes` (its own
   key, no agent forwarding), which lists the Mac's panes and, for each `ssh`
   process, its local `ip:port` from `lsof`;
2. reads each live agent's `SSH_CONNECTION` from `/proc/<pid>/environ`;
3. gives the agent the pane whose SSH connection has that address.

`AMIRMCP_HERDR_HOSTS=amir-m5` turns this on. If the Mac is asleep the server's
agents are still listed, with a gap saying their panes could not be read. The
Mac's own list names the panes that SSH elsewhere so the dot knows to ask the
other plugin.

## Live state

| State | Claude | Codex |
| --- | --- | --- |
| working | `status: busy`, or `status: shell` (turn ended, background jobs it started still running) | Herdr says working, or the rollout has a turn started and not finished |
| blocked | `status: waiting`, or its last tool call is an unanswered `AskUserQuestion` / `ExitPlanMode` | Herdr says blocked |
| idle | `status: idle`: finished its turn, waiting at the prompt | Herdr says idle or done |
| stopped | no running process; transcript written in the last 72 hours | same |
| unknown | anything else, said so | same |

Idle is never reported as finished. The summary is where "finished the work" vs
"stopped to ask" vs "gave up" gets decided, from the transcript.

## Tools

All carry `readOnlyHint: true`, so the dot does not ask Amir to confirm each call.

| Tool | Answers |
| --- | --- |
| `list_sessions(query?, include_stopped?)` | What is running, where, in what state. |
| `get_session(session, recent?)` | One session's location, state, Amir's requests, last entries. |
| `read_history(session, after_cursor?, before_cursor?, limit?, kinds?)` | A bounded slice; follow along or page back with cursors. |
| `search_history(query, session?)` | Find an error, PR, test or decision, with neighbouring entries. |
| `summarize_session(session, refresh?)` | Haiku brief: status, objective, progress marked said/did/verified with cursors, latest result, remaining, needs Amir, blockers, links, gaps. |
| `overview(query?)` | One line per live session and whether it needs Amir, needing-Amir first. |

Sessions resolve by id (`claude:d8e7a6ac`, `codex:fa93e68a`), pane id (`wH2:p1`), or
a loose name matched against titles, Herdr workspace and tab labels, directory,
account and first request. Several matches return the candidates instead of
guessing.

## Summaries

- Model `claude-haiku-5-5`, effort `medium`, structured output (`SessionBrief`).
- Input: live facts, all of Amir's requests in the session (up to 30k chars,
  newest kept), and the newest 150k chars of transcript (40k for `overview`).
- The system prompt teaches the reader and the stakes, and asks for intent,
  action and verified outcome to stay separate with cursor citations.
- Cache key: transcript path + size + state. A brief written from a wider window
  serves a narrower request, not the reverse.
- Measured on 2026-10-09: one brief 9 to 24 seconds; a cold overview of 40 live
  sessions 66 seconds with 16 workers, 40 seconds with 48.

## Connection and access

- Public endpoints by Tailscale Funnel, so URLs are stable across restarts and
  no inbound port is opened: `amir-m5.fairy-salmon.ts.net` for the Mac and
  `home-mcp.fairy-salmon.ts.net` for amir-server.
- ChatGPT only reaches port 443. Tested on 2026-10-10: the same Mac connector
  on Funnel port 8443 got ChatGPT's `424 Connection failed (kind: network)`
  while 443 worked.
- amir-server's own node (`home.fairy-salmon.ts.net`) already serves other
  tailnet-only services on 443 (`/`, `/health`, `/mcp/hand_builder`), and Funnel
  publishes a whole port. So the connector there has its own unprivileged
  Tailscale node, `home-mcp` (`amirmcp-tailscale` service, userspace networking,
  state in `~/.local/state/amirmcp/tailscale`), with key expiry turned off in the
  Tailscale admin. The main node's serve settings are unchanged.
- Access control is one secret: the first path segment. Any other path is a 404.
  The dot's plugin uses "No authentication", which the dot supports; it cannot
  send API keys or custom headers, and OAuth would be the heavier route the brief
  asked to avoid.
- The secret lives in `.env` (gitignored; the repo is public). Rotating it means
  a new `AMIRMCP_SECRET`, a service restart, and editing the plugin's URL.
- Alternative kept in reserve: OpenAI's Secure MCP Tunnel (`tunnel-client`)
  avoids any public address but needs an OpenAI Platform tunnel tied to the
  ChatGPT workspace.

## Operating it

- Install or restart on either machine: `./scripts/install-service.sh`.
- Restart after a code change: `launchctl kickstart -k gui/$(id -u)/com.amir.amirmcp`.
- Log: `~/Library/Logs/amirmcp/amirmcp.log`, rotated at 5 MB with five old
  files kept (about 30 MB at most), one line per request with the MCP method,
  tool, time, caller address and user agent (`openai-mcp/1.0.0` is the dot).
- Stop: `launchctl bootout gui/$(id -u)/com.amir.amirmcp` and
  `tailscale funnel --https=443 off`.
- After changing tools, press Refresh on the plugin in ChatGPT.

## Later, not now

- MCP Events, so the dot can say "tell me when X finishes": needs protocol
  version `2026-07-28`, `events/list|subscribe|unsubscribe`, stored
  subscriptions and signed webhook delivery.
- Remote control (prompting agents) as a separate, explicitly approved tool set.
