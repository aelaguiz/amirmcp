# amirmcp

A read-only MCP connector that lets Amir's OpenAI dot (Ralph), which runs on
OpenAI's servers, see the Claude Code and Codex sessions running in his Herdr
spaces on Amir-M5. From anywhere he can ask "how's the SEO plan going?" or
"what's waiting on me?" and get an answer grounded in the agents' own
transcripts, with summaries written by Claude Haiku 5.5.

It is installed in the dot as the **Coding sessions** plugin (ChatGPT, BrowserOS
Work profile, chatgpt.com/plugins). It never types into a pane or changes a file.

- [Requirements](docs/REQUIREMENTS.md): what the dot must be able to do.
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md): how it works and why.
- [Test plan](docs/TEST_PLAN.md): how to check it, from unit tests to the dot.

## What the dot can call

| Tool | For |
| --- | --- |
| `overview` | "What's going on?" One line per live session, those needing Amir first. |
| `summarize_session` | "How's X going?" A Haiku brief with status, progress marked said / did / verified, what it needs from Amir, links, and cursors into the transcript. |
| `list_sessions` | Which sessions exist, where they run in Herdr, their state and account. |
| `get_session` | One session's state, Amir's requests in it, and its last entries. |
| `read_history` | A bounded slice of a transcript, paged by cursor. |
| `search_history` | Find an error, PR, test or decision across transcripts. |

## Run it

Needs `uv`, `herdr`, and Tailscale with Funnel allowed for this Mac.

```bash
cp .env.example .env          # fill in ANTHROPIC_API_KEY and AMIRMCP_SECRET
uv sync
./scripts/install-service.sh  # launchd service + Tailscale Funnel; prints the URL
```

The public URL is `https://amir-m5.fairy-salmon.ts.net/<AMIRMCP_SECRET>/mcp`.
Paths without the secret get a 404. In ChatGPT the plugin is added with
Plugins, then Add, then Add custom MCP server, with that URL and "No
authentication".

| Task | Command |
| --- | --- |
| Restart after a code change | `launchctl kickstart -k gui/$(id -u)/com.amir.amirmcp` |
| Watch calls (the dot shows as `ua=openai-mcp/1.0.0`) | `tail -f ~/Library/Logs/amirmcp.log` |
| Unit tests | `uv run pytest` |
| Call every tool like the dot does | `uv run python scripts/smoke.py http://127.0.0.1:8787/<secret>/mcp` |
| Stop | `launchctl bootout gui/$(id -u)/com.amir.amirmcp && tailscale funnel --https=443 off` |

After changing tools, press Refresh on the plugin in ChatGPT so the dot sees
the new list.
