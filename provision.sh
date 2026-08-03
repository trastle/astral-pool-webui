#!/usr/bin/env bash
#
# Provisions this machine's environment for the pool chlorinator BLE->MQTT
# gateway: Bluetooth stack, a dedicated service account, Python venv,
# dependencies, and the systemd service.
#
# Run this ON the Raspberry Pi (or other Linux host with Bluetooth), from
# inside a checkout of this repo, as a user with sudo access:
#   bash provision.sh
#
# Safe to re-run any time (e.g. after a reboot, to pick up a code/
# requirements.txt change, or to redeploy after a `git pull`) - every step
# here is idempotent.
#
# The gateway runs as its own "chlorinator-gateway" system account rather
# than whichever user happens to run this script, so a bug or compromised
# dependency in the BLE/MQTT/web stack can't touch anything outside that
# account's own home directory. This script creates that account (if
# missing), copies the project into its home directory (unless already
# running from there), and installs/enables/restarts the systemd service
# under that account.

set -euo pipefail

SERVICE_USER="chlorinator-gateway"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Ensuring Bluetooth is unblocked and running =="
sudo rfkill unblock bluetooth
sudo systemctl enable --now bluetooth

echo "== Ensuring Python venv/pip/rsync tooling is installed =="
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip rsync

echo "== Ensuring the $SERVICE_USER service account exists =="
if ! id "$SERVICE_USER" &>/dev/null; then
  sudo useradd --system --create-home --home-dir "/home/$SERVICE_USER" \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"

# Some distros gate BLE access behind group membership rather than opening
# it to every local user - harmless to add if BlueZ's D-Bus policy doesn't
# actually require it.
if getent group bluetooth >/dev/null; then
  sudo usermod -aG bluetooth "$SERVICE_USER"
fi

PROJECT_NAME="$(basename "$SCRIPT_DIR")"
TARGET_DIR="$SERVICE_HOME/$PROJECT_NAME"

if [[ "$SCRIPT_DIR" != "$TARGET_DIR" ]]; then
  echo "== Copying project into $TARGET_DIR (owned by $SERVICE_USER) =="
  sudo rsync -a --exclude venv --exclude __pycache__ --exclude .pytest_cache \
    "$SCRIPT_DIR/" "$TARGET_DIR/"
  sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$TARGET_DIR"
  SCRIPT_DIR="$TARGET_DIR"
fi

# Enforce this regardless of whatever permissions the source file (or an
# earlier version of this script) happened to leave it with - it holds the
# access code and any other per-install secrets, and rsync alone only
# preserves whatever the source had (typically the default umask, e.g.
# 644, if someone just `cp`'d it from the .example per the README).
if [[ -f "$SCRIPT_DIR/gateway/.secrets.yaml" ]]; then
  sudo chmod 600 "$SCRIPT_DIR/gateway/.secrets.yaml"
fi

echo "== Setting up the project venv (as $SERVICE_USER) =="
if [[ ! -d "$SCRIPT_DIR/venv" ]]; then
  sudo -u "$SERVICE_USER" python3 -m venv "$SCRIPT_DIR/venv"
fi
sudo -u "$SERVICE_USER" "$SCRIPT_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$SCRIPT_DIR/venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

if [[ ! -f "$SCRIPT_DIR/gateway/.secrets.yaml" ]]; then
  echo
  echo "WARNING: $SCRIPT_DIR/gateway/.secrets.yaml is missing (needed for the access code)."
  echo "Copy gateway/.secrets.yaml.example to gateway/.secrets.yaml (under $SCRIPT_DIR)"
  echo "and fill it in, or set a GATEWAY_CHLORINATOR__ACCESS_CODE environment variable"
  echo "instead - the service will still install below, but won't run until one of"
  echo "those is done."
fi

echo "== Installing and starting the systemd service =="
sudo tee /etc/systemd/system/chlorinator-gateway.service > /dev/null <<EOF
[Unit]
Description=Pool chlorinator web UI, Prometheus exporter, and MQTT bridge
After=bluetooth.target network-online.target
Wants=bluetooth.target network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$SCRIPT_DIR/gateway
ExecStart=$SCRIPT_DIR/venv/bin/python3 $SCRIPT_DIR/gateway/app.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable chlorinator-gateway
sudo systemctl restart chlorinator-gateway

echo
echo "Done. Service status:"
sudo systemctl status chlorinator-gateway --no-pager -l -n 5 || true
echo
echo "Deployed to $SCRIPT_DIR, running as $SERVICE_USER."
echo "To redeploy after code changes, just re-run this script from your working"
echo "checkout - it copies the update into $TARGET_DIR and restarts the service."
