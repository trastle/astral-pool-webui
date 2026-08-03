#!/usr/bin/env bash
#
# Provisions this machine's environment for the pool chlorinator BLE->MQTT
# gateway: Bluetooth stack, Python venv, dependencies, and the systemd
# service.
#
# Run this ON the Raspberry Pi (or other Linux host with Bluetooth), from
# inside a checkout of this repo, as a user with sudo access:
#   bash provision.sh
#
# Safe to re-run any time (e.g. after a reboot, or to pick up a
# requirements.txt change) - every step here is idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Ensuring Bluetooth is unblocked and running =="
sudo rfkill unblock bluetooth
sudo systemctl enable --now bluetooth

echo "== Ensuring Python venv/pip tooling is installed =="
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip

echo "== Setting up the project venv =="
if [[ ! -d "$SCRIPT_DIR/venv" ]]; then
  python3 -m venv "$SCRIPT_DIR/venv"
fi
"$SCRIPT_DIR/venv/bin/pip" install --upgrade pip -q
"$SCRIPT_DIR/venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

if [[ ! -f "$SCRIPT_DIR/gateway/.env" ]]; then
  echo
  echo "WARNING: $SCRIPT_DIR/gateway/.env is missing (needed for CHLORINATOR_ACCESS_CODE)."
  echo "Copy gateway/.env.example to gateway/.env and fill it in before the gateway will work."
fi

echo
echo "== Optional: install as a systemd service =="
echo "Edit systemd/chlorinator-gateway.service (User/WorkingDirectory/ExecStart)"
echo "to match this checkout, then:"
echo "  sudo cp $SCRIPT_DIR/systemd/chlorinator-gateway.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now chlorinator-gateway"
echo
echo "Venv ready at $SCRIPT_DIR/venv - activate with:"
echo "  source $SCRIPT_DIR/venv/bin/activate"
