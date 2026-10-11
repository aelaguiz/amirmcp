#!/bin/sh
# Install or restart the connector as a service that runs on its own, and
# publish it with Tailscale Funnel. Safe to rerun.
#   macOS: a launchd agent, started at every login and restarted if it exits.
#   Linux: a systemd user service, started at boot (lingering) and restarted
#          if it exits.
# Tailscale keeps the Funnel setting across restarts.
set -eu
repo="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$repo/.env" ] || { echo "missing $repo/.env (copy .env.example and fill it in)" >&2; exit 1; }
env_value() { grep "^$1=" "$repo/.env" | tail -1 | cut -d= -f2-; }
port="$(env_value AMIRMCP_PORT)"; port="${port:-8787}"
node="$(env_value AMIRMCP_TAILSCALE_NODE)"

(cd "$repo" && uv sync --frozen -q)

case "$(uname)" in
Darwin)
  plist="$HOME/Library/LaunchAgents/com.amir.amirmcp.plist"
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
  ;;
Linux)
  mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state/amirmcp"
  cp "$repo/deploy/amirmcp.service" "$HOME/.config/systemd/user/amirmcp.service"
  systemctl --user daemon-reload
  systemctl --user enable amirmcp.service
  systemctl --user restart amirmcp.service
  # Without lingering, user services stop at logout and do not start at boot.
  loginctl show-user "$USER" -p Linger | grep -q "=yes" || sudo loginctl enable-linger "$USER"
  ;;
*)
  echo "unsupported system: $(uname)" >&2; exit 1 ;;
esac

# ChatGPT only reaches port 443, and Funnel publishes a whole port. Where this
# machine's 443 already serves other things, AMIRMCP_TAILSCALE_NODE gives the
# connector its own Tailscale node (and so its own 443) instead.
if [ -n "$node" ]; then
  sock="$HOME/.local/state/amirmcp/tailscale/tailscaled.sock"
  mkdir -p "$(dirname "$sock")"
  cp "$repo/deploy/amirmcp-tailscale.service" "$HOME/.config/systemd/user/amirmcp-tailscale.service"
  systemctl --user daemon-reload
  systemctl --user enable --now amirmcp-tailscale.service
  sleep 3
  if ! tailscale --socket="$sock" status >/dev/null 2>&1; then
    # Prints a login URL; approve it as the tailnet owner. Then turn off key
    # expiry for the node in the Tailscale admin so it does not drop in 180 days.
    tailscale --socket="$sock" up --hostname="$node" --qr=false --timeout=300s
  fi
  tailscale --socket="$sock" funnel --bg "$port"
  host="$(tailscale --socket="$sock" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
else
  # Funnel serves https://<machine>.<tailnet>.ts.net/ from the local port.
  tailscale funnel --bg "$port" 2>/dev/null || sudo tailscale funnel --bg "$port"
  host="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
fi
echo "connector URL: https://$host/$(env_value AMIRMCP_SECRET)/mcp"
[ -z "$(env_value AMIRMCP_READ_SECRET)" ] || echo "read-only URL: https://$host/$(env_value AMIRMCP_READ_SECRET)/mcp"
