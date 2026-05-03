#!/usr/bin/env bash
# Install piggydrive-sidecar on a Mac as a launchd LaunchAgent.
#
# Usage:
#   cd sidecar/
#   ./install.sh /Users/$USER/Library/CloudStorage/OneDrive-XXX
#
# The argument is the absolute path to your OneDrive folder root.
#
# After install:
#   - Daemon listens on 0.0.0.0:9090
#   - Token at ~/.config/piggydrive-sidecar/token (mode 0600). Copy that
#     to client machines and put it in their ~/.config/piggydrive/config.toml.
#   - Logs at ~/Library/Logs/piggydrive-sidecar.{out,err}.log
#
# Reload after editing config:
#   launchctl unload ~/Library/LaunchAgents/com.piggydrive.sidecar.plist
#   launchctl load ~/Library/LaunchAgents/com.piggydrive.sidecar.plist

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <absolute-path-to-OneDrive-folder>" >&2
    echo "Example: $0 /Users/$USER/Library/CloudStorage/OneDrive-MyOrg" >&2
    exit 2
fi

ONEDRIVE_ROOT="$1"

if [[ ! -d "$ONEDRIVE_ROOT" ]]; then
    echo "error: not a directory: $ONEDRIVE_ROOT" >&2
    exit 1
fi

# Ensure python3 is available
PYTHON="${PIGGYDRIVE_PYTHON:-/usr/local/bin/python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
fi
if [[ -z "$PYTHON" ]]; then
    echo "error: python3 not found. Install with 'brew install python3' or Xcode CLT." >&2
    exit 1
fi
echo "Using python: $PYTHON"

# Check Python version (need 3.11+ for tomllib)
PY_OK=$("$PYTHON" -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [[ "$PY_OK" != "1" ]]; then
    echo "error: piggydrive-sidecar needs Python 3.11+ (for stdlib tomllib)." >&2
    "$PYTHON" --version >&2
    exit 1
fi

CONF_DIR="$HOME/.config/piggydrive-sidecar"
APP_DIR="$HOME/Library/Application Support/piggydrive-sidecar"
LOG_DIR="$HOME/Library/Logs"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

mkdir -p "$CONF_DIR" "$APP_DIR" "$LOG_DIR" "$LAUNCH_AGENTS"

# Generate token if missing
if [[ ! -f "$CONF_DIR/token" ]]; then
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32 > "$CONF_DIR/token"
    else
        head -c 32 /dev/urandom | xxd -p -c 64 > "$CONF_DIR/token"
    fi
    chmod 600 "$CONF_DIR/token"
    echo "Generated bearer token at $CONF_DIR/token"
else
    echo "Reusing existing token at $CONF_DIR/token"
fi

# Write config.toml
cat > "$CONF_DIR/config.toml" <<EOF
[server]
host = "0.0.0.0"
port = 9090

[onedrive]
root = "$ONEDRIVE_ROOT"

[auth]
token_file = "$CONF_DIR/token"

[materialize]
poll_interval_ms = 250
default_timeout_seconds = 120
EOF
echo "Wrote config: $CONF_DIR/config.toml"

# Copy sidecar.py to Application Support
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/sidecar.py" "$APP_DIR/sidecar.py"
chmod 755 "$APP_DIR/sidecar.py"
echo "Installed sidecar.py to $APP_DIR/"

# Render plist with substitutions
PLIST_OUT="$LAUNCH_AGENTS/com.piggydrive.sidecar.plist"
sed -e "s|\${HOME}|$HOME|g" \
    -e "s|/usr/local/bin/python3|$PYTHON|g" \
    "$SCRIPT_DIR/com.piggydrive.sidecar.plist" > "$PLIST_OUT"
echo "Wrote launchd plist: $PLIST_OUT"

# Reload launchd unit
if launchctl list | grep -q com.piggydrive.sidecar; then
    launchctl unload "$PLIST_OUT" 2>/dev/null || true
fi
launchctl load "$PLIST_OUT"
echo "Loaded launchd unit"

# Quick health check
sleep 2
if curl -sf http://127.0.0.1:9090/healthz >/dev/null; then
    echo
    echo "✓ piggydrive-sidecar is running."
    echo
    echo "Bearer token (deploy this to client machines):"
    cat "$CONF_DIR/token"
    echo
    echo "On the Linux side, point your client config at this Mac:"
    echo "  url = \"http://$(hostname -s):9090\""
    echo "  token = \"<the token above>\""
    echo
    cat <<EOF

────────────────────────────────────────────────────────────────────────
REQUIRED MANUAL STEP: grant Full Disk Access to python3

macOS TCC (Transparency, Consent, Control) blocks launchd-spawned
processes from reading ~/Library/CloudStorage/ by default. Without
granting access, /ls and /pull will silently hang.

Open System Settings:
  Privacy & Security → Full Disk Access → click "+"
  Press Cmd+Shift+G, paste this path, press Enter, click Add:
    $PYTHON
  Make sure the toggle next to it is ON.

Then reload the daemon:
  launchctl unload  $PLIST_OUT
  launchctl load    $PLIST_OUT

Confirm it can read the OneDrive folder:
  curl -sf -H "Authorization: Bearer \$(cat $CONF_DIR/token)" \\
    "http://127.0.0.1:9090/ls?path=/" | head -c 200

If you skip this, the daemon will run but file ops will hang. The
healthz check above passes either way — it doesn't touch the protected
folder.
────────────────────────────────────────────────────────────────────────
EOF
else
    echo
    echo "⚠ healthz check failed. Inspect logs:"
    echo "  $LOG_DIR/piggydrive-sidecar.err.log"
    exit 1
fi
