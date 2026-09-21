# Docker Agent Builder

A focused Codex/ChatGPT skill for creating, editing, migrating, repairing, reviewing, formatting, and validating Docker Agent configuration files.

The skill is grounded in Docker Agent's official repository, documentation, `llms.txt`, and JSON Schema. It treats validation as part of authoring: generated YAML is formatted, checked against the official schema, inspected for broken references and exposed secrets, and optionally verified with the Docker Agent CLI.

> This is a community-authored skill package. It is not an official Docker product.

## Scope

Use this skill for Docker Agent, including:

- Single-agent and multi-agent configurations
- Models, providers, fallback and routing settings
- Built-in toolsets and reusable toolset definitions
- MCP servers and Docker MCP Catalog references
- RAG sources and retrieval toolsets
- Commands and Docker Agent runtime skills
- Sub-agents, handoffs, and forced handoff pipelines
- Permissions, hooks, budgets, flavors, and runtime settings
- Structured output schemas
- Schema errors, startup errors, migrations, and configuration repair

The skill is not intended for ordinary Dockerfiles or Docker Compose files unless the task also requires a Docker Agent configuration.

## Key capabilities

- Uses Docker's current official `agent-schema.json` for final schema validation.
- Fetches and caches official source material while retaining a conservative offline diagnostic schema.
- Adds the official YAML language-server schema comment to new files.
- Produces clean block-style YAML with two-space indentation, canonical section ordering, LF line endings, no tabs, no anchors, no duplicate keys, no trailing whitespace, and one final newline.
- Detects unknown or broken local references across agents, models, MCPs, RAG sources, toolsets, commands, skills, and budgets.
- Verifies that `instruction_file` paths exist and remain inside the configuration directory after symlink resolution.
- Detects self-referencing or cyclic `force_handoff` chains.
- Checks type-specific toolset requirements.
- Scans for obvious embedded credentials and private keys.
- Verifies cached official-schema source metadata and SHA-256 integrity.
- Preserves comments and scalar styles where possible when repairing existing YAML.
- Optionally runs `docker agent run --dry-run` or `docker-agent run --dry-run` as a runtime gate.
- Includes ready-to-adapt templates for common configurations.

## Package contents

```text
docker-agent-builder/
├── README.md
├── SKILL.md
├── agents/
│   └── openai.yaml
├── scripts/
│   ├── validate_agent_yaml.py
│   ├── refresh_official_sources.py
│   ├── test_validator.py
│   └── requirements.txt
├── references/
│   ├── authoring-guide.md
│   ├── validation-contract.md
│   ├── source-manifest.md
│   ├── core-schema.json
│   ├── upstream-mcp-definitions.yaml
│   ├── THIRD_PARTY_NOTICES.md
│   └── docker-agent-LICENSE.txt
└── assets/
    └── templates/
        ├── minimal.yaml
        ├── multi-agent.yaml
        ├── reusable-mcp.yaml
        ├── rag.yaml
        ├── structured-output.yaml
        └── read-only-review.yaml
```

## Installation

From the collection repository, install this skill through the skills CLI:

```bash
npx skills add synit-io/skills --skill docker-agent-builder
```

For a manual local Codex installation, copy or symlink this directory to `~/.agents/skills/docker-agent-builder`. Preserve the directory structure and keep `SKILL.md` at the skill root. Start a new task, then verify discovery with:

```text
$docker-agent-builder review this agent.yaml
```

The skill's validator requires Python 3.10 or newer and these packages:

```text
jsonschema>=4.22,<5
ruamel.yaml>=0.18,<0.19
```

For direct local use, install them from the skill root:

```bash
python3 -m pip install -r scripts/requirements.txt
```

Commands in this README use `python3` on Unix and macOS. On Windows, use `py -3`.

The Docker Agent CLI is optional for static validation but required for the runtime dry-run gate.

Official-source downloads use Python's verified TLS stack. If that stack lacks a usable CA store, the scripts fall back to verified `curl`; they never disable certificate verification.

## Using the skill

Ask the host assistant to work on a Docker Agent file or describe the desired agent. Example requests:

```text
Create an agent.yaml for a documentation assistant that uses OpenAI,
searches ./docs with RAG, and has read-only filesystem access.
```

```text
Build a multi-agent Docker Agent team with a coordinator, researcher,
and reviewer. Use reusable model and MCP definitions, then validate it.
```

```text
Repair this Docker Agent without changing its intended provider,
permissions, tools, or orchestration behavior. Explain every correction.
```

```text
Review this agent.yaml for schema errors, broken references, excessive
permissions, literal secrets, and Docker Agent runtime incompatibilities.
```

