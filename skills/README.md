# Hermes skill for piggydrive

`skills/piggydrive/SKILL.md` is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill file that teaches an agent how to use `piggydrive` correctly: when to invoke it, which subcommand maps to which need, the exit-code recovery contract, and common workflow patterns (find → pull, push → sync-status verification, etc.).

## Why a skill?

The `piggydrive` CLI is already on a Hermes agent's PATH after install. The agent can call it via its existing terminal tool with no special wiring. But without context, the agent will still default to its training-prior on OneDrive — typically reaching for `rclone` or `curl` to Graph API, both of which fail on tenants where piggydrive is meant to help.

A skill gives the agent the right priors:
- "Use `piggydrive` for any OneDrive task on this machine"
- "Start with `find`, not recursive `ls`"
- "These exit codes mean these things — branch your recovery accordingly"
- "Don't try `rclone`/`abraunegg`/`davfs2` — they're blocked at tenant level"

## Install on a Hermes-using Linux machine

Hermes loads skills from `~/.hermes/skills/<skill-name>/SKILL.md` (or a configured skill root). Symlink works fine and keeps the repo authoritative:

```bash
git clone git@github.com:ilyasst/piggydrive.git ~/piggydrive  # if not already cloned
mkdir -p ~/.hermes/skills/piggydrive
ln -sf ~/piggydrive/skills/piggydrive/SKILL.md ~/.hermes/skills/piggydrive/SKILL.md
```

Or copy if you prefer no symlink:
```bash
mkdir -p ~/.hermes/skills/piggydrive
cp ~/piggydrive/skills/piggydrive/SKILL.md ~/.hermes/skills/piggydrive/SKILL.md
```

Restart your Hermes gateway (or start a new conversation) for the skill to be picked up.

## Format

Standard Hermes skill: YAML frontmatter (`name`, `description`, `category`) followed by markdown content. The `description` is what the agent's skill-discovery surfaces, so it's written to be clearly relevant when OneDrive comes up in conversation.

## Companion guidance: SOUL.md

In addition to the skill file, consider adding a brief paragraph to your agent's `SOUL.md` (or equivalent persona/system-prompt file) telling it that piggydrive is the sanctioned OneDrive tool on this machine. The skill provides the depth; SOUL.md provides the *trigger* (the agent reaches for the skill before considering alternatives).

A minimal SOUL.md addition:

```markdown
## OneDrive access on this machine

OneDrive on Linux is exposed via `piggydrive` (talks to a Mac bridge over Tailscale). Don't try rclone/abraunegg-onedrive/davfs2 — they're blocked at the tenant OAuth layer. See the `piggydrive` skill for details. Use `piggydrive find` first when looking for files; the tree is huge.
```

## Other agent integrations

The skill format is Hermes-specific but the *content* is agent-agnostic. If you're using another agent runtime (Cline, Continue, custom CrewAI/LangGraph setup), adapt the skill content into whatever your runtime calls a "tool description", "system prompt addition", or "agent rules file". The exit-code contract and workflow patterns are the part that generalizes.
