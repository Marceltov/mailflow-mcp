"""mailflow-mcp: an MCP server for MailFlow webmail over its personal API tokens.

It reads mail and saves drafts. It never sends: MailFlow's token API has no route that sends, and
this server only calls the routes listed in ALLOWED_CALLS. The server holds no secret: each client
sends its own MailFlow API token (`mfat_…`), which is forwarded to MailFlow as
`Authorization: Bearer <token>` (MailFlow's backend/src/middleware/apiTokenAuth.js).
"""

from __future__ import annotations

import contextlib
import html
import os
import re
import ssl
import sys
from contextvars import ContextVar
from datetime import datetime
from html.parser import HTMLParser
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx2 as httpx
import truststore
import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# ── Configuration (environment only) ─────────────────────────────────────────

MAILFLOW_URL_ENV = "MAILFLOW_URL"  # MailFlow's backend, e.g. http://mailflow-backend:3000
PUBLIC_URL_ENV = "MAILFLOW_PUBLIC_URL"  # where the user opens MailFlow, for links; default MAILFLOW_URL
VERIFY_TLS_ENV = "MAILFLOW_VERIFY_TLS"  # "false" turns certificate checks off (never the default)
TIMEOUT_ENV = "MAILFLOW_TIMEOUT"  # seconds per MailFlow call
MCP_HOST_ENV = "MCP_HOST"
MCP_PORT_ENV = "MCP_PORT"
MCP_PATH_ENV = "MCP_PATH"
MCP_ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"  # comma-separated Host allowlist (see serve)

DEFAULT_MAILFLOW_URL = "http://mailflow-backend:3000"
DEFAULT_TIMEOUT = 60.0
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8082
DEFAULT_PATH = "/mcp"
HEALTH_PATH = "/health"

# A body longer than this is cut, so one newsletter can't fill the client's context.
DEFAULT_BODY_CHARS = 20_000

# MailFlow's token format (services/apiTokens.js). Anything else is refused here, so a client
# can never get some other secret forwarded to MailFlow.
TOKEN_RE = re.compile(r"^mfat_[A-Za-z0-9_-]{43}$")

# Every MailFlow call this server may make: (method, path pattern). A subset of MailFlow's
# apiTokenAllowlist.js. The transport refuses anything else before it leaves the process, so a
# bug here can never reach a send, delete or settings route.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
ALLOWED_CALLS = (
    ("GET", r"/api/health"),
    ("GET", r"/api/api-tokens/self"),
    ("GET", r"/api/accounts"),
    ("GET", rf"/api/accounts/{_UUID}/folders"),
    ("GET", r"/api/mail/messages"),
    ("GET", rf"/api/mail/messages/{_UUID}"),
    ("GET", rf"/api/mail/messages/{_UUID}/body"),
    ("GET", r"/api/mail/resolve-message"),
    ("GET", r"/api/mail/thread"),
    ("GET", r"/api/mail/unread-counts"),
    ("GET", r"/api/search"),
    ("GET", r"/api/search/contacts"),
    ("POST", r"/api/mail/draft"),
)
_ALLOWED = tuple((method, re.compile(f"^{pattern}$")) for method, pattern in ALLOWED_CALLS)


def call_allowed(method: str, path: str) -> bool:
    return any(method == m and rx.match(path) for m, rx in _ALLOWED)


def mailflow_url() -> str:
    return os.environ.get(MAILFLOW_URL_ENV, DEFAULT_MAILFLOW_URL).strip().rstrip("/")


def public_url() -> str:
    return (os.environ.get(PUBLIC_URL_ENV, "").strip() or mailflow_url()).rstrip("/")


def message_url(message_id: str | None, row_id: str | None = None) -> str | None:
    """A link that opens the message in MailFlow (`/?m=`, MailFlow #270). The Message-ID header is
    the durable reference; the row UUID changes when the message moves folder."""
    ref = message_id if message_id and MESSAGE_ID_RE.fullmatch(message_id) else row_id
    return f"{public_url()}/?m={quote(ref, safe='')}" if ref else None