```text
Migrate this older Docker Agent configuration to the latest schema supported
by the official source, preserving behavior wherever possible.
```

A completed result should include the YAML plus a compact note identifying:

- The schema source and configuration version used
- Whether official-schema validation passed
- Whether Docker Agent dry-run passed, was skipped, or was environment-blocked
- Explicit YAML environment references and provider/runtime credentials that must be configured

## Direct validator usage

Run commands from the skill root, or replace script paths with absolute paths.

### Format and fully validate

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --fix \
  --schema-mode official \
  --docker-check auto
```

`--fix` rewrites the target file in place using the canonical formatter. The command must exit with status `0` before the YAML is described as validated.

### Inspect the current official schema

```bash
python3 scripts/validate_agent_yaml.py --schema-info
```

This reports the schema source, SHA-256 digest, and latest configuration version detected by the validator.

### Require Docker Agent runtime validation

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --schema-mode official \
  --docker-check required
```

Use this when the Docker Agent CLI is expected to be installed and runtime initialization must succeed.

### Validate against a target Docker Agent release

Record `docker agent version`, obtain the matching schema from Docker's official release tag when available, then run:

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --schema path/to/version-matched-agent-schema.json \
  --docker-check required
```

Explicit schemas are reported as `explicit`, not `official`, because their provenance is caller-controlled. A current main-branch schema pass does not prove compatibility with an older runtime.

For startup diagnosis, capture read-only evidence before editing:

```bash
docker agent version
docker agent doctor path/to/agent.yaml
docker agent debug config path/to/agent.yaml
```

### Validate offline with a cached official schema

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --fix \
  --schema-mode official \
  --offline
```

Offline official validation succeeds only when a valid official schema has already been cached. The default cache directory is `~/.cache/docker-agent-builder`.

### Emit a machine-readable report

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --schema-mode official \
  --json
```

### Run the diagnostic core schema

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --fix \
  --schema-mode core \
  --docker-check off
```

The bundled core schema is intentionally conservative and incomplete. A passing core-schema result is useful for diagnostics and tests, but it is not sufficient for a final validity claim.

## Validation model

A file is accepted only after the applicable gates complete:

1. **YAML parse gate** - Reject malformed YAML, duplicate keys, invalid tabs, and non-mapping documents.
2. **Clean-format gate** - Enforce the schema comment, canonical ordering, block style, clean whitespace, and final newline.
3. **Official-schema gate** - Validate with Docker's current official JSON Schema using URI format checks. Cached schemas require matching official-source metadata and SHA-256.
4. **Semantic gate** - Resolve named and local references, validate instruction files, check handoff graphs and tool requirements, and scan for embedded secrets.
5. **Runtime gate when available** - Run Docker Agent in dry-run mode and distinguish configuration failures from missing credentials or unavailable external dependencies.

Possible result states are:

- **PASS** - Static validation passed and runtime validation passed or was not required.
- **PASS WITH WARNINGS** - Static validation passed, but runtime verification was unavailable or an external environment dependency was missing.
- **FAIL** - At least one YAML, formatting, schema, semantic, security, or non-environment runtime error exists.
- **INCOMPLETE** - Required official validation could not be completed, usually because neither the upstream schema nor a cached official schema was available.

Validator exit codes:

| Code | Meaning |
| ---: | --- |
| `0` | Validation passed; warnings may remain. |
| `1` | The file is invalid or unsafe. |
| `2` | Validation could not be completed because a required dependency, tool, or official schema was unavailable. |

## Formatting conventions

New files begin with:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json
```

When present, top-level sections are ordered as follows:

```text
version, metadata, providers, models, mcps, rag, commands, skills,
toolsets, permissions, runtime, budget, budgets, flavors, agents
```

Additional rules:

- Use two spaces for indentation and block-style YAML.
- Use `instruction: |` for multi-line agent instructions.
- Prefer the smallest valid configuration and add only requested capabilities.
- Prefer explicit `provider/model` values or reusable named models for deterministic behavior.
- Reuse top-level definitions when several agents share a model, MCP server, RAG source, command group, skill group, toolset, or budget.
- Use `sub_agents` for delegation, `handoffs` for conversation transfer, and `force_handoff` only for deterministic pipelines.
- Avoid anchors, aliases, duplicate keys, trailing whitespace, absolute paths, and unnecessary privileged tools.

## Secrets and permissions

Never place secret values directly in generated YAML.

Use environment interpolation for ordinary configuration values:

```yaml
env:
  GITHUB_PERSONAL_ACCESS_TOKEN: "${env.GITHUB_PERSONAL_ACCESS_TOKEN}"
