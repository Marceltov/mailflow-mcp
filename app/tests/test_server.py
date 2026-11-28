"""Unit tests against a fake MailFlow (httpx2.MockTransport). Nothing here touches a network."""

import json
import ssl

import httpx2 as httpx
import pytest
import truststore
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

import server

TOKEN = "mfat_" + "A" * 43
ACCOUNT = "11111111-1111-4111-8111-111111111111"
OTHER_ACCOUNT = "22222222-2222-4222-8222-222222222222"
ALIAS = "33333333-3333-4333-8333-333333333333"
MSG = "44444444-4444-4444-8444-444444444444"

ACCOUNTS = [
    {
        "id": ACCOUNT,
        "name": "3D Bruckner",
        "email_address": "info@3dbruckner.de",
        "signature_enabled": False,
        "auto_cc_addresses": [],
        "auto_bcc_addresses": ["archive@3dbruckner.de"],
        "aliases": [{"id": ALIAS, "name": "Sales", "email": "sales@3dbruckner.de", "reply_to": None}],
    },
    {"id": OTHER_ACCOUNT, "name": "Private", "email_address": "me@example.com", "signature_enabled": True, "aliases": []},
]

ORIGINAL = {
    "id": MSG,
    "uid": 7,
    "account_id": ACCOUNT,
    "folder": "INBOX",
    "message_id": "<reply2@t-online.de>",
    "in_reply_to": "<offer@3dbruckner.de>",
    "thread_references": "<offer@3dbruckner.de>",
    "subject": "AW: Ihr Angebot 3D-2026-0010 von 3D Bruckner",
    "from_name": "Manfred Kandziora",
    "from_email": "mkwigg@t-online.de",
    "to_addresses": [{"name": "", "email": "sales@3dbruckner.de"}],
    "reply_to": [],
    "delivery_addresses": [],
    "date": "2026-11-27T09:15:00.000Z",
    "spam_score_ml": 0.01,
    "account_color": "#fff",
}


class FakeMailFlow:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status: dict[str, int] = {}
        self.snoozed = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path in self.status:
            return httpx.Response(self.status[path], json={"error": "This API token does not have the draft scope"})
        if path == "/api/accounts":
            return httpx.Response(200, json=ACCOUNTS)
        if path == f"/api/mail/messages/{MSG}":
            return httpx.Response(200, json=ORIGINAL)
        if path == f"/api/mail/messages/{MSG}/body":
            return httpx.Response(
                200, json={"html": "<p>Hallo,</p><p>geht das?</p>", "text": None, "attachments": [{"filename": "a.pdf", "size": 12, "content": "x" * 999}]}
            )
        if path == "/api/mail/messages":
            return httpx.Response(200, json={"messages": [ORIGINAL], "total": 1})
        if path == "/api/mail/resolve-message":
            return httpx.Response(200, json={**ORIGINAL, "thread_id": "<offer@3dbruckner.de>"})
        if path == "/api/mail/thread":
            return httpx.Response(200, json={"messages": [ORIGINAL]})
        if path == "/api/search":
            return httpx.Response(200, json={"messages": [ORIGINAL], "query": request.url.params["q"]})
        if path == "/api/mail/unread-counts":
            return httpx.Response(200, json={"total": 3, "byAccount": {ACCOUNT: 3}, "complete": True, "snoozed": 0, "scheduled": {"pending": 0}})
        if path == "/api/mail/category-counts":
            return httpx.Response(200, json={"counts": {"primary": 2}, "totals": {"primary": 9}})
        if path.endswith(("/bulk-read", "/bulk-star")):
            return httpx.Response(200, json={"ok": True, "updated": [MSG]})
        if path.endswith("/bulk-archive"):
            return httpx.Response(200, json={"ok": True, "archived": [MSG], "noArchiveFolder": []})
        if path.endswith("/bulk-move"):
            return httpx.Response(200, json={"ok": True, "moved": [MSG]})
        if path == f"/api/mail/messages/{MSG}/snooze" and request.method == "POST" and self.snoozed:
            return httpx.Response(400, json={"error": "Message is already snoozed"})
        if path.endswith(("/snooze", "/category")):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/unsnooze"):
            return httpx.Response(200, json={"ok": True, "restored": 2})
        if path == "/api/mail/draft":
            return httpx.Response(200, json={"uid": 99, "folder": "Entwürfe", "messageId": "<new@3dbruckner.de>"})
        return httpx.Response(404, json={"error": "not faked"})

    def posted(self) -> dict:
        [post] = [c for c in self.calls if c.method == "POST"]
        return json.loads(post.content)

    def writes(self) -> list[tuple[str, str, dict]]:
        return [(c.method, c.url.path, json.loads(c.content) if c.content else {}) for c in self.calls if c.method != "GET"]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv(server.PUBLIC_URL_ENV, "https://mail.example.org")
    return FakeMailFlow()


