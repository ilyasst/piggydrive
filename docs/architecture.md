# piggydrive architecture

## Problem

Microsoft 365 institutional tenants commonly block third-party OAuth apps that request `Files.ReadWrite.All` or other admin-tier delegated scopes. This breaks Linux OneDrive clients (rclone, abraunegg/onedrive-client, etc.) at consent. The user has zero recourse — only IT admins can grant tenant-wide consent. Same problem exists for many Microsoft Graph–based tools.

Meanwhile, the same user almost always has a Mac or Windows machine with the official OneDrive client running and syncing correctly. That client uses Microsoft-published, tenant-approved OAuth flows that bypass the third-party restrictions.

## Solution

Treat the trusted device (Mac/Windows) as a **bridge**. It runs the official OneDrive client, which handles all auth, sync, and Files-On-Demand cloud fetching. Linux talks to a small daemon (the **sidecar**) on the bridge, which exposes the OneDrive folder over a clean HTTP+JSON API.

```
┌──────────────┐                      ┌──────────────────────────┐
│ Linux box    │   HTTPS / Tailscale  │ Bridge (Mac with         │
│              │ ──────────────────▶  │   OneDrive)              │
│ piggydrive   │                      │                          │
│ CLI / agent  │ ◀──────────────────  │ piggydrive-sidecar       │
│              │   JSON responses     │ (Python HTTP daemon)     │
└──────────────┘                      │                          │
                                      │     ↓ ↑ filesystem ops   │
                                      │                          │
                                      │ ~/Library/CloudStorage/  │
                                      │   OneDrive-XYZ/          │
                                      │                          │
                                      │   ↓ Files-On-Demand      │
                                      │                          │
                                      │ Microsoft 365 cloud      │
                                      └──────────────────────────┘
```

## Key mechanics

### Stub detection (Files-On-Demand)

macOS marks cloud-only files as **dataless** in APFS. They report their full size via `stat()` but consume zero on-disk blocks until accessed.

Detection: `stat -f "%z %b" <file>` returns `<reported-size> <blocks-on-disk>`. `blocks == 0` AND `size > 0` → stub.

Windows uses a similar mechanism with NTFS reparse points; same pattern (size > 0, allocated_size = 0).

### Materialization trigger

To download a stub, **read any byte from it**. The OS file provider intercepts the read, fetches from cloud, materializes the file, then returns the requested bytes.

The sidecar uses `head -c 1 <file> > /dev/null` (or equivalent Python `open().read(1)`) and then polls `stat()` until `blocks > 0`.

### Materialization wait

Polling interval: 250ms. Default timeout: 120s (configurable per-call). Returns success when `blocks > 0` AND no further size changes in 2 successive polls (small files materialize instantly; large files trickle in).

### Sync status

`piggydrive-sidecar` exposes `/sync-status` which returns:
- `bridge_online`: bool — daemon is up
- `onedrive_running`: bool — OneDrive process detected
- `pending_uploads`: int — files modified locally but not yet pushed to cloud (best-effort detection via xattrs / app state)
- `last_error`: str | null — most recent OneDrive error from log file

### Auth

Bearer token. Generated at sidecar install time, stored in `~/.config/piggydrive-sidecar/token` on the Mac with mode 0600. Client config has the same token. Sidecar rejects requests without matching `Authorization: Bearer <token>` header.

The sidecar binds to `0.0.0.0:9090` by default. Tailscale provides the network-level isolation. Bearer token is defense-in-depth.

## API

### `GET /healthz`
No auth. Returns `200 OK` with `{"status": "ok"}` if running.

### `GET /sync-status`
Returns sidecar state, OneDrive process state, OneDrive sync queue state.

### `GET /ls?path=<remote-path>&depth=<n>`
List a directory. Returns array of entries with `{name, is_dir, size, materialized, modified_utc}`.

### `GET /stat?path=<remote-path>`
Stat a single path. Same per-entry shape as `/ls`.

### `GET /pull?path=<remote-path>`
Download a file. Server materializes if needed (with timeout), then streams the bytes. Response body is the raw file content.

