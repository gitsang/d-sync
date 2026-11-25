#!/usr/bin/env bash
# Install D-Sync as a per-user systemd service on Linux (run WITHOUT sudo).
#
# D-Sync is a resident process that watches devices itself, so this installer is
# deliberately much smaller than the old shell version's: no udev rule, no per-device
# template unit, no root, no mount helper. Everything runs as your own user.
#
#   ./contrib/linux/install.sh            # install and start
#   ./contrib/linux/install.sh --uninstall
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/dsync.service"

if [[ $EUID -eq 0 ]]; then
  echo "ERROR: do not run this as root; it installs a *user* service." >&2
  echo "       Run it as the user who should own the daemon." >&2
  exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now dsync.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Uninstalled $UNIT"
  exit 0
fi

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: python3 not found in PATH." >&2
  exit 1
fi

# Fail early on a missing dependency instead of installing a unit that crash-loops.
if ! "$PYTHON" -c "import yaml" >/dev/null 2>&1; then
  echo "ERROR: PyYAML is missing. Install it first:" >&2
  echo "  $PYTHON -m pip install --user -r $ROOT/requirements.txt" >&2
  exit 1
fi

if ! "$PYTHON" -c "import pyudev" >/dev/null 2>&1; then
  echo "NOTE: pyudev is not installed, so device detection falls back to polling"
  echo "      every few seconds. For instant detection install python3-pyudev."
  echo
fi

mkdir -p "$UNIT_DIR"
sed -e "s|__DSYNC_USER__|$(id -un)|g" \
    -e "s|__DSYNC_GROUP__|$(id -gn)|g" \
    -e "s|__DSYNC_HOME__|$ROOT|g" \
    -e "s|__DSYNC_PYTHON__|$PYTHON|g" \
    "$DIR/dsync.service" > "$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now dsync.service

echo "Installed and started $UNIT"
echo
echo "  Dashboard : http://127.0.0.1:8765"
echo "  Logs      : journalctl --user -u dsync -f"
echo
echo "To keep it running after you log out:"
echo "  sudo loginctl enable-linger $(id -un)"
echo
echo "Uninstall with: $0 --uninstall"