@pytest.fixture
async def mcp(fake):
    client = httpx.AsyncClient(base_url="http://mailflow", auth=server.MailFlowTokenAuth(), transport=server.GuardedTransport(httpx.MockTransport(fake)))
    reset = server._incoming_token.set(TOKEN)
    try:
        async with Client(server.build_server(client)) as c:
            yield c
    finally:
        server._incoming_token.reset(reset)


async def call(mcp, name, **args):
    result = await mcp.call_tool(name, args)
    return result.data if result.data is not None else result.structured_content


# ── Safety ──────────────────────────────────────────────────────────────────


async def test_exactly_these_tools_and_no_send_or_delete(mcp):
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "list_accounts",
        "list_folders",
        "list_messages",
        "get_message",
        "get_thread",
        "search_messages",
        "search_contacts",
        "unread_counts",
        "category_counts",
        "mark_read",
        "star_messages",
        "archive_messages",
        "move_messages",
        "snooze_message",
        "unsnooze_message",
        "set_category",
        "create_draft",
    }
    assert not any("send" in n or "delete" in n or "trash" in n for n in names)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/mail/send"),
        ("POST", "/api/mail/messages/bulk-delete"),
        ("POST", "/api/mail/messages/bulk-spam"),
        ("PATCH", f"/api/mail/messages/{MSG}/read"),
        ("DELETE", "/api/mail/draft/5"),
        ("POST", "/api/auth/login"),
        ("GET", f"/api/mail/messages/{MSG}/raw"),
        ("GET", "/api/mail/messages/../send"),
    ],
)
def test_guard_refuses_everything_else(method, path):
    assert not server.call_allowed(method, path)


def test_no_allowed_call_sends():
    assert not any(w in p for _, p in server.ALLOWED_CALLS for w in ("send", "scheduled", "delete", "trash", "spam"))
    assert {m for m, _ in server.ALLOWED_CALLS} == {"GET", "POST", "PATCH"}


async def test_guarded_transport_raises_before_the_network(fake):
    client = httpx.AsyncClient(base_url="http://mailflow", transport=server.GuardedTransport(httpx.MockTransport(fake)))
    with pytest.raises(RuntimeError, match="does not call POST /api/mail/send"):
        await client.post("/api/mail/send", json={})
    assert fake.calls == []


# ── Auth ────────────────────────────────────────────────────────────────────


@pytest.fixture
def app(fake):
    client = httpx.AsyncClient(base_url="http://mailflow", auth=server.MailFlowTokenAuth(), transport=server.GuardedTransport(httpx.MockTransport(fake)))
    return server.build_app(server.build_server(client))


def test_health_is_open(app):
    with TestClient(app) as tc:
        assert tc.get("/health").text == "ok"


