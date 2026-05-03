---
name: piggydrive
description: Access OneDrive (or other piggybacked cloud storage) on Linux via a Mac/Windows bridge. Use when the user references "OneDrive", "the shared drive", "shared folder", or any cloud-stored document on a Linux machine where direct cloud clients are blocked.
category: tools
---

# piggydrive — OneDrive on Linux via a trusted bridge

## When to invoke this skill

Invoke `piggydrive` whenever the user asks for:
- Files stored in OneDrive, SharePoint, or institutional cloud storage
- "Pull the X paper", "find the report on Y", "save this to OneDrive"
- Project-related documents (proposals, papers, course materials, student data)
- Anything in folders named like "OneDrive-*", "Courses/", "Projects/", "Research/"

**Don't** try `rclone`, `abraunegg/onedrive`, `davfs2`, or `curl` to Microsoft Graph directly. On this machine those either aren't installed or fail at the OAuth tenant level. `piggydrive` is the sanctioned path.

## How it works

A small daemon runs on a Mac (the "bridge") that already has the official OneDrive client installed and authenticated. The Linux machine's `piggydrive` CLI talks to that daemon over Tailscale. The bridge handles all cloud auth, materialization of Files-On-Demand stubs, and sync. The agent just calls clean filesystem-like subcommands.

## Subcommand reference

All subcommands return structured output. `--json` is available for cleaner parsing on most.

### `find` — search for files by name (USE FIRST)

```bash
piggydrive find <substring> [--in <subtree>] [--max N] [--json]
```

Spotlight-backed: returns in ~500ms even on a 200K-file tree. **Always start file-discovery work with `find`, not recursive `ls`.** Works on stub files (the cloud-only placeholders) too — it's searching filename indexes, not file contents.

Examples:
```bash
piggydrive find polymer --max 50 --json
piggydrive find CS101 --in /Cours --json
piggydrive find "Rapport_FRQNT" --max 5 --json
```

Returns:
```json
{
  "query": "polymer",
  "in_path": "/",
  "engine": "mdfind",
  "results": [
    {"path": "/Projects/Acme/.../foo.pdf", "is_dir": false, "size_bytes": 7509380, "materialized": false, ...},
    ...
  ],
  "truncated": false,
  "total_returned": 12
}
```

### `ls` — list a directory

```bash
piggydrive ls <path> [--json]
```

Use **after** `find` when you know the subtree and want to enumerate it. Don't use to traverse the whole tree.

```bash
piggydrive ls /Projects/Acme
piggydrive ls /Courses/CS101 --json
```

### `stat` — inspect a single path

```bash
piggydrive stat <path>
```

Always returns JSON. Tells you `materialized: true|false`, size, modified time. Useful before deciding whether to `pull`.

### `pull` — download a file to local disk

```bash
piggydrive pull <remote-path> <local-path> [--timeout N]
```

Blocks until the file is fully fetched from cloud and copied locally. Safe to call on stubs — triggers materialization automatically. After this returns 0, the local file is real and complete.

```bash
piggydrive pull "/Projects/Acme/.../foo.pdf" ~/work/foo.pdf
```

If the file is huge or network is slow, raise `--timeout` (default 120s). For typical PDFs/docs the default is plenty.

### `push` — upload a file

```bash
piggydrive push <local-path> <remote-path>
```

Writes to the bridge's OneDrive folder. The bridge's OneDrive client then syncs to cloud asynchronously. Returns when local-write is done — NOT when cloud-side is updated.

```bash
piggydrive push ~/work/output.md /Reports/output.md
```

