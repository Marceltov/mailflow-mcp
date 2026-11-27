# CLAUDE.md

MCP server for [MailFlow](https://github.com/maathimself/mailflow) over its personal API tokens (#617): read mail and save plain-text drafts. One module, `app/server.py`; FastMCP over streamable HTTP, `httpx2`, config from the environment only. The server holds no secret: it forwards the client's `mfat_…` token to MailFlow as `Authorization: Bearer <token>`.

## Commands

```bash
cd app && uv run pytest -q                                   # unit tests, fake MailFlow, no network
cd app && uv run ruff format --config ../ruff.toml . ../dev && uv run ruff check --config ../ruff.toml . ../dev
cd app && MAILFLOW_URL=https://mail.example.org MAILFLOW_TOKEN=… uv run python ../dev/live-smoke.py   # read-only, after a MailFlow update
cd app && MAILFLOW_URL=… uv run python server.py              # serve on :8082/mcp, /health open
docker build -t mailflow-mcp:local .                          # the image CI publishes as ghcr.io/marceltov/mailflow-mcp
dev/check-allowlist.sh [--update]                             # MailFlow's token allowlist changed upstream? (daily in CI)
```

CI (`.github/workflows/publish.yml`): tests and Ruff on every push and PR; every branch push publishes `:<branch>`, a `v*` tag `:X.Y.Z`/`:X.Y`.

## Rules that are easy to break

- **Never a send tool**, and nothing that moves, deletes or changes mail. `ALLOWED_CALLS` lists every MailFlow route the server may call; `GuardedTransport` refuses the rest before the request leaves. Adding a route means adding it there, and it must be on MailFlow's `backend/src/services/apiTokenAllowlist.js`.
- **Against a live instance, only read.** `dev/live-smoke.py` calls read tools only and prints shapes, never mail content. A draft against the live instance only after the user has seen its full text.
- **Drafts follow MailFlow's composer**: reply headers, quote format and alias choice mirror `frontend/src/utils/composeFromMessage.js`; signature off is `editedSignature: ""`; the account's automatic Cc/Bcc are added. MailFlow's draft guard (`middleware/apiTokenGuards.js`) refuses HTML, attachments and replacing a draft.

## Conventions left out

- No `src/` layout or build backend (`package = false`): the single-module shape of paperless-ngx-mcp, chosen on purpose.
- No OpenAPI spec: MailFlow has none and the tools are hand-written against its route handlers.
- MCP first, partly: `guidance` only on results where the call has something to say (a draft saved or previewed, a cut body, an empty search or list, stale counts), never on every tool; and a MailFlow link (`url`, `/?m=<Message-ID>`, built from `MAILFLOW_PUBLIC_URL`) only on `get_message`, `get_thread` and `create_draft`, not on every list row, where it would cost context in every call.
