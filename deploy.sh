#!/usr/bin/env bash
#
# Syncs this project to a remote host (e.g. a Raspberry Pi) and re-runs its
# provisioning script there. Run this FROM YOUR DEV MACHINE:
#   ./deploy.sh user@host [path-to-ssh-key] [remote-dir]
#
# Example:
#   ./deploy.sh pi@raspberrypi.local ~/.ssh/id_ed25519
#
# Safe to re-run any time you change code or requirements.txt.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 user@host [path-to-ssh-key] [remote-dir]" >&2
  exit 1
fi

REMOTE_HOST="$1"
SSH_KEY="${2:-}"
REMOTE_DIR="${3:-~/astral-pool-webui}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_OPTS=()
if [[ -n "$SSH_KEY" ]]; then
  SSH_OPTS=(-i "$SSH_KEY")
fi

echo "== Syncing this project to $REMOTE_HOST:$REMOTE_DIR =="
rsync -av --exclude venv --exclude __pycache__ --exclude .git --exclude .secrets.yaml \
  -e "ssh ${SSH_OPTS[*]}" \
  "$SCRIPT_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"

echo "== Running provisioning on $REMOTE_HOST =="
ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" "bash $REMOTE_DIR/provision.sh"
