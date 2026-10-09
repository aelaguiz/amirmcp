# Test plan: coding-session connector

The connector is only working when Amir's dot answers a real question from
real sessions. Everything below builds up to that, from the parts that are
cheap to check on every change to the one check that proves the whole path.
Requirements are in [REQUIREMENTS.md](REQUIREMENTS.md); design in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## 1. Unit tests (every change)

`uv run pytest` runs `tests/`. They use small transcript files written by the
tests in the exact shapes Claude Code and Codex write, plus fake Claude session
records, so they run anywhere in about a second.

| Area | What is checked |
| --- | --- |
| Claude parsing | user, assistant, tool call and tool result entries; mid-turn messages (`queued_command`) count as Amir's requests only when `origin.kind` is human; slash commands and task notifications become events; meta and sidechain lines are skipped. |
| Codex parsing | `item_completed` user and agent messages, commands with exit code and output, MCP calls, file changes, turn started and finished. |
| Cursors | tail returns the newest entries in order; `before_cursor` returns strictly earlier entries; `after_cursor` returns strictly later ones and a `next_cursor` that picks up where it stopped; a half-written last line is ignored. |
| Search | case-insensitive hits with the entry before and after. |
| Claude state | busy and shell are working, waiting is blocked, idle is idle, an unanswered `AskUserQuestion` is blocked. |
| Short ids | two Codex threads started the same minute get different short ids. |
| Session lookup | id, id prefix, pane id and loose names resolve; several matches return all of them. |
| Secret path | requests outside `/<secret>/` get a 404; requests under it reach the app with the prefix removed. |

## 2. Against live data on this Mac (after any change to the session map)

```bash
uv run python -c "from amirmcp import sessions; s = sessions.snapshot(24); print(len(s.sessions), s.gaps)"
```

Pass: every Herdr pane that has a Claude or Codex process shows up with the
right Herdr session, workspace label and pane id; no gaps; it takes a few
seconds. Spot-check two panes by eye in Herdr: the title and state match what
the pane shows. On 2026-10-09 this found 49 panes, 42 live agents (37 Claude, 8
Codex counted by process; the 42 includes one Claude running outside Herdr and
excludes idle shells), and 171 sessions stopped in the last 24 hours, in 2.2 s.

## 3. MCP client against the running service

```bash
uv run python scripts/smoke.py "http://127.0.0.1:8787/<secret>/mcp"
uv run python scripts/smoke.py "https://amir-m5.fairy-salmon.ts.net/<secret>/mcp" --overview
```

`smoke.py` speaks MCP the way the dot does. It checks that all six tools are
listed as read-only, then calls each one against a live session, checks that
paging back returns earlier entries, and prints the Haiku brief's status and
"needs Amir" line. `--overview` also summarizes every live session.

Pass: every line says `ok`; the brief's status matches what the pane shows;
`curl https://amir-m5.fairy-salmon.ts.net/mcp` (no secret) returns 404.

Results on 2026-10-09: all tools ok locally and through the public URL; a brief
took 9 to 24 s; a cold overview of 40 sessions took 66 s and found 16 needing
Amir; wrong and missing secrets returned 404.

## 4. Summary quality (after any change to the prompt or input)

Pick three live sessions of different kinds: one that finished and is waiting,
one mid-work, one that asked Amir something. For each, read the transcript tail
yourself, then call `summarize_session` and check:

- the status says what the pane shows, with the right time;
- "needs Amir" quotes the actual question, or says nothing is needed;
- progress keeps said, did and verified apart, and cites cursors that open to
  the claimed entry (`read_history` with `before_cursor` = cursor + 1);
- links are copied exactly and are real.

Example from 2026-10-09: the PR-agent session's brief named the two Docker Hub
workflow PRs and quoted the question waiting on Amir ("May I apply
ufc-approved and merge these two workflow fixes?") with its cursor.

## 5. The dot, in the BrowserOS Work profile (after any change to tools or connection)

1. In BrowserOS, Work profile, open `chatgpt.com/plugins`. "Coding sessions"
   is installed and its app shows Connected. After changing tools, open it and
   press Refresh.
2. Open the dot (Ralph) and ask in plain words, for example "how many of my
   coding sessions are working, and which ones are waiting on me?"
3. In `~/Library/Logs/amirmcp/amirmcp.log`, the calls arrive with `ua=openai-mcp/1.0.0`
   from OpenAI addresses, not from this Mac.
4. Ralph's answer names real sessions with their Herdr spaces and matches
   what `list_sessions` and the panes show.
5. Ask a follow-up about one named session ("how's the SEO 10x plan going?")
   and check the answer cites what the transcript shows.

Pass: Ralph answers from the connector (step 3) and the answer is right (steps
4 and 5).

Results on 2026-10-09, BrowserOS Work profile, Ralph at `chatgpt.com/dots/home`:

| Question | Calls that reached the connector (`ua=openai-mcp/1.0.0`) | Ralph's answer |
| --- | --- | --- |
| "How many are working, and which ones are waiting on me?" | `list_sessions`, `overview` (40 s), then 12 parallel `get_session` checks | "41 live sessions, 17 working and 24 idle. Thirteen flag something for you", then the queue by name, e.g. "PR-agent Docker fixes: approve mobile #7464 and Rust #962". |
| "How's the SEO 10x plan session going? What has it actually verified, and what's left?" | `list_sessions(query="SEO 10x")`, `get_session`, `summarize_session`, then `read_history` and `search_history` to check the brief's claims | Separated verified work (robots.txt and 831 sitemap URLs, layout checks, 271/271 localization entries, 32 tests) from what is left, and noted that green CI does not cover the current head. |

Both answers matched `list_sessions` and the session's transcript.

## 6. Service resilience (after changing the service setup)

- Rerun `./scripts/install-service.sh` while the service is up: it replaces it
  cleanly and the smoke test passes.
- `kill -9` the process: launchd restarts it within about 10 s.
- After a reboot and login: the service and Funnel are both up
  (`launchctl print gui/$(id -u)/com.amir.amirmcp`, `tailscale funnel status`).

Results on 2026-10-09: installer rerun with the service up worked (the first
version raced launchd's unload and failed with "Bootstrap failed: 5"; the script
now waits for the unload); after `kill -9` a new process was serving in 8 s; the
smoke test passed through the public URL afterwards. Tailscale's own setting
`TailscaleStartOnLogin` is on, and the Funnel setting is stored by Tailscale, so
both come back at login. A real reboot has not been tried yet.
