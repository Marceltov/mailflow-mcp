---
status: accepted
date: 2026-11-27
decision-makers: Marcel Bruckner
---

# Hand-written tools over MailFlow's API tokens, read and draft only

## Context and Problem Statement

Claude should read the mail MailFlow brings together and prepare replies, without ever sending. MailFlow (one maintainer, fast-moving 4.x) has no OpenAPI spec, and upstream turned down MCP inside MailFlow (#296, #321, #352); for #663 the maintainer said to build against the token API. Since 4.3 (#617) personal API tokens may call only the routes in `backend/src/services/apiTokenAllowlist.js`, with the scopes read, organize and draft, and none of them sends. How do we give Claude that access?

## Considered Options

* **Hand-written tools over the token API**, in the shape of paperless-ngx-mcp (FastMCP, one module, sidecar).
* **Generate tools from an OpenAPI spec**, as paperless-ngx-mcp, invoiceninja-mcp and teamscale-mcp do: MailFlow has no spec, Express can't produce one reliably, and keeping one by hand is the maintenance we want to avoid.
* **Fork MailFlow or contribute MCP upstream**: upstream declined; a fork of a fast-moving project is a second job.
* **trust11/mailflow-mcp**: signs in with the full password and can send.

## Decision Outcome

Chosen: **hand-written tools over the token API**, read and draft only. Nine tools, each written against the route handler it calls. The server holds no secret; it forwards the client's `mfat_` token. It may call only the routes in `ALLOWED_CALLS`, a subset of MailFlow's allowlist, and its transport refuses every other call before it leaves the process, so no bug can reach a send, delete or settings route even if MailFlow added one to the token API.

### Consequences

* Good: no spec to maintain; the tool descriptions say what the routes really do; sending stays a human step in MailFlow.
* Good: the drafts behave like MailFlow's own replies (threading headers, quote, alias, signature setting, automatic Cc/Bcc), mirrored from its composer.
* Bad: upstream changes are noticed only by checks: `dev/check-allowlist.sh` (daily in CI) flags a changed allowlist, and `dev/live-smoke.py` calls every read tool against a real instance after an update.
* Neutral: organize (move, archive, snooze) is left out of v0.1; adding it means new tools and entries in `ALLOWED_CALLS`.
