# amirmcp

An MCP connector that lets Amir's OpenAI dot (Ralph), which runs on OpenAI's
servers, see the Claude Code and Codex sessions running on his machines, with
the Herdr pane each one is in, and talk to them for him. From anywhere he can
ask "how's the SEO plan going?" or "what's waiting on me?" and get an answer
grounded in the agents' own transcripts, with summaries written by Claude
Haiku 5.5, then say "tell it to go ahead" and Ralph types that into the
agent's input box. It never starts, stops or restarts agents, and never changes
a file.

One copy runs on each machine where agents run, and each is its own plugin in
the dot (ChatGPT, BrowserOS Work profile, chatgpt.com/plugins):

| Machine | ChatGPT plugin | Public URL | Runs as |
| --- | --- | --- | --- |
| Amir-M5 (MacBook Pro) | Coding sessions | `https://amir-m5.fairy-salmon.ts.net/<secret>/mcp` | launchd agent `com.amir.amirmcp` |
| amir-server ("home") | Coding sessions (home server) | `https://home-mcp.fairy-salmon.ts.net/<secret>/mcp` | systemd user services `amirmcp` and `amirmcp-tailscale` |

Agents on amir-server run in SSH sessions opened from Herdr panes on the Mac.
The server's copy reads the Mac's panes over SSH and matches each agent to its
pane by the SSH connection, so the dot sees "rustai → subgame, pane w15:p1" for
an agent running on the server.

- [Requirements](docs/REQUIREMENTS.md): what the dot must be able to do.
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md): how it works and why.
- [Test plan](docs/TEST_PLAN.md): how to check it, from unit tests to the dot.
- [Sending to agents](docs/SENDING_TO_AGENTS.md): how the dot types into agents' panes safely, and the experiments behind it.

## What the dot can call

| Tool | For |
| --- | --- |
| `overview` | "What's going on?" One line per live session, those needing Amir first. |
| `summarize_session` | "How's X going?" A Haiku brief with status, progress marked said / did / verified, what it needs from Amir, links, and cursors into the transcript. |
| `list_sessions` | Which sessions exist, where they run in Herdr, their state and account. |
| `get_session` | One session's state, Amir's requests in it, and its last entries. |
| `read_history` | A bounded slice of a transcript, paged by cursor. |
| `search_history` | Find an error, PR, test or decision across transcripts. |
| `read_screen` | What an agent's pane shows right now, such as a question or approval it is waiting on. |
| `send_message` | "Tell it to ..." Types Amir's message into the agent's input box, prefixed "From Amir, via ChatGPT:", and confirms the agent got it. Refuses when a prompt is showing, Amir has unsent text there, or the agent has exited. |
| `press_keys` | Answers a question, menu or approval the agent is showing ("2", "y", "esc"), optionally with typed text. |

Sending works for agents on amir-server too: the server's copy types through
the Mac's Herdr pane over SSH. Why each check exists, and what failed without
it, is in [Sending to agents](docs/SENDING_TO_AGENTS.md).

## Run it on a machine

Needs `uv`, Tailscale with Funnel allowed for the machine, and either `herdr`
on the machine or SSH access to the machine whose Herdr panes SSH into it.

```bash
git clone https://github.com/aelaguiz/amirmcp ~/workspace/amirmcp && cd ~/workspace/amirmcp
cp .env.example .env          # key, a new secret, machine name; see the comments
./scripts/install-service.sh  # service + Tailscale Funnel; prints the URL
```

Then in ChatGPT: Plugins, Add, Add custom MCP server, with that URL and "No
authentication", then Connect. ChatGPT only reaches port 443. On a machine whose
Tailscale 443 already serves other things (amir-server), set
`AMIRMCP_TAILSCALE_NODE` so the connector gets its own Tailscale node; approve
its login link once and turn off its key expiry in the Tailscale admin.

Both copies stay up on their own: they start at login (Mac) or boot
(amir-server), restart within about 10 seconds if they die, and keep their
Funnel address. Logs rotate at 5 MB and keep five old files. The Mac's copy is
reachable only while the Mac is awake; amir-server's lists its agents either
way but shows their Herdr panes only while the Mac is reachable.

| Task | Mac | amir-server |
| --- | --- | --- |
| Restart after a code change | `launchctl kickstart -k gui/$(id -u)/com.amir.amirmcp` | `systemctl --user restart amirmcp` |
| Watch calls (the dot shows as `ua=openai-mcp/1.0.0`) | `tail -f ~/Library/Logs/amirmcp/amirmcp.log` | `tail -f ~/.local/state/amirmcp/amirmcp.log` |
| Stop | `launchctl bootout gui/$(id -u)/com.amir.amirmcp && tailscale funnel --https=443 off` | `systemctl --user disable --now amirmcp amirmcp-tailscale` |

| Task | Command |
| --- | --- |
| Unit tests | `uv run pytest` |
| Call every tool like the dot does (reads only) | `uv run python scripts/smoke.py https://<host>/<secret>/mcp` |
| Also send one message to a test agent | add `--send <session>` |

After changing tools or a server's description, press Refresh on the plugin in
ChatGPT so the dot sees the new version.