```

For provider `token_key`, use the environment variable name rather than interpolation:

```yaml
providers:
  internal:
    provider: openai
    base_url: "${env.INTERNAL_LLM_BASE_URL}"
    token_key: INTERNAL_LLM_API_KEY
```

Treat shell execution, filesystem writes, webhooks, API tools, remote MCP servers, and broad autonomous permissions as privileged capabilities. Grant only what the requested workflow needs.

Constrain filesystem reads separately from writes:

```yaml
toolsets:
  - type: filesystem
    readonly: true
    allow_list:
      - .
```

`readonly` blocks mutation; `allow_list` confines readable paths. Omitting `allow_list` permits every path reachable by the process. Permissions are client-side approval policy, not a sandbox or security boundary.

## Templates

The files under `assets/templates/` are starting points, not final configurations:

| Template | Purpose | Runtime prerequisites |
| --- | --- | --- |
| `minimal.yaml` | Small single-agent configuration with a model and thinking tool. | `OPENAI_API_KEY` |
| `multi-agent.yaml` | Coordinator, researcher, and reviewer using a reusable model. | `OPENAI_API_KEY` |
| `reusable-mcp.yaml` | Shared MCP definitions and environment-based authentication. | `OPENAI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN` |
| `rag.yaml` | Local-document retrieval with a reusable RAG source. | `OPENAI_API_KEY`, readable `./docs` |
| `structured-output.yaml` | Agent response constrained by a JSON Schema. | `OPENAI_API_KEY` |
| `read-only-review.yaml` | Path-confined, fail-closed local review baseline. | `OPENAI_API_KEY`, readable working directory |

Copy a template, adapt its models, instructions, tools, paths, and permissions, then run full official validation.

## Refreshing official sources

Refresh the official schema and `llms.txt` cache:

```bash
python3 scripts/refresh_official_sources.py
```

Useful options:

```bash
python3 scripts/refresh_official_sources.py --schema-only
python3 scripts/refresh_official_sources.py --llms-only
python3 scripts/refresh_official_sources.py --json
python3 scripts/refresh_official_sources.py --vendor-dir ./audited-snapshot
```

The refresh script downloads and validates every requested source before replacing cache files. It records source URLs, retrieval times, SHA-256 digests, and the latest schema version it detects. The bundled source manifest documents source precedence and snapshot provenance.

The repository contains no official-source cache. Agents using the skill inspect `agent-schema.meta.json` and `llms.meta.json` in the default `~/.cache/docker-agent-builder` directory. If either entry is missing or older than seven days, they refresh both sources when network access is available. Offline official validation therefore requires a cache created locally by an earlier online run.

## Running the tests

The regression suite checks formatting, schema provenance, source-refresh failure behavior, duplicate-key detection, schema failures, reference and instruction-file validation, secret detection, runtime classification, forced-handoff cycles, structured-output strictness, and every bundled template.

```bash
python3 scripts/test_validator.py
```

The tests use the bundled core schema and disable Docker dry-run so they remain deterministic and do not require network access or model credentials. Full acceptance of an actual Docker Agent file still requires official-schema validation.

## Continuous integration

The collection's path-filtered GitHub Actions workflow verifies skill metadata, source-manifest hashes, Python syntax, validator regression tests, and every bundled template against a freshly downloaded official schema. Official schema, `llms.txt`, official-source metadata, bytecode, and cache directories remain outside version control; each installed agent maintains its own local copy.

## Official sources

The skill uses these upstream sources:

- [Docker Agent repository](https://github.com/docker/docker-agent)
- [Docker Agent documentation](https://docker.github.io/docker-agent/)
- [Docker Agent `llms.txt`](https://docker.github.io/docker-agent/llms.txt)
- [Stable Docker Agent documentation](https://docs.docker.com/ai/docker-agent/)
- [Official JSON Schema](https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json)
- [Repository examples](https://github.com/docker/docker-agent/tree/main/examples)

The main-branch documentation and schema can describe features newer than an installed Docker Agent release. For compatibility-sensitive work, the target CLI version and stable documentation take precedence over main-branch examples. The repository contains no documentation-index snapshot; refresh official sources locally before relying on rapidly changing features.

## License

This skill uses the [Synit Repository License v1.1](LICENSE), a source-available internal-use license. Bundled upstream Docker material retains its original license and notices.

## Attribution

Upstream Docker Agent material included in the package retains its original attribution. See:

- `references/THIRD_PARTY_NOTICES.md`
- `references/docker-agent-LICENSE.txt`
- `references/source-manifest.md`
