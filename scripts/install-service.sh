#!/bin/sh
# Install or restart the connector as a login service and publish it with
# Tailscale Funnel. Safe to rerun: it replaces the service and reapplies Funnel.
# launchd starts the service at every login and restarts it if it exits;
# Tailscale keeps the Funnel setting across restarts and starts at login.
set -eu
repo="$(cd "$(dirname "$0")/.." && pwd)"
plist="$HOME/Library/LaunchAgents/com.amir.amirmcp.plist"
port="${AMIRMCP_PORT:-8787}"

[ -f "$repo/.env" ] || { echo "missing $repo/.env (copy .env.example and fill it in)" >&2; exit 1; }

(cd "$repo" && uv sync --frozen -q)
mkdir -p "$HOME/Library/Logs/amirmcp"
cp "$repo/deploy/com.amir.amirmcp.plist" "$plist"
service="gui/$(id -u)/com.amir.amirmcp"
launchctl bootout "$service" 2>/dev/null || true
# bootout returns before the old service is gone; bootstrapping too early
# fails with "Input/output error".
for _ in 1 2 3 4 5 6 7 8 9 10; do
  launchctl print "$service" >/dev/null 2>&1 || break
  sleep 1
done
launchctl bootstrap "gui/$(id -u)" "$plist"

# Funnel serves https://<this-mac>.<tailnet>.ts.net/ from the local port and
# keeps doing so across reboots.
tailscale funnel --bg "$port"

host="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
secret="$(grep '^AMIRMCP_SECRET=' "$repo/.env" | cut -d= -f2-)"
echo "connector URL: https://$host/$secret/mcp"
