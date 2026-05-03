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

**Pre-alpha.** Designed but not yet implemented. See `docs/architecture.md` for the design.

## Why "piggydrive"?

You're piggybacking on another device's already-authorized cloud sync. Hence `piggy + drive`.

## License

MIT — see [LICENSE](LICENSE).