def verify_tls() -> ssl.SSLContext | bool:
    """The OS trust store (conventions: HTTPS clients), unless explicitly turned off."""
    if os.environ.get(VERIFY_TLS_ENV, "").strip().lower() in ("false", "0", "no"):
        return False
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


# ── Auth: forward the client's token ────────────────────────────────────────

# The client's MailFlow token for the request being served, set by TokenCaptureMiddleware.
_incoming_token: ContextVar[str | None] = ContextVar("incoming_token", default=None)


def bare_token(authorization: str) -> str:
    """The token from an Authorization value sent raw or as `Bearer <token>`."""
    scheme, _, rest = authorization.strip().partition(" ")
    return rest.strip() if scheme.lower() == "bearer" else authorization.strip()


class MailFlowTokenAuth(httpx.Auth):
    """Send the client's own token to MailFlow as `Authorization: Bearer mfat_…`."""

    def auth_flow(self, request):
        token = _incoming_token.get()
        if not token:
            raise RuntimeError("No MailFlow API token for this request.")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class GuardedTransport(httpx.AsyncBaseTransport):
    """Refuse every call that is not in ALLOWED_CALLS, then hand it to the real transport."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request):
        if not call_allowed(request.method, request.url.path):
            raise RuntimeError(f"mailflow-mcp does not call {request.method} {request.url.path}")
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class TokenCaptureMiddleware:
    """Pure ASGI (so the streamable-HTTP response isn't buffered): require a MailFlow token on
    every request except /health and keep it for the outgoing calls. Whether the token is valid,
    unexpired and has the scope is MailFlow's to decide on each call."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        token = bare_token(headers.get(b"authorization", b"").decode("latin-1"))
        if not TOKEN_RE.match(token):
            error = b'{"error":"send your MailFlow API token (mfat_...) as Authorization: Bearer <token>"}'
            headers = [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]
            await send({"type": "http.response.start", "status": 401, "headers": headers})
            await send({"type": "http.response.body", "body": error})
            return
        reset = _incoming_token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _incoming_token.reset(reset)


# ── MailFlow calls ──────────────────────────────────────────────────────────


def build_client() -> httpx.AsyncClient:
    verify = verify_tls()
    timeout = float(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT))
    return httpx.AsyncClient(
        base_url=mailflow_url(),
        auth=MailFlowTokenAuth(),
        transport=GuardedTransport(httpx.AsyncHTTPTransport(verify=verify)),
        verify=verify,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )


