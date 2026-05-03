# piggydrive

Use OneDrive (or any cloud sync target) on Linux by piggybacking on a Mac/Windows machine that already has the official sync client running.

## What this is

Many institutions (universities, enterprises) deploy Microsoft 365 with strict tenant policies that block third-party OAuth apps — meaning rclone, abraunegg's onedrive-client, davfs2, and other Linux OneDrive tools fail at the consent step:

```
AADSTS65001: User or admin has not consented to use the application
AADSTS65002: must be configured via preauthorization
```

You can't fix this from Linux alone — the policy is set at the Microsoft Entra ID tenant level, and only IT admins can grant tenant-wide consent.

But you almost always have a Mac or Windows device that DOES sync OneDrive — that device runs Microsoft's official client, which is signed and tenant-approved.

**piggydrive lets your Linux box delegate cloud sync to that trusted device.**

The Mac runs a small `piggydrive-sidecar` daemon. Linux runs the `piggydrive` CLI. They talk over Tailscale (or any IP route). The Mac's OneDrive client handles all the Microsoft authentication and Files-On-Demand cloud fetching. Linux just sees a clean filesystem-like API.

## Architecture

```
┌────────────────┐         ┌─────────────────────────┐
│  Linux box     │  HTTPS  │  Mac (with OneDrive)    │
│                │ ─────▶  │                         │
│  piggydrive    │         │  piggydrive-sidecar     │
│  CLI / agent   │ ◀─────  │  (HTTP daemon, launchd) │
│                │  JSON   │                         │
└────────────────┘         └─────────────────────────┘
                                       │
                                       ▼
                           ┌─────────────────────────┐
                           │  ~/Library/CloudStorage │
                           │  /OneDrive-XYZ          │
                           │  (OneDrive client       │
                           │   handles auth + sync)  │
                           └─────────────────────────┘
                                       │
                                       ▼
                                  Microsoft 365
```

The sidecar exposes a small HTTP+JSON API. The CLI consumes it. All cloud auth and Files-On-Demand materialization is the OneDrive client's responsibility — piggydrive just orchestrates.

## Status

**v1 working.** End-to-end tested against a real institutional OneDrive (ETS Montreal):

- `ls /` lists the full OneDrive root in <200ms
- `stat` correctly distinguishes Files-On-Demand stubs (`materialized: false`) from local files
- `pull` triggers cloud materialization and waits for completion (~7s for a 2.8MB stub on first access, sub-second cached)
- `push` writes through the Mac's OneDrive client; cloud sync follows asynchronously
- `rm`, `mv`, `mkdir`, `cat`, `sync-status`, `wait-online`, `config check` all working

See `docs/architecture.md` for the design. See `sidecar/` and `client/` for the install scripts.

## Setup

### On the bridge (Mac with OneDrive)

```bash
git clone git@github.com:ilyasst/piggydrive.git
cd piggydrive/sidecar
./install.sh /Users/$USER/Library/CloudStorage/OneDrive-<YourTenant>
```

The install script generates a bearer token, writes a config, copies the daemon to `~/Library/Application Support/piggydrive-sidecar/`, installs a launchd plist, and starts the service.

**Required manual step on macOS**: grant Full Disk Access to your `python3` binary (the one the install script printed). Without this, `launchd`-spawned daemons cannot read `~/Library/CloudStorage/` and file operations will silently hang. System Settings → Privacy & Security → Full Disk Access → `+` → `/usr/local/bin/python3` (or wherever your python3 lives).

This is a one-time per-Mac step. The install script reminds you with the exact path at the end.

### On a client (Linux box)

```bash
git clone git@github.com:ilyasst/piggydrive.git
cd piggydrive/client
./install.sh
```

Edit `~/.config/piggydrive/config.toml`:
- `bridge.url` — `http://<bridge-tailscale-hostname>:9090`
- `bridge.token` — paste from `~/.config/piggydrive-sidecar/token` on the bridge

Smoke test:
```bash
piggydrive config check
piggydrive ls /
piggydrive pull /SomeFile.pdf ~/local/file.pdf
```

## Why "piggydrive"?

You're piggybacking on another device's already-authorized cloud sync. Hence `piggy + drive`.

## License

MIT — see [LICENSE](LICENSE).
