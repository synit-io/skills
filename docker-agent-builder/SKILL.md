---
name: docker-agent-builder
description: Create, edit, migrate, repair, review, and validate Docker Agent configuration files written in YAML. Use for agent.yaml files, single-agent or multi-agent teams, models and providers, built-in toolsets, MCP definitions, RAG, commands, Docker Agent skills, permissions, hooks, structured output, budgets, flavors, runtime settings, or Docker Agent schema and startup errors. Do not use for Docker Compose or Dockerfiles unless the task also requires a Docker Agent configuration.
license: MIT
---

# Docker Agent Builder

Build Docker Agent configurations grounded in Docker's official repository, documentation, `llms.txt`, and current JSON Schema. Validation is part of authoring, not an optional review step.

Commands use `python3` on Unix and macOS (`py -3` on Windows). Install the validator's dependencies once, preferably in a virtual environment: `python3 -m pip install -r <skill-dir>/scripts/requirements.txt`.

## Non-negotiable rules

- Never invent a key, enum value, nesting pattern, or reference type.
- Never write a secret value into YAML; use `${env.NAME}` (rules: `references/authoring-guide.md`, section 10).
- Never silently change a prompt, provider, model, tool, permission, runtime setting, or `version`. Preserve user intent and comments when editing; report every change you make.
- Use the latest configuration version advertised by the official schema for new files (`--schema-info` prints it). Do not bump `version` in an existing file unless the task is a migration.
- Do not call a file valid until the validator completes official-schema validation with exit code `0`. A core-schema result is diagnostic only.

## Workflow

1. **Inspect** the request and existing files. Decide the operation: create, edit, migrate, repair, review, or diagnose.
2. **Refresh sources** when network access is available (cheap; refreshes schema and `llms.txt`, policy in `references/source-manifest.md`). The validator refreshes the schema itself when its cache is older than 24 hours.

   ```bash
   python3 <skill-dir>/scripts/refresh_official_sources.py
   ```

3. **Ground the design** with the pointer table below. Search the cached `llms.txt` for the official page of any uncommon or fast-moving feature.
4. **Draft from the smallest valid structure**: `version` and `agents`, then only the sections the request needs. Reuse named `models`, `mcps`, `rag`, `commands`, `skills`, and `toolsets` for shared definitions. Start from `assets/templates/` when one fits, then adapt models, tools, paths, and instructions.
5. **Validate** with the command for the operation (below). Fix every error and rerun until exit code `0`. Interpret results with `references/validation-contract.md`.
6. **Deliver** the YAML plus a compact note: schema source and version used, whether Docker dry-run passed, was skipped, or was environment-blocked, the explicit `${env...}` references, and which provider or runtime credentials must be configured. Never print secret values.

## Commands per operation

`--docker-check auto` (the default) and `required` execute the Docker Agent CLI (`docker agent run <file> --dry-run`) against the file. Only do that for files you authored or trust.

**Create, edit, migrate, repair** (formats the file in place; the formatter writes only when the rewrite verifiably preserves every value):

```bash
python3 <skill-dir>/scripts/validate_agent_yaml.py <agent-file.yaml> --fix --schema-mode official --docker-check auto
```

Add `--docker-check required` before claiming compatibility with an installed runtime; add `--offline` when working from an existing cache. For a migration, record `docker agent version`, validate before and after, and use `--schema <release-schema.json>` for the target release when available.

**Review or diagnose** (no `--fix`, no runtime execution; a non-current `version` becomes a warning instead of an error so you never bump it just to get a pass):

```bash
python3 <skill-dir>/scripts/validate_agent_yaml.py <agent-file.yaml> --schema-mode official --docker-check off --allow-legacy-version --json
```

Report findings by severity with exact YAML paths, impact, smallest fix, and verification criterion. Do not edit unless asked. For startup problems, first capture `docker agent version`, `docker agent doctor <file>`, and `docker agent debug config <file>` (when supported), then separate schema, configuration, credential, dependency, and runtime failures.

**Diagnostic fallback without network or cache** (never sufficient for a validity claim):

```bash
python3 <skill-dir>/scripts/validate_agent_yaml.py <agent-file.yaml> --schema-mode core --docker-check off
```

Runtime dry-run failures that name a missing environment variable or API key are environment warnings, not configuration errors; report them as such instead of editing the file.

## Read when

| Situation | Read |
| --- | --- |
| Creating or restructuring content: document shape, agents, models, toolsets, MCP, RAG, secrets, formatting rules | `references/authoring-guide.md` |
| Interpreting gates, result states, exit codes, or deciding whether a file is complete | `references/validation-contract.md` |
| Cache refresh policy, source precedence, upstream provenance | `references/source-manifest.md` |
| A feature not covered by the guide | the cached `llms.txt`, then the current official schema |

## Resource map

- `references/authoring-guide.md`, `references/validation-contract.md`, `references/source-manifest.md`: see the table above.
- `references/upstream-mcp-definitions.yaml`: verbatim upstream example for reusable MCP definitions (an excerpt, not a complete agent file).
- `references/core-schema.json`: conservative offline diagnostic schema; never the full official schema.
- `references/THIRD_PARTY_NOTICES.md`, `references/docker-agent-LICENSE.txt`: upstream attribution and Apache License 2.0 text.
- `assets/templates/`: minimal, multi-agent, reusable-MCP, RAG, structured-output, and read-only-review starting points.
- `scripts/validate_agent_yaml.py`: formatter, official-schema validator, semantic linter, secret scanner, optional Docker dry-run gate (`--help` lists every flag; README.md documents them).
- `scripts/refresh_official_sources.py`: refreshes the official schema and `llms.txt` cache.
- `tests/test_validator.py`: offline regression tests (`python3 tests/test_validator.py`).