@pytest.mark.parametrize("header", [None, "Bearer", "Bearer secret", "Basic dXNlcjpwYXNz", "Bearer mfat_short", f"Token {TOKEN}"])
def test_mcp_endpoint_needs_a_mailflow_token(app, header):
    with TestClient(app) as tc:
        response = tc.post("/mcp", headers={"Authorization": header} if header else {}, json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", [f"Bearer {TOKEN}", TOKEN, f"bearer  {TOKEN}"])
def test_valid_token_passes_the_gate(app, header):
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    with TestClient(app) as tc:
        response = tc.post("/mcp", headers={"Authorization": header, "Accept": "application/json, text/event-stream"}, json=init)
    assert response.status_code == 200


async def test_token_is_forwarded_as_bearer(mcp, fake):
    await call(mcp, "list_accounts")
    assert fake.calls[0].headers["authorization"] == f"Bearer {TOKEN}"


# ── TLS (conventions: HTTPS clients) ────────────────────────────────────────


def test_client_uses_the_os_trust_store(monkeypatch):
    monkeypatch.delenv(server.VERIFY_TLS_ENV, raising=False)
    client = server.build_client()
    assert isinstance(client._transport.inner._pool._ssl_context, truststore.SSLContext)


def test_tls_check_can_be_turned_off(monkeypatch):
    monkeypatch.setenv(server.VERIFY_TLS_ENV, "false")
    client = server.build_client()
    assert client._transport.inner._pool._ssl_context.verify_mode == ssl.CERT_NONE


# ── Read tools ──────────────────────────────────────────────────────────────


async def test_list_messages_maps_params_and_trims_rows(mcp, fake):
    data = await call(mcp, "list_messages", account_id=ACCOUNT, folder="Archiv", unread_only=True, limit=10)
    params = fake.calls[0].url.params
    assert params["accountId"] == ACCOUNT and params["folder"] == "Archiv" and params["unreadOnly"] == "true" and params["limit"] == "10"
    assert "threaded" not in params
    row = data["messages"][0]
    assert row["subject"].startswith("AW:") and "spam_score_ml" not in row and "account_color" not in row and "uid" not in row
    assert data["total"] == 1


async def test_get_message_converts_html_and_drops_attachment_content(mcp):
    data = await call(mcp, "get_message", message_id=MSG)
    assert data["body"] == "Hallo,\n\ngeht das?"
    assert data["attachments"] == [{"filename": "a.pdf", "size": 12}]
    assert data["url"] == "https://mail.example.org/?m=%3Creply2%40t-online.de%3E"
    assert "guidance" not in data  # nothing to say


async def test_get_message_cuts_long_bodies(mcp):
    data = await call(mcp, "get_message", message_id=MSG, max_body_chars=5)
    assert data["body"] == "Hallo" and data["body_truncated"] is True
    assert "max_body_chars" in data["guidance"][0]


async def test_get_thread_resolves_the_thread_key(mcp, fake):
    data = await call(mcp, "get_thread", message_id=MSG)
    assert data["thread_id"] == "<offer@3dbruckner.de>"
    assert fake.calls[-1].url.params["id"] == "<offer@3dbruckner.de>"
    assert data["url"].endswith("?m=%3Creply2%40t-online.de%3E")


def test_message_url_falls_back_to_the_row_id(monkeypatch):
    monkeypatch.setenv(server.PUBLIC_URL_ENV, "https://mail.example.org/")
    assert server.message_url(None, MSG) == f"https://mail.example.org/?m={MSG}"
    assert server.message_url("not-a-message-id", None) is None


async def test_empty_search_and_list_say_what_to_try(mcp, monkeypatch):
    empty = httpx.Response(200, json={"messages": [], "total": 0})
    original = FakeMailFlow.__call__
    monkeypatch.setattr(
        FakeMailFlow, "__call__", lambda self, r: (self.calls.append(r), empty)[1] if r.url.path in ("/api/search", "/api/mail/messages") else original(self, r)
    )
    assert "in:trash" in (await call(mcp, "search_messages", query="zzz"))["guidance"][0]
    assert "unified inbox" in (await call(mcp, "list_messages"))["guidance"][0]


async def test_stale_counts_are_flagged(mcp, monkeypatch):
    original = FakeMailFlow.__call__
    stale = httpx.Response(200, json={"total": 1, "byAccount": {}, "complete": False})
    monkeypatch.setattr(FakeMailFlow, "__call__", lambda self, r: stale if r.url.path == "/api/mail/unread-counts" else original(self, r))
    assert "stale" in (await call(mcp, "unread_counts"))["guidance"][0]


async def test_search_passes_the_query(mcp, fake):
    await call(mcp, "search_messages", query='from:"Manfred Kandziora" after:2026-11-01')
    assert fake.calls[0].url.params["q"] == 'from:"Manfred Kandziora" after:2026-11-01'


async def test_unread_counts(mcp):
    data = await call(mcp, "unread_counts")
    assert data["total"] == 3 and data["by_account"] == {ACCOUNT: 3}


async def test_mailflow_errors_become_tool_errors(mcp, fake):
    fake.status["/api/accounts"] = 403
    with pytest.raises(ToolError, match=r"403.*draft scope.*lacks a scope"):
        await call(mcp, "list_accounts")


# ── Drafts ──────────────────────────────────────────────────────────────────


async def test_reply_draft_threads_into_the_conversation(mcp, fake):
    data = await call(mcp, "create_draft", reply_to_message_id=MSG, body="Guten Tag Herr Kandziora,\n\nleider nicht.")
    sent = fake.posted()
    assert sent["accountId"] == ACCOUNT
    assert sent["to"] == ["Manfred Kandziora <mkwigg@t-online.de>"]
    assert sent["subject"] == "AW: Ihr Angebot 3D-2026-0010 von 3D Bruckner"
    assert sent["inReplyTo"] == "<reply2@t-online.de>"
    assert sent["references"] == "<offer@3dbruckner.de> <reply2@t-online.de>"
    assert sent["aliasId"] == ALIAS  # addressed to sales@
    assert sent["bcc"] == ["archive@3dbruckner.de"]  # the account's automatic Bcc
    assert sent["editedSignature"] == ""  # signature off on this account
    assert sent["bodyIsHtml"] is False
    assert "> Hallo," in sent["quotedBody"] and "Manfred Kandziora <mkwigg@t-online.de> wrote:" in sent["quotedBody"]
    assert not {"existingUid", "existingFolder", "attachments", "fromScheduled", "quotedBodyHtml"} & sent.keys()
    assert data["saved"] is True and data["folder"] == "Entwürfe" and data["draft"]["from"] == "sales@3dbruckner.de"
    assert data["url"] == "https://mail.example.org/?m=%3Cnew%403dbruckner.de%3E"
    assert data["original_url"] == "https://mail.example.org/?m=%3Creply2%40t-online.de%3E"
    assert any("archive@3dbruckner.de" in g for g in data["guidance"]) and any("not sent" in g for g in data["guidance"])
    assert "next" not in data


async def test_preview_only_saves_nothing(mcp, fake):
    data = await call(mcp, "create_draft", reply_to_message_id=MSG, body="Text", preview_only=True)
    assert data["saved"] is False
    assert all(c.method == "GET" for c in fake.calls)
    assert data["draft"]["body"] == "Text" and data["draft"]["quoted_original"].startswith("---")
    assert "url" not in data and any("Nothing is saved yet" in g for g in data["guidance"])


async def test_reply_uses_reply_to_and_adds_re(mcp, fake, monkeypatch):
    monkeypatch.setitem(ORIGINAL, "reply_to", [{"name": "Kandziora, Manfred", "email": "office@example.de"}])
    monkeypatch.setitem(ORIGINAL, "subject", "Ihr Angebot")
    await call(mcp, "create_draft", reply_to_message_id=MSG, body="x", quote_original=False)
    sent = fake.posted()
    assert sent["to"] == ['"Kandziora, Manfred" <office@example.de>']
    assert sent["subject"] == "Re: Ihr Angebot"
    assert "quotedBody" not in sent


async def test_reply_refuses_another_account(mcp):
    with pytest.raises(ToolError, match="account the original is in"):
        await call(mcp, "create_draft", reply_to_message_id=MSG, account_id=OTHER_ACCOUNT, body="x")


async def test_new_draft_needs_an_account(mcp):
    with pytest.raises(ToolError, match="Give account_id"):
        await call(mcp, "create_draft", body="x", to=["a@example.com"])


async def test_new_draft_keeps_signature_when_account_has_it_on(mcp, fake):
    data = await call(mcp, "create_draft", account_id=OTHER_ACCOUNT, body="x", to=["a@example.com", "A <A@example.com>"], subject="Hi")
    sent = fake.posted()
    assert "editedSignature" not in sent and "inReplyTo" not in sent
    assert any("signature" in g for g in data["guidance"])
    assert sent["to"] == ["a@example.com"]  # same address once


def test_quote_block_matches_mailflow():
    block = server.quote_block(ORIGINAL, "a\nb")
    assert block.startswith("\n\n---\nOn ") and block.endswith("wrote:\n> a\n> b")


async def test_list_folders_drops_sync_state(mcp, monkeypatch):
    row = {"path": "INBOX", "name": "INBOX", "special_use": None, "total_count": 5, "unread_count": 1, "highest_modseq": "9", "uid_validity": 3}
    original = FakeMailFlow.__call__
    monkeypatch.setattr(FakeMailFlow, "__call__", lambda self, r: httpx.Response(200, json=[row]) if r.url.path.endswith("/folders") else original(self, r))
    [folder] = await call(mcp, "list_folders", account_id=ACCOUNT)
    assert folder == {"path": "INBOX", "name": "INBOX", "special_use": None, "total_count": 5, "unread_count": 1}


# ── Organize ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "args", "path", "body"),
    [
        ("mark_read", {"message_ids": [MSG]}, "/api/mail/messages/bulk-read", {"ids": [MSG], "read": True}),
        ("mark_read", {"message_ids": [MSG], "read": False}, "/api/mail/messages/bulk-read", {"ids": [MSG], "read": False}),
        ("star_messages", {"message_ids": [MSG]}, "/api/mail/messages/bulk-star", {"ids": [MSG], "starred": True}),
        ("archive_messages", {"message_ids": [MSG]}, "/api/mail/messages/bulk-archive", {"ids": [MSG]}),
        ("move_messages", {"message_ids": [MSG], "folder": "Paperless"}, "/api/mail/messages/bulk-move", {"ids": [MSG], "folder": "Paperless"}),
        ("set_category", {"message_id": MSG, "category": "newsletter"}, f"/api/mail/messages/{MSG}/category", {"category": "newsletter"}),
    ],
)
async def test_organize_tools_call_mailflow(mcp, fake, tool, args, path, body):
    await call(mcp, tool, **args)
    [(_, sent_path, sent)] = fake.writes()
    assert sent_path == path and sent == body


