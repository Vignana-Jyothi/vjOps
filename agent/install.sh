#!/usr/bin/env bash
# ViljaOps agent installer — run on each deployment server as root.
#
#   curl -fsSL https://<control-plane>/agent/install.sh | \
#     VILJAOPS_URL=https://ops.vnrvjiet.in VILJAOPS_AGENT_TOKEN=vops_xxx bash
#
# Installs to /opt/viljaops-agent and registers a systemd unit.

set -euo pipefail

VILJAOPS_URL="${VILJAOPS_URL:-}"
VILJAOPS_AGENT_TOKEN="${VILJAOPS_AGENT_TOKEN:-}"
INSTALL_DIR="/opt/viljaops-agent"
SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
[[ -n "$VILJAOPS_URL" ]] || die "VILJAOPS_URL is required"
[[ -n "$VILJAOPS_AGENT_TOKEN" ]] || die "VILJAOPS_AGENT_TOKEN is required (enroll the server in the dashboard first)"

command -v docker >/dev/null || die "docker is not installed"
command -v python3 >/dev/null || die "python3 is not installed"

echo "==> Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" /etc/viljaops/env /var/lib/viljaops-agent
cp -r "$SOURCE_DIR/viljaops_agent" "$INSTALL_DIR/"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet httpx

echo "==> Writing configuration"
cat > /etc/viljaops/agent.env <<EOF
VILJAOPS_URL=$VILJAOPS_URL
VILJAOPS_AGENT_TOKEN=$VILJAOPS_AGENT_TOKEN
VILJAOPS_SERVER_NAME=$(hostname)
VILJAOPS_HEARTBEAT_S=30
VILJAOPS_POLL_S=15
VILJAOPS_LOG_SHIP_S=60
NGINX_SITES_AVAILABLE=/etc/nginx/sites-available
NGINX_SITES_ENABLED=/etc/nginx/sites-enabled
NGINX_LOG_DIR=/var/log/nginx
VILJAOPS_ENV_DIR=/etc/viljaops/env
VILJAOPS_STATE_DIR=/var/lib/viljaops-agent
# Set to true for a no-op first run that reports what it *would* do.
VILJAOPS_DRY_RUN=false
EOF
chmod 600 /etc/viljaops/agent.env

echo "==> Installing systemd unit"
cat > /etc/systemd/system/viljaops-agent.service <<EOF
[Unit]
Description=ViljaOps deployment agent
Documentation=https://github.com/vnrvjiet/viljaops
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/viljaops/agent.env
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m viljaops_agent.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# The agent needs docker and nginx; it does not need anything else.
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/etc/nginx /var/log/nginx /etc/viljaops /var/lib/viljaops-agent /var/run/docker.sock

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now viljaops-agent

sleep 3
if systemctl is-active --quiet viljaops-agent; then
  echo "==> Agent is running."
  echo "    logs:   journalctl -u viljaops-agent -f"
  echo "    config: /etc/viljaops/agent.env"
else
  echo "==> Agent failed to start. Check: journalctl -u viljaops-agent -n 50" >&2
  exit 1
fi