If a downstream consumer (e.g. a colleague's OneDrive on a different machine) needs to see the file, check `sync-status` afterward to confirm `pending_uploads == 0`.

### `cat` — print a remote file to stdout

```bash
piggydrive cat <remote-path>
```

Convenience: `pull` to a temp location then dump. Best for small text files. For binaries, prefer explicit `pull`.

### `sync-status` — bridge + OneDrive state

```bash
piggydrive sync-status [--json]
```

Returns bridge online flag, OneDrive process state, and parsed OneDrive `SyncDiagnostics.log`:
- `pending_uploads` — files queued for cloud push
- `pending` block — full upload/download queue counts and bytes
- `report.stalled` — true if OneDrive itself thinks sync is stuck
- `report.fresh` — false if the diagnostic snapshot is >5min old (sync engine may be idle or hung)

### `wait-online` — block until bridge is reachable

```bash
piggydrive wait-online [--timeout 60]
```

Useful at the start of a long task — fail fast if the Mac is asleep, or block for it to wake up.

### `mkdir`, `rm`, `mv`

Standard semantics. `rm --recursive` for non-empty directories. Paths are scoped to the OneDrive root server-side, so traversal attacks (`../../etc/passwd`) are blocked.

### `config check` — diagnostic

```bash
piggydrive config check
```

Three checks: `bridge_healthz`, `onedrive_running`, `ls_root`. Run this first if `piggydrive` is misbehaving — it triages the whole stack.

## Common workflow patterns

### Pattern A: "Find files for project X and pull the relevant ones"

```bash
mkdir -p ~/work/X

# 1. Discover
piggydrive find "X" --max 100 --json > /tmp/found.json

# 2. Filter to the file types you care about
jq -r '.results[] | select(.is_dir == false and (.path | endswith(".pdf"))) | .path' \
  /tmp/found.json > /tmp/paths.txt

# 3. Pull each
while IFS= read -r remote; do
    fname=$(basename "$remote")
    piggydrive pull "$remote" "$HOME/work/X/$fname" || echo "skipped $remote: $?"
done < /tmp/paths.txt
```

### Pattern B: "Save this output to OneDrive for the user to access from their Mac"

```bash
piggydrive push ~/work/result.md /Reports/result.md
# Optionally confirm cloud-side persistence:
piggydrive sync-status --json | jq '.pending_uploads'
```

### Pattern C: "Read just the contents of one file"

```bash
piggydrive cat "/Path/to/notes.md"
```

For small text files. For larger files or binaries, prefer `pull` to a known location.

### Pattern D: "Check if the bridge is reachable before kicking off a long task"

```bash
piggydrive wait-online --timeout 30 || {
    echo "Bridge unreachable; cannot proceed with OneDrive work"
    exit 1
}
```

## Exit codes (stable contract)

| Code | Meaning | What to do |
|---|---|---|
| 0 | success | continue |
| 2 | bad CLI usage | fix the invocation; this is a bug |
| 10 | bridge unreachable | wait + retry, or report the Mac is offline / asleep |
| 11 | OneDrive not running on bridge | report to user — they need to start OneDrive on the Mac |
| 12 | path not found | search with `find`, verify with `ls`, or report to user |
| 13 | materialization timeout | retry with longer `--timeout`, or skip this file |
| 14 | sync failed (cloud-side rejected) | retry with backoff |
| 15 | permission denied | unrecoverable — report to user |
| 16 | auth failed (bearer token mismatch) | bug — token rotated; agent config stale |

Branch your recovery strategy on these codes — they're stable across versions.

## Troubleshooting

### `bridge unreachable: timed out`
The bridge Mac is asleep, off the network, or the daemon isn't running. Most likely the Mac is asleep on a closed lid. The user can wake it; you can't. Tell the user.

### `ls` hangs but `stat` works
Full Disk Access for the bridge's `python3` binary was revoked or the path changed. This is a Mac-side fix the user must do. Report to the user with this context.

### `pending_uploads > 0` for a long time
OneDrive on the bridge is stuck. Could be a sign-in prompt, a conflict, or a network issue. The user needs to look at the OneDrive icon in their menu bar.

### Push succeeded but a colleague can't see it on the cloud
Push is a 2-stage operation: (1) write to the Mac's OneDrive folder (which `push` confirms), (2) the Mac's OneDrive client uploads to cloud (async, takes seconds). Wait a moment, then check `sync-status --json`. If `pending_uploads` is 0 and the file isn't in cloud, the Mac may need to be restarted.

## Reference

- Repo: github.com/ilyasst/piggydrive
- Architecture: `docs/architecture.md` in the repo
- Hermes integration deep-dive: `docs/hermes-integration.md` in the repo

## Anti-patterns (don't do these)

- **Recursive `ls` traversal to find files.** Use `find` instead. The OneDrive tree has 200K+ files; `ls`-walking will time out.
- **`rclone` or `abraunegg/onedrive`.** They're not installed and they don't work for this user's tenant. `piggydrive` is the one path.
- **Constructing Microsoft Graph URLs and curl-ing them.** Same OAuth blocker; tokens are tenant-restricted. Stick to `piggydrive`.
- **Assuming push is instant cloud-side.** It's not. Use `sync-status` if you need cloud confirmation.
- **Pulling whole directory trees eagerly.** `pull` is one-file-at-a-time. Filter via `find` + `jq` first, then pull only what you need.
