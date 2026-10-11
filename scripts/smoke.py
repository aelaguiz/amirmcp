"""Call every connector tool against a running server and print what came back.

    uv run python scripts/smoke.py http://127.0.0.1:8787/<secret>/mcp
    uv run python scripts/smoke.py https://<public-host>/<secret>/mcp --overview
    uv run python scripts/smoke.py <url> --send amirmcp-lab:w1:p1

It talks to the server exactly as the dot does (MCP over streamable HTTP), so a
pass here means the tools work end to end against live sessions. It only reads,
unless --send names a test agent to send one message to.
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


async def run(url: str, with_overview: bool, send_to: str | None) -> None:
    async with Client(url, read_timeout_seconds=300) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("tools:", ", ".join(names))
        acting = {t.name for t in tools.tools if not (t.annotations and t.annotations.read_only_hint)}
        # The read-only endpoint (AMIRMCP_READ_SECRET) has neither pane tool.
        assert acting in ({"send_message", "press_keys"}, set()), f"only the two pane tools may act, got {acting}"
        assert not (send_to and not acting), "--send needs the full endpoint; this one is read-only"

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

        in_pane = [s for s in live if isinstance(s["where"], dict)]
        if in_pane:
            t = time.time()
            shown = show("read_screen", await client.call_tool("read_screen", {"session": in_pane[0]["id"]}), t)
            print("     input box:", shown["input_box"], "| last line:", shown["screen"].strip().splitlines()[-1][:80])

        if send_to:
            t = time.time()
            sent = show("send_message", await client.call_tool("send_message", {"session": send_to, "text": "Smoke test: reply with exactly SMOKE OK."}), t)
            print("     result:", sent["result"], sent.get("cursor") or sent.get("detail") or sent.get("reason"))

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
    parser.add_argument("--send", metavar="SESSION", help="send one test message to this (test) agent")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.overview, args.send))


if __name__ == "__main__":
    main()
