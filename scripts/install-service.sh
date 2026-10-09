#!/bin/sh
# Install or restart the connector as a login service and publish it with
# Tailscale Funnel. Safe to rerun: it replaces the service and reapplies Funnel.
set -eu
repo="$(cd "$(dirname "$0")/.." && pwd)"
plist="$HOME/Library/LaunchAgents/com.amir.amirmcp.plist"
port="${AMIRMCP_PORT:-8787}"

[ -f "$repo/.env" ] || { echo "missing $repo/.env (copy .env.example and fill it in)" >&2; exit 1; }

cp "$repo/deploy/com.amir.amirmcp.plist" "$plist"
launchctl bootout "gui/$(id -u)/com.amir.amirmcp" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"

# Funnel serves https://<this-mac>.<tailnet>.ts.net/ from the local port and
# keeps doing so across reboots.
tailscale funnel --bg "$port"

host="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
secret="$(grep '^AMIRMCP_SECRET=' "$repo/.env" | cut -d= -f2-)"
echo "connector URL: https://$host/$secret/mcp"
