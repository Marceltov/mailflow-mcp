#!/usr/bin/env bash
# Exit 1 when MailFlow's API-token allowlist changed upstream since dev/apiTokenAllowlist.sha256 was
# recorded. A change may add routes (new tools possible) or drop ones this server calls (tools break):
# read the diff, adjust server.py's ALLOWED_CALLS if needed, then record the new hash with --update.
set -euo pipefail
cd "$(dirname "$0")"
url=https://raw.githubusercontent.com/maathimself/mailflow/main/backend/src/services/apiTokenAllowlist.js
current=$(curl -fsSL "$url" | sha256sum | cut -d' ' -f1)
if [[ "${1:-}" == "--update" ]]; then echo "$current" > apiTokenAllowlist.sha256; echo "recorded $current"; exit 0; fi
recorded=$(cat apiTokenAllowlist.sha256)
if [[ "$current" != "$recorded" ]]; then
  echo "MailFlow's apiTokenAllowlist.js changed: recorded $recorded, now $current."
  echo "History: https://github.com/maathimself/mailflow/commits/main/backend/src/services/apiTokenAllowlist.js"
  exit 1
fi
echo "unchanged ($current)"