### `POST /push?path=<remote-path>`
Upload a file. Request body is the raw bytes. Server writes to the OneDrive folder (the OneDrive client picks up the change and syncs to cloud). Returns when local write is complete; sync-to-cloud is async (use `/sync-status` to confirm if needed).

### `DELETE /rm?path=<remote-path>&recursive=<bool>`
Remove a file or directory.

### `POST /mkdir?path=<remote-path>`
Create a directory.

### `POST /mv?src=<>&dst=<>`
Rename / move within the OneDrive root.

## Configuration

### Sidecar (`~/.config/piggydrive-sidecar/config.toml` on the bridge)

```toml
[server]
host = "0.0.0.0"
port = 9090

[onedrive]
# Absolute path to the OneDrive root on this Mac.
# Find it:  ls ~/Library/CloudStorage/
# Looks like:  /Users/<your-mac-user>/Library/CloudStorage/OneDrive-<TenantName>
root = "/Users/USER/Library/CloudStorage/OneDrive-CompanyName"

[auth]
# Auto-generated at install. Used by clients in their Authorization header.
token_file = "~/.config/piggydrive-sidecar/token"

[materialize]
# Polling cadence and timeout when waiting for stub → fully materialized.
poll_interval_ms = 250
default_timeout_seconds = 120
```

### Client (`~/.config/piggydrive/config.toml` on Linux)

Single bridge (legacy, still works):

```toml
[bridge]
url = "http://YOUR-MAC-HOSTNAME:9090"   # Tailscale hostname or IP
token = "<paste-from-mac>"              # contents of ~/.config/piggydrive-sidecar/token

[defaults]
pull_timeout_seconds = 120              # override sidecar default
verbose = false
```

Multi-bridge with automatic fallback (recommended when more than one Mac
syncs the same drive):

```toml
[[bridges]]
name  = "primary"
url   = "http://PRIMARY-HOST:9090"
token = "<paste-from-primary-mac>"

[[bridges]]
name  = "fallback"
url   = "http://FALLBACK-HOST:9090"
token = "<paste-from-fallback-mac>"

[defaults]
pull_timeout_seconds = 120
verbose = false                          # set true to see "trying next bridge" notices
```

The client tries bridges in declared order and falls back on connectivity
errors only. HTTP errors (404 / 401 / etc.) are returned immediately without
retrying — those are definitive answers from a reachable bridge.

## Exit codes (Linux CLI)

- `0` success
- `2` invalid CLI usage
- `10` bridge unreachable (network / daemon down)
- `11` OneDrive not running on bridge
- `12` path not found
- `13` materialization timeout
- `14` cloud sync failed
- `15` permission denied
- `16` auth failed (bearer token mismatch)

Designed for agent error-handling: each code maps to a distinct recovery strategy.

## Multi-bridge / multi-client

The v1 design assumes one Linux client → one Mac bridge. Extensions:

- **Multiple Linux clients, one bridge**: works out of the box. Each client just configures the same `bridge.url` and `bridge.token`.
- **One Linux client, multiple bridges**: support config sections like `[bridge.ets]` and `[bridge.personal]`, then `piggydrive --bridge ets ls /`. Deferred to v2.
- **Bridge on Windows**: the same architecture works. The sidecar is rewritten in Python or PowerShell, OneDrive folder lives at `%USERPROFILE%\OneDrive - <Tenant>`. Stub detection is via `GetFileAttributes` returning `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`. Deferred to v2.

## Security model

- Tailscale network restricts who can reach the sidecar at all
- Bearer token restricts which Tailscale peers can actually call it
- Sidecar runs as the user who owns the OneDrive folder — same access scope as that user already has
- File operations are confined to the configured OneDrive root (path traversal guarded server-side)
- Bearer token is symmetric — there's no per-operation auth. Rotate by regenerating the token file and re-deploying client config.

## Non-goals

- **Real-time bidirectional sync.** This is a pull/push API, not a file-system sync engine. If you want a sync engine, use Syncthing on top of piggydrive.
- **Offline operation.** When the Mac is asleep or off the network, piggydrive is not available. There is no local cache on the Linux side.
- **Conflict resolution.** Last write wins via OneDrive's own conflict handling.
- **General-purpose remote-FS.** This is a delegate-to-trusted-device pattern, not SSHFS or NFS.
