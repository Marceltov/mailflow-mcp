# mailflow-mcp

An MCP server for [MailFlow](https://github.com/maathimself/mailflow) webmail. It lets Claude (or any MCP client) read your mailboxes, organize them and save plain-text drafts, through MailFlow's personal API tokens. **It cannot send or delete mail**: MailFlow's token API has no route that does, and this server only calls the routes it needs. You review each draft in MailFlow and send it there.

| Scope | Tools |
|---|---|
| read | `list_accounts`, `list_folders`, `list_messages`, `get_message`, `get_thread`, `search_messages`, `search_contacts`, `unread_counts`, `category_counts` |
| organize | `mark_read`, `star_messages`, `archive_messages`, `move_messages`, `snooze_message`, `unsnooze_message`, `set_category` |
| draft | `create_draft` |

The token's scopes decide what a client may do: a tool whose scope the token lacks is refused by MailFlow, and the answer names the scope. MailFlow never lets a token move mail into Trash, Junk, Drafts or Snoozed.

## Run it next to MailFlow

```yaml
services:
  mailflow-mcp:
    image: ghcr.io/marceltov/mailflow-mcp:main
    restart: unless-stopped
    environment:
      MAILFLOW_URL: http://mailflow-backend:3000          # MailFlow's backend on the compose network
      MAILFLOW_PUBLIC_URL: https://mail.example.org       # where you open MailFlow, for links in results
    ports:
      - "8082:8082"
```

Publish it over HTTPS behind your reverse proxy. The server holds no secret: each client sends its own MailFlow API token.

| Variable | Default | |
|---|---|---|
| `MAILFLOW_URL` | `http://mailflow-backend:3000` | MailFlow's backend |
| `MAILFLOW_PUBLIC_URL` | `MAILFLOW_URL` | base of the links in tool results (`/?m=<Message-ID>`) |
| `MAILFLOW_VERIFY_TLS` | `true` | `false` turns certificate checks off; the OS trust store is used otherwise |
| `MAILFLOW_TIMEOUT` | `60` | seconds per MailFlow call |
| `MCP_HOST`, `MCP_PORT`, `MCP_PATH` | `0.0.0.0`, `8082`, `/mcp` | where the server listens |
| `MCP_ALLOWED_HOSTS` | (any) | comma-separated Host allowlist |

`GET /health` answers `ok` without a token.

## Connect a client

Create a token in MailFlow (Settings → Security → API tokens) with the scopes the client should have (read, organize, draft), then:

```bash
claude mcp add mailflow --scope user --transport http https://mailflow-mcp.example.org/mcp \
  --header "Authorization: Bearer mfat_…"
```

## Development

```bash
cd app && uv run pytest -q
cd app && MAILFLOW_URL=https://mail.example.org MAILFLOW_TOKEN=mfat_… uv run python ../dev/live-smoke.py   # read-only
dev/check-allowlist.sh   # has MailFlow's token allowlist changed upstream?
```

License: AGPL-3.0-or-later.
