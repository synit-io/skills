# synit.io - Agent Skill Collection

[![skills.sh](https://skills.sh/b/synit-io/skills)](https://skills.sh/synit-io/skills)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A collection of agent skills created and maintained by [synit.io](https://synit.io).
Each skill packages instructions, scripts, and reference material that let an AI
agent operate a specific tool or service safely and predictably.

## About synit.io

[synit.io](https://synit.io) is a German consultancy for data engineering,
analytics, and AI, working with mid-market companies in the DACH region under
the principle of "Owned Intelligence": the AI is the tool, the data is the plan.
Focus areas include data infrastructure, machine learning for forecasting and
anomaly detection, language-model automation, and system integration.

The skills in this repository grew out of that work. They are published so that
other teams can reuse them with their own agents and tenants.

## What is in this repository

Each top-level directory is one skill. A skill follows the open
[Agent Skills](https://agentskills.io) format:

```text
<skill-name>/
  SKILL.md        # frontmatter (name, description) plus operating instructions
  scripts/        # CLI tooling the agent runs
  references/     # API notes, config templates, background material
  tests/          # regression tests for the scripts
```

Skills cover different use cases, tools, and services. They are independent of
each other; install only the ones you need.

## Skills

| Skill | Description |
| --- | --- |
| [m42sd-skill](m42sd-skill/) | Operate Matrix42 Enterprise Service Management through the m42Services API: ticket, journal, user, knowledge-base, and service-catalog workflows for helpdesk agents. Includes a stateless Python CLI, guided tenant setup, and safety rules for mutations such as closing or forwarding tickets. |

## Installation

Every skills-compatible agent scans one or more directories for
`<skill-name>/SKILL.md`. Installing a skill means placing (or symlinking) the
skill folder into such a directory. You can let the `skills` CLI do that for
you (next section) or do it by hand (the sections after). The manual steps
assume you cloned this repository first:

```bash
git clone https://github.com/synit-io/skills.git
cd skills
```

Symlinking keeps the installed skill in sync with `git pull`; copying gives you
an isolated snapshot. Both work. Skill scripts write their local config (for
example `m42_config.json`) next to the script, so with a symlink that file lands
inside your clone. It is gitignored.

After installing, open the skill's `SKILL.md` and follow its setup section
(credentials, tenant discovery, safety rules) before the first real task.

### Quick install with the skills CLI

The fastest route on any supported harness is the open-source
[`skills` CLI](https://github.com/vercel-labs/skills) behind
[skills.sh](https://skills.sh/synit-io/skills). It detects the agents installed
on your machine and copies the skill into the right directory for each.

Install every skill in this repository into the current project:

```bash
npx skills add synit-io/skills
```

Install one skill, for your user account, for specific agents:

```bash
npx skills add synit-io/skills --skill m42sd-skill -g -a claude-code -a codex -a opencode
```

List what the CLI would install without installing:

```bash
npx skills add synit-io/skills --list
```

The CLI copies files rather than symlinking. Re-run the same command to pull a
newer version. Skill config such as `m42_config.json` is written inside the
installed copy, so back it up before reinstalling if you do not want to run
setup again. The CLI sends anonymous install telemetry to skills.sh; set
`DISABLE_TELEMETRY=1` to opt out.

### Where each harness looks

| Harness | Project-level | User-level | Invoke |
| --- | --- | --- | --- |
| Claude Code | `.claude/skills/` | `~/.claude/skills/` | `/skill-name` or automatic |
| Codex (CLI, IDE) | `.agents/skills/` | `~/.agents/skills/` | `$skill-name`, list with `/skills` |
| OpenCode | `.opencode/skills/` (also reads `.claude/skills/`, `.agents/skills/`) | `~/.config/opencode/skills/` (also reads `~/.claude/skills/`, `~/.agents/skills/`) | `skill` tool, automatic |
| Gemini CLI | `.gemini/skills/` or `.agents/skills/` | `~/.gemini/skills/` or `~/.agents/skills/` | `/skills` commands, automatic |
| Cursor | `.cursor/skills/` or `.agents/skills/` (also reads `.claude/skills/`) | `~/.cursor/skills/` or `~/.agents/skills/` (also reads `~/.claude/skills/`) | `/` in Agent chat, automatic |

`.agents/skills/` is the cross-tool convention. One install into
`~/.agents/skills/` is picked up by Codex, OpenCode, Gemini CLI, and Cursor.
Claude Code uses `~/.claude/skills/`, which OpenCode and Cursor also read.

### Claude Code

Personal install, available in every project:

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/m42sd-skill" ~/.claude/skills/m42sd-skill
```

Project install, checked into a repository for the whole team:

```bash
mkdir -p .claude/skills
cp -r /path/to/skills/m42sd-skill .claude/skills/
```

Claude Code picks up changes to `SKILL.md` within the running session. Invoke
with `/m42sd-skill`, or let Claude select the skill from its description.

### Codex

Codex reads `.agents/skills/` in the working directory, its parents up to the
repository root, and `~/.agents/skills/` for personal skills:

```bash
mkdir -p ~/.agents/skills
ln -s "$(pwd)/m42sd-skill" ~/.agents/skills/m42sd-skill
```

Mention a skill with `$m42sd-skill`, or run `/skills` to see what is loaded.
Restart Codex if a new skill does not show up. To disable a skill without
deleting it, add a `[[skills.config]]` entry in `~/.codex/config.toml`.

### OpenCode

OpenCode reads its own directories plus the Claude Code and `.agents` paths, so
an existing Claude Code or Codex install already works. For a dedicated install:

```bash
mkdir -p ~/.config/opencode/skills
ln -s "$(pwd)/m42sd-skill" ~/.config/opencode/skills/m42sd-skill
```

Project-local skills go in `.opencode/skills/`. Skills load through the native
`skill` tool. Control access in `opencode.json`:

```json
{
  "permission": {
    "skill": {
      "*": "allow"
    }
  }
}
```

### Other agent harnesses

Any tool that implements the Agent Skills format (Gemini CLI, Cursor, GitHub
Copilot, Goose, OpenHands, and others listed at
[agentskills.io](https://agentskills.io)) follows the same pattern:

1. Find the skill directory your harness scans. Check its documentation; most
   accept `.agents/skills/` in the project and `~/.agents/skills/` in your home
   directory.
2. Copy or symlink the skill folder there so that
   `<skills-dir>/<skill-name>/SKILL.md` exists.
3. Restart or reload the harness and confirm the skill is listed.
4. Make sure the harness can run shell commands and that `python3` is on the
   path. Skill scripts in this repository use the Python standard library only.

If your harness has no skill support, paste the contents of `SKILL.md` into the
agent's system prompt or its `AGENTS.md`, keep the `scripts/` and `references/`
folders reachable from the working directory, and give the agent shell access.
The instructions reference scripts by relative path, so run the agent from the
skill directory or adjust the paths.

### Updating

```bash
cd /path/to/skills
git pull
```

Symlinked installs update immediately. Copied installs need the copy step again.
Installs made with the `skills` CLI update by re-running the same
`npx skills add` command.

## Security

Skill scripts store credentials in local config files that are excluded from
version control. Never commit tokens, tenant profiles, or discovery output, and
never install a skill into a directory that is committed to a public repository
together with its generated config.

## Contributing

Issues and pull requests are welcome. Keep each skill self-contained, add or
update its tests, and do not include tenant-specific values or secrets.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 synit.io.
