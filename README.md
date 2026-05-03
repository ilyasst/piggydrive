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

**v1 working.** End-to-end tested against a real institutional OneDrive (ETS Montreal — 208,703 files, 19,375 folders):

- `ls` — list a remote directory (<200ms)
- `find` — Spotlight-backed name search across the entire tree (~450ms regardless of tree size). Works on stub files too.
- `stat` — distinguishes Files-On-Demand stubs (`materialized: false`) from local files
- `pull` — triggers cloud materialization, waits for completion, streams bytes (~7s for a 2.8MB stub on first access, sub-second cached)
- `push` — writes through the Mac's OneDrive client; cloud sync follows asynchronously
- `cat` — convenience: pull + write-to-stdout
- `rm`, `mv`, `mkdir` — straightforward filesystem ops
- `sync-status` — bridge state + parsed OneDrive `SyncDiagnostics.log`: pending uploads, pending downloads, sync stalls, failure counts, client version
- `wait-online`, `config check` — health/diagnostics

See `docs/architecture.md` for the design and `docs/hermes-integration.md` for using piggydrive from a Hermes-backed agent. See `sidecar/` and `client/` for the install scripts.

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

## Designed for agents

piggydrive is built to be used by an LLM-driven agent (like a Hermes-style coding assistant) as much as by humans. Concretely:

- **Default JSON output** for `stat`, `find --json`, `sync-status`, `config check` — machine-readable
- **Distinct exit codes per failure mode** (`10` bridge unreachable, `12` not found, `13` materialize timeout, `14` cloud sync failed, `15` permission, `16` auth) so the agent can branch its recovery strategy without parsing error strings
- **Idempotent operations** — `pull` works whether the file is a stub or already materialized
- **Predictable blocking** — `pull` returns only when the file is fully local; the agent never sees half-fetched data
- **Spotlight-backed `find`** so the agent can locate files in O(milliseconds) on huge trees instead of recursive `ls`

See [docs/hermes-integration.md](docs/hermes-integration.md) for example agent usage patterns.

## Why "piggydrive"?

You're piggybacking on another device's already-authorized cloud sync. Hence `piggy + drive`.

## Roadmap

Things planned for v2+ (not in v1):

- **Multi-bridge support.** v1 assumes one Linux client → one Mac bridge. v2 will support multiple bridges per client (e.g., one for OneDrive-Acme via your work Mac, one for OneDrive-Personal via a different Mac, one for Google Drive via a Windows machine), selectable via `piggydrive --bridge work ls /`.
- **Windows bridge.** The architecture is OS-neutral. A Python sidecar on Windows that talks to OneDrive-for-Business via NTFS reparse points and the Windows OneDrive client would extend piggydrive to Windows-OneDrive shops. Probably ~75% code reuse with the macOS sidecar; the stub-detection differs (NTFS `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` instead of APFS dataless files).
- **Other cloud providers.** Same piggyback pattern works for Google Drive (`~/Library/CloudStorage/GoogleDrive-<email>/`), Dropbox, Box, etc. The sidecar abstraction makes this a per-provider plugin, not a rewrite.
- **Native Hermes plugin.** v1 uses the agent's existing `terminal` tool to invoke piggydrive. A native plugin (in-process HTTP client, typed tool definitions, streaming progress) would be more efficient and cleaner. v1 works fine without it.
- **Content search.** `find` currently searches by name. Spotlight indexes file *contents* too for materialized files of supported types (PDFs, Office docs, etc.), so a `--content` mode could search inside files. Won't work for stubs.
- **Bidirectional sync mode.** Some users want continuous mirroring of a remote subtree to a local folder. piggydrive is currently pull/push only — adding a `mirror` mode (one-shot or watch) is straightforward but not in v1.
- **TLS termination.** Currently HTTP over Tailscale. For non-Tailscale deployments, adding optional TLS at the sidecar (or sitting it behind a reverse proxy) is easy.

## License

MIT — see [LICENSE](LICENSE).
