"""Read-only smoke test against a real MailFlow: calls each read tool once. Run after a MailFlow update.

    cd app && MAILFLOW_URL=https://mail.example.org MAILFLOW_TOKEN="$(op read 'op://…/credential')" uv run python ../dev/live-smoke.py

It prints shapes and counts, never mail content. It calls no tool that writes: create_draft is left
out on purpose.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from fastmcp import Client

import server

READ_TOOLS = {"list_accounts", "list_folders", "list_messages", "get_message", "get_thread", "search_messages", "search_contacts", "unread_counts"}


def shape(value) -> str:
    if isinstance(value, list):
        return f"list[{len(value)}]" + (f" of {sorted(value[0])}" if value and isinstance(value[0], dict) else "")
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {shape(v) if isinstance(v, (list, dict)) else type(v).__name__}" for k, v in value.items()) + "}"
    return type(value).__name__


async def main() -> int:
    token = os.environ.get("MAILFLOW_TOKEN", "").strip()
    if not server.TOKEN_RE.match(token) or not os.environ.get(server.MAILFLOW_URL_ENV):
        print("Set MAILFLOW_URL and MAILFLOW_TOKEN (mfat_…).", file=sys.stderr)
        return 2
    server._incoming_token.set(token)
    client = server.build_client()
    me = (await client.get("/api/api-tokens/self")).json()
    print(f"token: scopes={me.get('scopes')} expires={me.get('expires_at') or me.get('expiresAt')}")
    failures = 0
    async with Client(server.build_server(client)) as mcp:
        assert {t.name for t in await mcp.list_tools()} == READ_TOOLS | {"create_draft"}

        async def run(name, **args):
            nonlocal failures
            assert name in READ_TOOLS
            try:
                result = await mcp.call_tool(name, args)
            except Exception as err:  # report every tool, then fail
                failures += 1
                print(f"FAIL {name}: {err}")
                return None
            data = result.data if result.data is not None else result.structured_content
            print(f"ok   {name}: {shape(data)}")
            return data

        accounts = await run("list_accounts") or []
        first = accounts[0]["id"] if accounts else None
        if first:
            await run("list_folders", account_id=first)
        listing = await run("list_messages", limit=3) or {}
        messages = listing.get("messages") or []
        if messages:
            await run("get_message", message_id=messages[0]["id"], max_body_chars=200)
            await run("get_thread", message_id=messages[0]["id"])
        await run("search_messages", query="after:2026-01-01", limit=3)
        await run("search_contacts", query="info")
        await run("unread_counts")
    print("all read tools answered" if not failures else f"{failures} tool(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
