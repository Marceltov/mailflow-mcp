---
status: accepted
date: 2026-11-28
decision-makers: Marcel Bruckner
---

# Organize tools, and the token's scopes decide what a client may do

## Context and Problem Statement

v0.1 read mail and saved drafts only (ADR 0001). Organizing (marking read, starring, archiving, moving, snoozing, setting the category) still meant switching to MailFlow, though MailFlow's token API allows it under the organize scope, with guards of its own: nothing into Trash, Junk, Drafts or Snoozed, no moving of drafts. Should a client get those tools, and how is one kept read-only?

## Considered Options

* **All tools for every client; the token's scopes decide.** A read-only client gets a read-only token, and a refused call names the missing scope.
* **Separate deployments or a server setting per scope set**: more to run for something the token already says.
* **Keep v0.1 read and draft only**: the user asked for organizing.

## Decision Outcome

Chosen: **all tools, the scopes decide.** Eight more routes in `ALLOWED_CALLS` (bulk read, star, archive and move, snooze and its change and end, category) and `category-counts` for reading; seven tools. MailFlow's guards stay the only rule about destinations; the transport still refuses every route not listed, so send, delete, spam and settings stay unreachable. The server's instructions ask the model to say which and how many messages before archiving or moving any the user hasn't named one by one.

### Consequences

* Good: one server for read-only and organizing clients; MailFlow checks the scope on every call.
* Good: nothing moves into Trash or Junk, and nothing is deleted: everything an organize tool does can be undone in MailFlow.
* Bad: a token with organize lets a client move mail around; a client that should only read needs a token without it.
* Neutral: archived, moved and snoozed messages get new row ids; the tools say so.