async def test_unchanged_messages_are_reported(mcp):
    other = "55555555-5555-4555-8555-555555555555"
    data = await call(mcp, "mark_read", message_ids=[MSG, other])
    assert data["updated"] == [MSG] and data["unchanged"] == [other] and data["guidance"]


async def test_snooze_needs_a_time_zone(mcp, fake):
    with pytest.raises(ToolError, match="time zone"):
        await call(mcp, "snooze_message", message_id=MSG, until="2026-12-01T08:00:00")
    assert fake.writes() == []


async def test_snooze_and_reschedule(mcp, fake):
    await call(mcp, "snooze_message", message_id=MSG, until="2026-12-01T08:00:00+01:00")
    assert fake.writes() == [("POST", f"/api/mail/messages/{MSG}/snooze", {"until": "2026-12-01T08:00:00+01:00"})]
    fake.calls.clear()
    fake.snoozed = True
    data = await call(mcp, "snooze_message", message_id=MSG, until="2026-12-02T08:00:00+01:00")
    assert data["rescheduled"] and [w[0] for w in fake.writes()] == ["POST", "PATCH"]


async def test_unsnooze(mcp, fake):
    data = await call(mcp, "unsnooze_message", message_id=MSG)
    assert data == {"unsnoozed": True, "restored": 2}


async def test_category_counts(mcp):
    assert await call(mcp, "category_counts") == {"unread": {"primary": 2}, "total": {"primary": 9}}


async def test_a_missing_scope_names_it(mcp, fake):
    fake.status["/api/mail/messages/bulk-archive"] = 403
    with pytest.raises(ToolError, match=r"403.*organize for flags"):
        await call(mcp, "archive_messages", message_ids=[MSG])


async def test_server_info_names_mailflows_icons(mcp):
    info = mcp.server_info
    assert info.name == "MailFlow"
    assert [i.src for i in info.icons] == [
        "https://mail.example.org/favicon.svg",
        "https://mail.example.org/icon-192.png",
        "https://mail.example.org/icon-512.png",
    ]
