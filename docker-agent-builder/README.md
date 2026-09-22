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

- Uses Docker's current official `agent-schema.json` for final schema validation, with a local cache that is reused for 24 hours and refreshed automatically afterwards.
- Retains a conservative offline diagnostic schema for work without network access.
- Formats files into the canonical shape defined in `references/authoring-guide.md` (section 11) and rewrites a file only when the rewrite verifiably preserves every value; comments stay next to their keys.
- Detects unknown or broken local references across agents, models, MCPs, RAG sources, toolsets, commands, skills, and budgets, and suggests the closest name for a mistyped model.
- Verifies that `instruction_file` paths exist and remain inside the configuration directory after symlink resolution.
- Detects self-referencing or cyclic `force_handoff` chains and type-specific toolset requirements.
- Scans for embedded credentials (known token shapes, private keys, passwords in URLs, sensitive command-line flags) without ever printing the value.
- Detects corruption of the cached official schema through recorded SHA-256 digests (see "Official sources").
- Optionally runs `docker agent run --dry-run` or `docker-agent run --dry-run` as a runtime gate.
- Includes ready-to-adapt templates for common configurations.

## Package contents

```text
docker-agent-builder/
├── README.md
├── SKILL.md
├── .gitignore
├── agents/
│   └── openai.yaml
├── scripts/
│   ├── validate_agent_yaml.py
│   ├── refresh_official_sources.py
│   ├── _sources.py
│   └── requirements.txt
├── tests/
│   └── test_validator.py
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

The validator requires Python 3.10 or newer and the packages in `scripts/requirements.txt` (`jsonschema[format-nongpl]` and `ruamel.yaml`; the `format-nongpl` extra provides the URI format checks, and the validator warns when they are unavailable). Install them from the skill root, preferably in a virtual environment:

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -r scripts/requirements.txt
```

Commands in this README use `python3` on Unix and macOS. On Windows, use `py -3`. `refresh_official_sources.py` needs only the standard library.

The Docker Agent CLI is optional for static validation but required for the runtime dry-run gate.

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

`--fix` rewrites the target file in place using the canonical formatter, atomically and only after verifying that the rewritten text parses to the same values (the one intended change is quoting `version`). The command must exit with status `0` before the YAML is described as validated.

`--docker-check auto` (the default) and `--docker-check required` execute the Docker Agent CLI against the file (`docker agent run ./agent.yaml --dry-run`). Treat that as running the file: for files you do not trust, use `--docker-check off`.

### Review a file without touching it

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --schema-mode official \
  --docker-check off \
  --allow-legacy-version \
  --json
```

`--allow-legacy-version` reports a `version` that differs from the schema's latest as a warning instead of an error, so a review never has to bump the version to obtain a pass.

### Inspect the current official schema

```bash
python3 scripts/validate_agent_yaml.py --schema-info
```

This reports the schema source, SHA-256 digest, and latest configuration version detected by the validator. It needs no third-party packages.

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

Offline official validation succeeds only when a valid official schema has already been cached. The default cache directory is `~/.cache/docker-agent-builder`. To pre-populate a cache once (for example in CI) and validate from it:

```bash
python3 scripts/refresh_official_sources.py --schema-only --cache-dir /path/to/cache
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --schema-mode official --offline --docker-check off --cache-dir /path/to/cache
```

### Run the diagnostic core schema

```bash
python3 scripts/validate_agent_yaml.py path/to/agent.yaml \
  --fix \
  --schema-mode core \
  --docker-check off
