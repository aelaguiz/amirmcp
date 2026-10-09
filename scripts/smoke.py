"""Call every connector tool against a running server and print what came back.

    uv run python scripts/smoke.py http://127.0.0.1:8787/<secret>/mcp
    uv run python scripts/smoke.py https://<public-host>/<secret>/mcp --overview

It talks to the server exactly as the dot does (MCP over streamable HTTP), so a
pass here means the tools work end to end against live sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from mcp import Client


def show(name: str, result, started: float) -> dict:
    took = time.time() - started
    if result.is_error:
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise SystemExit(f"FAIL {name} ({took:.1f}s): {text}")
    data = result.structured_content
    if data is None:
        data = json.loads(result.content[0].text)
    print(f"ok   {name} ({took:.1f}s)")
    return data


async def run(url: str, with_overview: bool) -> None:
    async with Client(url, read_timeout_seconds=300) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("tools:", ", ".join(names))
        assert all(t.annotations and t.annotations.read_only_hint for t in tools.tools), "every tool must be read-only"

        t = time.time()
        listed = show("list_sessions", await client.call_tool("list_sessions", {}), t)
        print("     counts:", listed["counts_by_state"], "gaps:", listed["gaps"])
        live = [s for s in listed["sessions"] if s["transcript"]]
        assert live, "expected at least one live session with a transcript"
        target = live[0]
        print("     first:", target["id"], "|", target["title"][:60], "|", target["where"])

        t = time.time()
        got = show("get_session", await client.call_tool("get_session", {"session": target["id"], "recent": 5}), t)
        print("     recent kinds:", [e["kind"] for e in got["recent"]])

        t = time.time()
        hist = show("read_history", await client.call_tool("read_history", {"session": target["id"], "limit": 10}), t)
        first = hist["entries"][0]["cursor"]
        t = time.time()
        back = show(
            "read_history before",
            await client.call_tool("read_history", {"session": target["id"], "before_cursor": first, "limit": 5}),
            t,
        )
        assert all(e["cursor"] < first for e in back["entries"]), "paging back must return earlier entries"

        word = target["title"].split()[0] if target["title"] else "the"
        t = time.time()
        found = show("search_history", await client.call_tool("search_history", {"query": word, "session": target["id"], "limit": 3}), t)
        print(f"     hits for {word!r}:", len(found["hits"]))

        t = time.time()
        brief = show("summarize_session", await client.call_tool("summarize_session", {"session": target["id"]}), t)
        print("     status:", brief.get("brief", {}).get("status", brief.get("error")))
        print("     needs Amir:", brief.get("brief", {}).get("needs_amir"))

        if with_overview:
            t = time.time()
            ov = show("overview", await client.call_tool("overview", {}), t)
            print("     sessions:", len(ov["sessions"]), "needing Amir:", ov["needs_amir_count"])
            for row in ov["sessions"][:5]:
                print("     -", row["id"], row["state"], "|", (row.get("needs_amir") or row.get("error") or "")[:100])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--overview", action="store_true", help="also run the overview tool (summarizes every live session)")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.overview))


if __name__ == "__main__":
    main()
