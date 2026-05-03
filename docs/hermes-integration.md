# Using piggydrive from a Hermes agent

`piggydrive` is designed to be agent-friendly: structured JSON output, distinct exit codes, idempotent operations, predictable blocking semantics. This doc shows how to wire it into a Hermes-backed agent so the agent can list, find, pull, and push OneDrive files autonomously.

## Setup checklist

Before the agent can use piggydrive, on the Linux machine running Hermes:

1. **`piggydrive` is on PATH.** The install script puts it at `~/.local/bin/piggydrive`. Confirm with `which piggydrive`. If Hermes runs as a service (systemd-user), make sure that PATH is inherited — check the `Environment="PATH=..."` line in your hermes-gateway.service unit.

2. **Config exists at `~/.config/piggydrive/config.toml`.** With `bridge.url` and `bridge.token` filled in. The agent will fail any piggydrive call cleanly with `EXIT_USAGE=2` if config is missing — easier to diagnose than a hung tool call.

3. **Bridge is reachable.** Test from the agent's user account:
   ```bash
   piggydrive config check
   ```
   All three checks should be `ok: true`. The agent can run this itself periodically as a self-diagnostic before kicking off any OneDrive operation.

## Calling piggydrive from a Hermes agent

The agent's existing `terminal` tool can invoke piggydrive directly. No custom Hermes plugin required for v1 — piggydrive's CLI surface is the agent interface.

### Recommended usage patterns

#### Discovery: "where are the files for project X?"

```bash
piggydrive find "X" --in / --max 50 --json
```

Returns JSON with a `results` array of stat entries. Agent reads, picks the relevant matches, then pulls them. Spotlight is fast (~500ms across hundreds of thousands of files on the ETS tree), so the agent doesn't need to fear running `find` casually.

#### Inspection: "is this file already on disk locally on the bridge?"

```bash
piggydrive stat /path/to/file --max 1 --json
```

Returns `materialized: true|false`. If `false` and the agent only needs to read the file once, `pull` is the right call. If the agent will read the same file repeatedly, it should pull and cache locally on the Linux box rather than relying on the bridge cache.

#### Reading: "I need the contents of this file"

For text/JSON/small files, `cat` to stdout:
```bash
piggydrive cat /path/to/file.md
```

For binaries or larger files, pull to a known location and read:
```bash
piggydrive pull /path/to/file.pdf /tmp/work/file.pdf
```

`pull` blocks until the file is fully materialized AND copied. The agent can trust that when this command returns 0, the file is local and complete.

#### Writing: "save this output to OneDrive"

```bash
piggydrive push /local/output.md /Reports/output.md
```

After this returns 0, the file is written into the Mac's OneDrive folder. The Mac's OneDrive client will sync it to the cloud asynchronously — usually within seconds. If you need to confirm cloud-side persistence, check `piggydrive sync-status` after.

#### Health check: "is the bridge available?"

```bash
piggydrive sync-status --json
piggydrive wait-online --timeout 30
```

`wait-online` is useful if you're starting a long-running task — block until the bridge is up rather than failing immediately if the Mac is temporarily off the network.

## Exit code contract

For agent error-handling logic:

| Code | Meaning | Recovery the agent should try |
|---|---|---|
| 0 | success | continue |
| 2 | bad CLI usage | bug — surface to user |
| 10 | bridge unreachable | wait + retry, or report Mac is offline |
| 11 | OneDrive not running on bridge | report to user — they need to start it on the Mac |
| 12 | path not found | search with `find` or report to user |
| 13 | materialization timeout | retry with longer `--timeout`, or skip this file |
| 14 | sync failed (cloud-side error) | retry with backoff |
| 15 | permission denied | unrecoverable; report to user |
| 16 | auth failed (bearer token mismatch) | bug — token rotated, agent config stale |

These codes are stable across versions. New failure modes will get new codes.

## What the agent should NOT do

- **Don't bypass `find` for known-name searches.** The temptation to do `piggydrive ls / && grep ... && piggydrive ls /SomePath && ...` looks like initiative but burns tokens and time. `find` exists; use it.
- **Don't pull large directory trees one file at a time eagerly.** Use `find` to identify exactly the files you need, then pull just those. piggydrive does NOT cache pulled files on the Linux side — re-pulling the same file goes back through the bridge.
- **Don't push without a clear remote path.** OneDrive paths are case-sensitive within the OneDrive client's view of the world. Stick to existing folder names. Use `ls` to confirm parents exist before push.
- **Don't assume sync-to-cloud is instant.** A successful `push` means the file is on the Mac. The Mac's OneDrive client takes seconds to push to cloud. Don't fail an entire workflow if a downstream consumer (e.g. another machine pulling the same file via OneDrive web) doesn't see it within 1 second.

## Putting it together — example agent task

User: "Find all the PDFs related to the polymer foam project and pull them into ~/work/foams/."

Reasonable agent execution:
```bash
mkdir -p ~/work/foams

# Discover candidates
piggydrive find polymer --max 100 --json > /tmp/found.json

# Filter to PDFs in the relevant project tree (jq, or any shell pipeline)
jq -r '.results[] | select(.is_dir == false and (.path | endswith(".pdf"))) | .path' \
  /tmp/found.json > /tmp/paths.txt

# Pull each one
while IFS= read -r remote; do
    fname=$(basename "$remote")
    piggydrive pull "$remote" "$HOME/work/foams/$fname" || echo "skipped $remote: $?"
done < /tmp/paths.txt
```

The agent gets a single search call (~500ms), filters in-process, then makes a bounded number of pulls. Each pull blocks until the file is local. Errors per-file don't kill the whole job (note the `|| echo`).

## When to register a proper Hermes plugin

For v1, the CLI-via-terminal approach works and is auditable (the agent's tool calls are visible in the trace). Reasons to upgrade to a custom Hermes plugin later:

- **Performance.** Each `piggydrive` invocation forks Python interpreters and goes over HTTP. A plugin running in-process could keep a persistent HTTP connection.
- **Schema.** A plugin can advertise its operations as typed tools (`piggydrive_find`, `piggydrive_pull`) with strict JSON-schema arguments. Better than asking the agent to construct shell commands.
- **Streaming.** Long pulls or large find results could stream progress back to the agent rather than blocking.

None of those are blocking for the current use case.