```

The bundled core schema is intentionally conservative and incomplete. A passing core-schema result is useful for diagnostics and tests, but it is not sufficient for a final validity claim.

### All flags

| Flag | Default | Effect |
| --- | --- | --- |
| `--fix` | off | Rewrite the file into canonical format before validating; written only when values are verifiably preserved. |
| `--schema-mode {official,core}` | `official` | Official schema (required for a validity claim) or bundled diagnostic core schema. |
| `--schema PATH` | – | Validate against an explicit schema file; reported as `explicit`. |
| `--cache-dir PATH` | `~/.cache/docker-agent-builder` | Where the official schema cache lives. A cache inside the validated file's directory tree is reported as `official (untrusted cache)`. |
| `--offline` | off | Never download; require a valid cached official schema. |
| `--refresh-schema` | off | Require a fresh download; fail (exit 2) instead of falling back to the cache. |
| `--max-age HOURS` | `24` | Reuse the cached schema while it is younger than this; `0` always downloads. |
| `--schema-timeout SECONDS` | `30` | Download timeout for the official schema. |
| `--schema-info` | off | Print schema source, digest, and latest config version, then exit. |
| `--allow-legacy-version` | off | Report a `version` that differs from the schema's latest as a warning instead of an error. |
| `--docker-check {auto,required,off}` | `auto` | Run the Docker Agent dry-run when the CLI is found, require it, or skip it. `auto` and `required` execute the CLI against the file. |
| `--docker-timeout SECONDS` | `90` | Dry-run timeout. |
| `--runtime-env-policy {warn,error}` | `warn` | Treat a dry-run failure caused only by missing credentials or environment variables as a warning or an error. |
| `--json` | off | Emit a machine-readable report (also used for INCOMPLETE errors). |

## Validation model

The gates, result states (PASS, PASS WITH WARNINGS, FAIL, INCOMPLETE), and exit codes (`0`, `1`, `2`) are defined in `references/validation-contract.md`. In short: YAML parse, clean format, official schema, semantic references, security, and an optional runtime dry-run must all pass for exit code `0`; `1` means the file is invalid or unsafe; `2` means validation could not be completed.

Findings never echo values from the file: duplicate keys are reported by key name and line, schema errors by expected type or allowed values, and secret findings by path and pattern name.

## Formatting, secrets, and permissions

The canonical formatting rules (schema comment, section order, block style, whitespace) are in `references/authoring-guide.md`, section 11; the secrets and least-privilege rules are in section 10 of the same file. The formatter applies the formatting rules; the security gate enforces the secrets rules.

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

## Official sources

The skill uses these upstream sources:

- [Docker Agent repository](https://github.com/docker/docker-agent)
- [Docker Agent documentation](https://docker.github.io/docker-agent/)
- [Docker Agent `llms.txt`](https://docker.github.io/docker-agent/llms.txt)
- [Stable Docker Agent documentation](https://docs.docker.com/ai/docker-agent/)
- [Official JSON Schema](https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json)
- [Repository examples](https://github.com/docker/docker-agent/tree/main/examples)

The main-branch documentation and schema can describe features newer than an installed Docker Agent release. For compatibility-sensitive work, the target CLI version and stable documentation take precedence over main-branch examples.

Refresh the local cache of the official schema and `llms.txt` with:

```bash
python3 scripts/refresh_official_sources.py
python3 scripts/refresh_official_sources.py --schema-only   # or --llms-only
python3 scripts/refresh_official_sources.py --json
python3 scripts/refresh_official_sources.py --vendor-dir ./audited-snapshot
```

The refresh policy (what is refreshed when, and by which script) is defined in `references/source-manifest.md`. Downloads are HTTPS-only with verified TLS, refuse redirects to plain HTTP, and are capped at 5 MB; when Python's own trust store is empty the scripts fall back to `curl`, which verifies certificates with its own store. When a refresh yields a different digest than the previous copy, both digests and versions are printed.

Source-cache integrity: each cached file has a `*.meta.json` record with the URL, retrieval time, byte count, and SHA-256 of the downloaded bytes. The validator refuses a cache whose file no longer matches its record. This detects local corruption or edits of the cache; it does not prove that the download was authentic, and any schema that declares the expected title is accepted. Keep the cache directory under your own control and outside the tree of files you validate (the validator flags a cache inside that tree as untrusted).

The repository contains no official-source cache: `references/llms.txt`, `references/agent-schema.json`, `references/*.meta.json`, bytecode, and cache directories are excluded by the `.gitignore` in this directory. Offline official validation therefore requires a cache created locally by an earlier online run or by `refresh_official_sources.py`.

## Running the tests

The regression suite covers formatting and value preservation, comment-preserving reordering, schema-source handling (live fetch, cache reuse, corruption, unwritable cache), download hardening, duplicate-key and schema failures, reference and instruction-file validation, secret detection for every known pattern, runtime classification with a fake Docker Agent CLI, exit codes, documentation consistency, and every bundled template.

```bash
python3 tests/test_validator.py
python3 -m unittest discover -s tests   # equivalent
```

The tests are offline: they use the bundled core schema, mock every download, and replace Docker dry-run with a fake CLI. Full acceptance of an actual Docker Agent file still requires official-schema validation.

## Continuous integration

The collection's path-filtered GitHub Actions workflow verifies skill metadata, source-manifest hashes, Python syntax (`python -m compileall -q scripts tests`), the regression suite (`python tests/test_validator.py`), and every bundled template with `--schema-mode core --docker-check off`. A separate network-dependent job pre-populates a cache with `refresh_official_sources.py --schema-only --cache-dir <dir>` and validates the templates with `--schema-mode official --offline --docker-check off --cache-dir <dir>`.

## License

This skill is released under the repository's [MIT License](../LICENSE). Bundled upstream Docker material under `references/` retains its original Apache License 2.0 notices; see `references/THIRD_PARTY_NOTICES.md` and `references/docker-agent-LICENSE.txt`.

## Attribution

Upstream Docker Agent material included in the package retains its original attribution. See:

- `references/THIRD_PARTY_NOTICES.md`
- `references/docker-agent-LICENSE.txt`
- `references/source-manifest.md`
