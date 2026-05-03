#!/usr/bin/env bash
# Install piggydrive client (Linux side).
#
# Usage:
#   cd client/
#   ./install.sh
#
# After install:
#   - CLI at ~/.local/bin/piggydrive (must be in PATH)
#   - Config template at ~/.config/piggydrive/config.toml — edit with bridge URL + token

set -euo pipefail

# Need Python 3.11+ for tomllib
PYTHON="${PIGGYDRIVE_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: python3 not found." >&2
    exit 1
fi
PY_OK=$("$PYTHON" -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [[ "$PY_OK" != "1" ]]; then
    echo "error: piggydrive needs Python 3.11+ (for stdlib tomllib)." >&2
    "$PYTHON" --version >&2
    exit 1
fi

BIN_DIR="$HOME/.local/bin"
CONF_DIR="$HOME/.config/piggydrive"
mkdir -p "$BIN_DIR" "$CONF_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Install CLI as ~/.local/bin/piggydrive (with shebang to chosen python)
cat > "$BIN_DIR/piggydrive" <<EOF
#!/usr/bin/env bash
exec "$PYTHON" "$SCRIPT_DIR/piggydrive.py" "\$@"
EOF
chmod 755 "$BIN_DIR/piggydrive"
echo "Installed CLI: $BIN_DIR/piggydrive -> $SCRIPT_DIR/piggydrive.py"

# Write config template if not already present
if [[ ! -f "$CONF_DIR/config.toml" ]]; then
    cp "$SCRIPT_DIR/config.example.toml" "$CONF_DIR/config.toml"
    chmod 600 "$CONF_DIR/config.toml"
    echo "Wrote template config: $CONF_DIR/config.toml"
    echo
    echo "EDIT IT:"
    echo "  - bridge.url: Tailscale hostname or IP of the Mac running the sidecar"
    echo "  - bridge.token: paste the bearer token from ~/.config/piggydrive-sidecar/token on the Mac"
    echo
    echo "Then test with:"
    echo "  piggydrive config check"
else
    echo "Config already exists: $CONF_DIR/config.toml (left in place)"
fi

# PATH check
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    echo
    echo "⚠ $BIN_DIR is not on your PATH."
    echo "  Add to ~/.bashrc or ~/.profile:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