def _drop_none(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


def _error_text(response) -> str:
    try:
        body = response.json()
        message = body.get("error") if isinstance(body, dict) else None
    except ValueError:
        message = None
    message = message or response.text[:300] or response.reason_phrase
    hint = {
        401: " The MailFlow API token is invalid or expired: create a new one in MailFlow (Settings → API tokens).",
        403: " The token may lack the scope this needs (read for reading, draft for drafts).",
        429: f" Rate limited; retry after {response.headers.get('Retry-After', 'a while')} seconds.",
    }.get(response.status_code, "")
    return f"MailFlow answered {response.status_code}: {message}.{hint}"


class MailFlow:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        try:
            response = await self.client.request(method, path, params=_drop_none(params or {}), json=json)
        except httpx.RequestError as err:
            raise ToolError(f"Could not reach MailFlow at {mailflow_url()}: {err}") from err
        if response.status_code >= 400:
            raise ToolError(_error_text(response))
        return response.json()

    async def get(self, path: str, **params) -> Any:
        return await self.request("GET", path, params=params)


# ── Shaping responses ───────────────────────────────────────────────────────

# What a model needs of a message row; MailFlow's rows also carry colours, spam scores, IMAP uids
# and unsubscribe headers.
MESSAGE_FIELDS = (
    "id",
    "account_id",
    "account_name",
    "account_email",
    "folder",
    "thread_id",
    "message_id",
    "subject",
    "from_name",
    "from_email",
    "to_addresses",
    "cc_addresses",
    "reply_to",
    "date",
    "snippet",
    "is_read",
    "is_starred",
    "has_attachments",
    "category",
    "is_draft",
    "in_trash",
    "message_count",
)


# Of a folder row: what a model needs. MailFlow's rows also carry IMAP sync state (modseq, uid
# validity, revisions, timestamps).
FOLDER_FIELDS = ("path", "name", "special_use", "no_select", "total_count", "unread_count", "counts_stale")


def summarize(message: dict) -> dict:
    out = {k: message[k] for k in MESSAGE_FIELDS if k in message and message[k] not in (None, [], "")}
    for key in ("is_read", "is_starred", "has_attachments"):
        if key in message:
            out[key] = bool(message[key])
    return out


class _TextExtractor(HTMLParser):
    BLOCKS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def body_text(body: dict) -> str:
    text = (body.get("text") or "").strip()
    if text:
        return text
    return html_to_text(body.get("html") or "")


def attachment_info(attachment: Any) -> Any:
    if not isinstance(attachment, dict):
        return attachment
    return {k: v for k, v in attachment.items() if isinstance(v, (str, int, float, bool)) and not (isinstance(v, str) and len(v) > 300)}


def cut(text: str, limit: int) -> tuple[str, bool]:
    if limit > 0 and len(text) > limit:
        return text[:limit], True
    return text, False


# ── Drafts ──────────────────────────────────────────────────────────────────

MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
REPLY_PREFIX_RE = re.compile(r"^\s*(re|aw|antw|sv|vs|wg|fw|fwd)\s*:", re.IGNORECASE)


def format_address(entry: Any) -> str | None:
    """`Name <email>` for MailFlow's {name, email} objects, quoted where the name needs it."""
    if isinstance(entry, str):
        return entry.strip() or None
    if not isinstance(entry, dict):
        return None
    email = (entry.get("email") or entry.get("address") or "").strip()
    if not email:
        return None
    name = re.sub(r'[\r\n"<>]+', " ", entry.get("name") or "").strip()
    if not name:
        return email
    if re.search(r"[,;:@()\[\]\\.]", name):
        name = f'"{name}"'
    return f"{name} <{email}>"


def email_of(address: str) -> str:
    match = re.search(r"<([^>]+)>", address)
    return (match.group(1) if match else address).strip().lower()


def merge_addresses(*lists: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for entries in lists:
        for address in entries or []:
            key = email_of(address)
            if key and key not in seen:
                seen.add(key)
                out.append(address)
    return out


def reply_subject(subject: str | None) -> str:
    subject = (subject or "").strip()
    if not subject:
        return "Re:"
    return subject if REPLY_PREFIX_RE.match(subject) else f"Re: {subject}"


def reply_references(original: dict) -> str | None:
    chain = MESSAGE_ID_RE.findall(original.get("thread_references") or "") or MESSAGE_ID_RE.findall(original.get("in_reply_to") or "")
    own = original.get("message_id")
    if own and MESSAGE_ID_RE.fullmatch(own):
        chain.append(own)
    unique = list(dict.fromkeys(chain))
    return " ".join(unique) or None


def reply_recipients(original: dict) -> list[str]:
    reply_to = [a for a in (format_address(e) for e in original.get("reply_to") or []) if a]
    if reply_to:
        return reply_to[:1]
    sender = format_address({"name": original.get("from_name"), "email": original.get("from_email")})
    return [sender] if sender else []


def pick_alias(account: dict, original: dict) -> str | None:
    """The alias the original was addressed to, as MailFlow's composer picks it."""
    aliases = {(a.get("email") or "").lower(): a.get("id") for a in account.get("aliases") or [] if a.get("email")}
    if not aliases:
        return None
    addressed = [*(original.get("delivery_addresses") or [])]
    for field in ("to_addresses", "cc_addresses"):
        addressed += [e.get("email") for e in original.get(field) or [] if isinstance(e, dict)]
    for address in addressed:
        if isinstance(address, str) and address.lower() in aliases:
            return aliases[address.lower()]
    return None


def quote_block(original: dict, text: str) -> str:
    """The quoted original as MailFlow's composer writes it (frontend utils/composeFromMessage.js)."""
    when = original.get("date") or ""
    with contextlib.suppress(ValueError):
        when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone().strftime("%d.%m.%Y %H:%M")
    author = format_address({"name": original.get("from_name"), "email": original.get("from_email")}) or ""
    quoted = "\n".join("> " + line for line in text.split("\n"))
    return f"\n\n---\nOn {when}, {author} wrote:\n{quoted}"


# ── Server ──────────────────────────────────────────────────────────────────

INSTRUCTIONS = """\
Tools for the user's MailFlow webmail, which brings all their mailboxes together. You can read \
mail and save drafts. You cannot send, delete, move or change mail: the user reviews every draft \
in MailFlow and sends it there.

- Start with list_accounts: every other tool takes account ids (UUIDs) from it.
- Find mail with search_messages (operators like from:, subject:, after:) rather than paging \
through list_messages. Lists return short summaries; read one message in full with get_message.
- Message ids are MailFlow's row UUIDs. They change when a message moves folder, so look a \
message up again rather than reusing an id from an earlier conversation.
- create_draft saves into the account's Drafts folder at once. Show the user the full text first \
(call it with preview_only=true to get exactly what will be saved), and only save once they agree.
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE_DRAFT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

AccountId = Annotated[str, Field(description="Account UUID from list_accounts.")]
MessageId = Annotated[str, Field(description="Message UUID from list_messages, search_messages or get_thread.")]


def register_tools(mcp: FastMCP, mailflow: MailFlow) -> None:
    async def accounts() -> list[dict]:
        return await mailflow.get("/api/accounts")

    @mcp.tool(annotations=READ)
    async def list_accounts() -> list[dict]:
        """List the user's mail accounts: id, name, email address, sender name, whether it is \
enabled and in the unified inbox, whether its signature is on, its automatic Cc/Bcc addresses and \
its aliases (other From addresses, each with an id). MailFlow shows a token nothing of the \
account's server settings."""
        return await accounts()

    @mcp.tool(annotations=READ)
    async def list_folders(account_id: AccountId) -> list[dict]:
        """List one account's folders with their path (what list_messages and search_messages take \
as `folder`), name, special use (\\\\Sent, \\\\Drafts, \\\\Trash, …) and message and unread counts."""
        folders = await mailflow.get(f"/api/accounts/{account_id}/folders")
        return [{k: f.get(k) for k in FOLDER_FIELDS if k in f} for f in folders]

    @mcp.tool(annotations=READ)
    async def list_messages(
        account_id: Annotated[str | None, Field(description="Account UUID. Without it: the unified inbox of all accounts (folder is then ignored).")] = None,
        folder: Annotated[str, Field(description="Folder path from list_folders; only with account_id.")] = "INBOX",
        limit: Annotated[int, Field(ge=1, le=200, description="Messages per page.")] = 25,
        offset: Annotated[int, Field(ge=0, description="Messages to skip, for paging.")] = 0,
        unread_only: bool = False,
        threaded: Annotated[bool, Field(description="One row per conversation, with thread_id and message_count.")] = False,
        category: Annotated[
            Literal["primary", "newsletter", "promotion", "automated", "social"] | None, Field(description="Only messages MailFlow put in this category.")
        ] = None,
        sort: Literal["date", "from", "subject"] = "date",
        direction: Literal["asc", "desc"] = "desc",
    ) -> dict:
        """List messages in a folder, newest first by default, as short summaries (sender, \
subject, date, snippet, read/starred flags). `total` is the folder's message count. Read a \
message in full with get_message."""
        data = await mailflow.get(
            "/api/mail/messages",
            accountId=account_id,
            folder=folder,
            limit=limit,
            offset=offset,
            unreadOnly="true" if unread_only else None,
            threaded="true" if threaded else None,
            category=category,
            sort=sort,
            dir=direction,
        )
        out = {"total": data.get("total"), "messages": [summarize(m) for m in data.get("messages") or []]}
        if not out["messages"]:
            where = f"folder {folder} of that account" if account_id else "the unified inbox (folder is ignored without account_id)"
            out["guidance"] = [f"Nothing in {where} with these filters. search_messages looks across folders and accounts."]
        return out

    @mcp.tool(annotations=READ)
    async def get_message(
        message_id: MessageId,
        include_body: bool = True,
        max_body_chars: Annotated[int, Field(ge=0, description="Cut the body after this many characters; 0 for no limit.")] = DEFAULT_BODY_CHARS,
    ) -> dict:
        """Read one message: headers (from, to, cc, reply-to, date, folder, account, Message-ID) \
and its body as plain text (HTML mail is converted), plus the attachments' names and sizes. \
Fetching the body can take a few seconds when MailFlow has to load it from the mail server."""
        message = await mailflow.get(f"/api/mail/messages/{message_id}")
        out = summarize(message)
        out["url"] = message_url(message.get("message_id"), message.get("id"))
        if include_body:
            body = await mailflow.get(f"/api/mail/messages/{message_id}/body")
            text, truncated = cut(body_text(body), max_body_chars)
            out["body"] = text
            if truncated:
                out["body_truncated"] = True
                out["guidance"] = [f"The body was cut at {max_body_chars} characters; call again with a higher max_body_chars (0 = all) if the rest matters."]
            attachments = [attachment_info(a) for a in body.get("attachments") or []]
            if attachments:
                out["attachments"] = attachments
        return out

    @mcp.tool(annotations=READ)
    async def get_thread(
        message_id: Annotated[str | None, Field(description="Any message of the conversation (UUID).")] = None,
        thread_id: Annotated[str | None, Field(description="A thread_id from list_messages(threaded=true) or search_messages.")] = None,
    ) -> dict:
        """Every message of a conversation across folders (the user's own replies in Sent \
included), oldest first, as summaries; is_draft marks unsent drafts. Give message_id or \
thread_id. Read a message's text with get_message."""
        if not (message_id or thread_id):
            raise ToolError("Give message_id or thread_id.")
        if not thread_id:
            message = await mailflow.get("/api/mail/resolve-message", ref=message_id)
            # MailFlow's thread key is the thread id, or the message's own id when it has none.
            thread_id = message.get("thread_id") or message.get("id")
        data = await mailflow.get("/api/mail/thread", id=thread_id)
        rows = data.get("messages") or []
        out: dict[str, Any] = {"thread_id": thread_id, "messages": [summarize(m) for m in rows]}
        if rows:
            latest = rows[-1]
            out["url"] = message_url(latest.get("message_id"), latest.get("id"))
        return out

    @mcp.tool(annotations=READ)
    async def search_messages(
        query: Annotated[
            str,
            Field(
                description="Words and operators: from:, to:, subject:, has:attachment, is:unread|read|starred, "
                'after:YYYY-MM-DD, before:YYYY-MM-DD, in:<folder>, quoted values (from:"Jane Doe") and a leading - to negate.'
            ),
        ],
        account_id: Annotated[str | None, Field(description="Only this account; default: all accounts in the unified inbox.")] = None,
        folder: Annotated[str | None, Field(description="Only this folder path; an in: operator in the query wins.")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """Search mail across folders (Trash excluded unless asked for with in:trash), newest \
first. Free words match sender, subject and body. Returns summaries."""
        data = await mailflow.get("/api/search", q=query, accountId=account_id, folder=folder, limit=limit, offset=offset)
        out = {"messages": [summarize(m) for m in data.get("messages") or []]}
        if not out["messages"]:
            out["guidance"] = [
                "No match. Trash is left out unless the query has in:trash; try fewer words, an operator (from:, subject:) or a wider date range."
            ]
        return out

    @mcp.tool(annotations=READ)
    async def search_contacts(query: Annotated[str, Field(min_length=2, max_length=100, description="Part of a name or address.")]) -> dict:
        """Find up to 10 email addresses by name or address: people the user has written to \
first, then people who wrote to them."""
        return await mailflow.get("/api/search/contacts", q=query)

    @mcp.tool(annotations=READ)
    async def unread_counts() -> dict:
        """Unread messages in each account's INBOX as last counted on the mail server: `total` \
(unified inbox), `by_account` (account id → count, null when not counted yet), whether every \
count is fresh, and how many messages are snoozed or scheduled."""
        data = await mailflow.get("/api/mail/unread-counts")
        out = {
            "total": data.get("total"),
            "by_account": data.get("byAccount"),
            "all_counts_fresh": data.get("complete"),
            "snoozed": data.get("snoozed"),
            "scheduled": data.get("scheduled"),
        }
        if data.get("complete") is False:
            out["guidance"] = ["Some counts are stale or not counted yet (MailFlow is still syncing); say so if you report them."]
        return out

    @mcp.tool(annotations=WRITE_DRAFT)
    async def create_draft(
        body: Annotated[str, Field(min_length=1, description="The plain-text message the user writes (no signature, no quote: those are added).")],
        reply_to_message_id: Annotated[
            str | None,
            Field(description="Reply to this message (UUID): sets the account, recipient, subject and threading headers from it."),
        ] = None,
        account_id: Annotated[str | None, Field(description="Account to save the draft in. Required unless replying.")] = None,
        to: Annotated[list[str] | None, Field(description='Recipients as "Name <email>" or bare addresses. Default for a reply: the sender.')] = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: Annotated[str | None, Field(description='Default for a reply: the original subject with "Re: " unless it already has a reply prefix.')] = None,
        alias_id: Annotated[str | None, Field(description="Send-as alias id from list_accounts. Default for a reply: the alias it was addressed to.")] = None,
        quote_original: Annotated[bool, Field(description="For a reply: quote the original's text below, as MailFlow does.")] = True,
        use_signature: Annotated[bool | None, Field(description="Add the account's signature. Default: the account's own setting.")] = None,
        preview_only: Annotated[bool, Field(description="Return exactly what would be saved, without saving.")] = False,
    ) -> dict:
        """Save a new plain-text draft in the account's Drafts folder, optionally as a reply in the \
original's conversation. It is never sent: the user opens it in MailFlow, checks it and sends it \
there. The account's automatic Cc/Bcc addresses are added as MailFlow's composer adds them. Show \
the user the text before saving; preview_only=true returns the exact draft without saving it."""
        original = None
        if reply_to_message_id:
            original = await mailflow.get(f"/api/mail/messages/{reply_to_message_id}")
            if account_id and account_id != original.get("account_id"):
                raise ToolError("A reply is saved in the account the original is in; leave account_id out or use that account.")
            account_id = original.get("account_id")
        if not account_id:
            raise ToolError("Give account_id (from list_accounts), or reply_to_message_id.")
        account = next((a for a in await accounts() if a.get("id") == account_id), None)
        if account is None:
            raise ToolError(f"No account {account_id}; list_accounts shows the ids.")

        payload: dict[str, Any] = {"accountId": account_id, "body": body, "bodyIsHtml": False, "includeIdentity": True}
        if original is not None:
            to = to if to is not None else reply_recipients(original)
            subject = subject if subject is not None else reply_subject(original.get("subject"))
            alias_id = alias_id or pick_alias(account, original)
            if MESSAGE_ID_RE.fullmatch(original.get("message_id") or ""):
                payload["inReplyTo"] = original["message_id"]
            references = reply_references(original)
            if references:
                payload["references"] = references
            if quote_original:
                original_body = await mailflow.get(f"/api/mail/messages/{reply_to_message_id}/body")
                text = body_text(original_body)
                if text:
                    payload["quotedBody"] = quote_block(original, text)
        payload["to"] = merge_addresses(to)
        payload["cc"] = merge_addresses(cc, account.get("auto_cc_addresses"))
        payload["bcc"] = merge_addresses(bcc, account.get("auto_bcc_addresses"))
        payload["subject"] = subject or ""
        if alias_id:
            payload["aliasId"] = alias_id
        signature = account.get("signature_enabled", True) if use_signature is None else use_signature
        if not signature:
            payload["editedSignature"] = ""  # MailFlow's way to save without the signature

        from_address = next((a.get("email") for a in account.get("aliases") or [] if a.get("id") == alias_id), account.get("email_address"))
        draft = {
            "account": account.get("name") or account.get("email_address"),
            "from": from_address,
            "to": payload["to"],
            "cc": payload["cc"],
            "bcc": payload["bcc"],
            "subject": payload["subject"],
            "body": body,
            "quoted_original": payload.get("quotedBody", "").strip() or None,
            "signature": "account signature appended by MailFlow" if signature else "none",
            "in_reply_to": payload.get("inReplyTo"),
        }
        guidance = []
        added = [a for a in payload["cc"] if a not in (cc or [])] + [a for a in payload["bcc"] if a not in (bcc or [])]
        if added:
            guidance.append(f"The account's automatic Cc/Bcc was added: {', '.join(added)}. Tell the user.")
        if signature:
            guidance.append("If the account has a signature, MailFlow adds it below the body: check that the body's own sign-off doesn't repeat it.")
        if not payload["to"]:
            guidance.append("The draft has no recipient; the user adds one in MailFlow.")
        out: dict[str, Any] = {"saved": False, "draft": draft}
        if original is not None:
            out["original_url"] = message_url(original.get("message_id"), original.get("id"))
        if preview_only:
            out["guidance"] = [*guidance, "Nothing is saved yet. Show the user this draft and call again without preview_only once they agree."]
            return out
        saved = await mailflow.request("POST", "/api/mail/draft", json=payload)
        out.update(
            saved=True,
            folder=saved.get("folder"),
            uid=saved.get("uid"),
            message_id=saved.get("messageId"),
            url=message_url(saved.get("messageId")),
            guidance=[*guidance, "Saved in MailFlow's Drafts folder, not sent. Give the user the link; they check and send it in MailFlow."],
        )
        return out


def register_health(mcp: FastMCP) -> None:
    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_request: Request):
        return PlainTextResponse("ok")


def build_server(client: httpx.AsyncClient | None = None) -> FastMCP:
    mcp = FastMCP(name="MailFlow MCP", instructions=INSTRUCTIONS)
    register_tools(mcp, MailFlow(client or build_client()))
    register_health(mcp)
    return mcp


def build_app(mcp: FastMCP):
    """The ASGI app: FastMCP's streamable HTTP behind the token gate."""
    path = os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)
    # FastMCP's DNS-rebinding protection 421s any Host but localhost. This runs behind a reverse
    # proxy or on the LAN and the token is the gate, so it is open unless MCP_ALLOWED_HOSTS is set.
    allowed = [h.strip() for h in os.environ.get(MCP_ALLOWED_HOSTS_ENV, "").split(",") if h.strip()]
    inner = mcp.http_app(path=path, allowed_hosts=allowed) if allowed else mcp.http_app(path=path, host_origin_protection=False)
    return TokenCaptureMiddleware(inner)


def main() -> None:
    host = os.environ.get(MCP_HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(MCP_PORT_ENV, DEFAULT_PORT))
    app = build_app(build_server())
    print(f"Serving MailFlow MCP on http://{host}:{port}{os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)} for {mailflow_url()}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
