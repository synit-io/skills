---
name: docker-agent-builder
description: Create, edit, migrate, repair, review, and validate Docker Agent configuration files written in YAML. Use for agent.yaml files, single-agent or multi-agent teams, models and providers, built-in toolsets, MCP definitions, RAG, commands, Docker Agent skills, permissions, hooks, structured output, budgets, flavors, runtime settings, or Docker Agent schema and startup errors. Do not use for Docker Compose or Dockerfiles unless the task also requires a Docker Agent configuration.
---

# Docker Agent Builder

Build Docker Agent configurations grounded in Docker's official repository, documentation, `llms.txt`, and current JSON Schema. Treat validation as part of authoring, not as an optional review step.

Commands use `python3` on Unix and macOS. On Windows, use `py -3`.

## Non-negotiable contract

- Never invent a key, enum value, nesting pattern, or reference type.
- Never hard-code API keys, access tokens, passwords, private keys, or other credentials. Use `${env.NAME}` interpolation. For `token_key`, write the environment variable name itself, such as `OPENAI_API_KEY`.
- Prefer the latest configuration version advertised by the fetched official schema. The bundled documentation snapshot used version `16`; do not assume that remains current.
- Add the official YAML language-server schema comment to every new file.
- Use clean block-style YAML with two-space indentation, LF line endings, no tabs, no anchors, no duplicate keys, no trailing whitespace, and one final newline.
- Do not claim a file is valid until the bundled validator completes full official-schema validation. A bundled-core-only result is diagnostic, not a final validation result.
- Preserve user intent when repairing an existing file. Do not silently change providers, models, permissions, tools, prompts, or runtime behavior.

## Required workflow

1. **Inspect the task and existing files.** Determine whether to create, edit, migrate, or diagnose. Preserve comments and unrelated settings when editing.
2. **Refresh outdated official-source caches.** The default cache is `~/.cache/docker-agent-builder`. If `agent-schema.meta.json` or `llms.meta.json` is missing, or its `fetched_at` value is more than seven days old, and network access is available, update both sources:

   ```bash
   python3 <skill-dir>/scripts/refresh_official_sources.py
   ```

   Use the refreshed cache's `llms.txt` for documentation discovery. If refresh fails, use an existing local cache. If neither is available, use bundled authoring guidance only and disclose that current official documentation was unavailable. Never expect release artifacts to contain official-source caches.
3. **Ground the design.** Read `references/authoring-guide.md`. For uncommon or rapidly changing features, search the refreshed cached `llms.txt` first for the relevant official documentation page. Read `references/validation-contract.md` before deciding that a file is complete.
4. **Resolve the schema version.** Fetch the current official schema or inspect its cached copy:

   ```bash
   python3 <skill-dir>/scripts/validate_agent_yaml.py --schema-info
   ```

   Use the returned latest configuration version for new files. For an existing or version-targeted file, run `docker agent version`; use stable documentation and a schema from the matching official release when available. A main-branch schema pass does not prove compatibility with an older target runtime.
5. **Draft from the smallest valid structure.** Start with `version` and `agents`, then add only sections required by the request. Reuse named `models`, `mcps`, `rag`, `commands`, `skills`, and `toolsets` when the same definition is shared.
6. **Format and validate.** Run:

   ```bash
   python3 <skill-dir>/scripts/validate_agent_yaml.py <agent-file.yaml> \
     --fix \
     --schema-mode official \
     --docker-check auto
   ```

   The command must exit with status `0`. Fix every error and rerun it. Warnings about a missing Docker Agent CLI or missing runtime credentials may remain only when official-schema and semantic validation passed.
7. **Run the Docker Agent runtime gate when available.** The validator invokes `docker agent run --dry-run` or `docker-agent run --dry-run`. If the CLI is installed but auto-detection is inconclusive, rerun with `--docker-check required`.
8. **Review the emitted file.** Confirm that references resolve, instructions match the user's goal, filesystem access is path-confined unless broad access is intentional, permissions are least-privilege, and paths are portable where practical.
9. **Deliver the YAML plus a compact validation note.** State which schema source was used, whether Docker dry-run passed or was skipped, explicit YAML environment references, and provider/runtime credentials that must be configured. Never expose secret values.

## Operation branches

- **Migration:** record source config version and target CLI version; consult version-matched stable documentation; preserve behavior; validate before and after; require `--docker-check required` before claiming target-runtime compatibility.
- **Diagnosis:** reproduce without editing first. Record `docker agent version`, run `docker agent doctor <agent-file>` and `docker agent debug config <agent-file>` when supported, then separate schema, configuration, credential, dependency, and runtime failures.
- **Review:** report findings by severity with exact YAML paths, behavior/security impact, smallest fix, and verification criterion. Do not edit unless requested.
- **Repair:** change only demonstrated defects. Rerun every failed gate and stop when the requested behavior and validation criteria pass.

## Creation rules

- Put the schema comment first:

  ```yaml
  # yaml-language-server: $schema=https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json
  ```

- Use this top-level order when sections are present:

  ```text
  version, metadata, providers, models, mcps, rag, commands, skills,
  toolsets, permissions, runtime, budget, budgets, flavors, agents
  ```

- Prefer an explicit inline model reference such as `openai/gpt-5-mini` for a single simple agent. Docker Agent can resolve bare primary model names at runtime, but explicit `provider/model` values or named entries under `models` are more deterministic. Prefer a named entry when settings are reused, routed, or customized.
- Give every agent a concrete `description` and focused `instruction`. Use `instruction: |` for multi-line instructions.
- Use `sub_agents` for delegation, `handoffs` for conversation transfer, and `force_handoff` only for deterministic pipelines. Local references must name agents in the same file; external references must use an explicit URL or OCI-style reference.
- Use reusable top-level definitions when multiple agents share the same MCP server, RAG source, command group, skill group, or toolset.
- Select only the tools required by the task. Treat `shell`, filesystem write access, webhooks, API tools, and remote MCP servers as privileged capabilities. Constrain `filesystem` and `file` tools with `allow_list` unless broad host access is explicitly required.
- Use templates in `assets/templates/` as starting points, not as unquestioned final answers. Replace models, tools, paths, and instructions to match the task, then validate.

## Editing and repair rules

- Parse the entire file before modifying it; never patch YAML by string substitution when structure can change.
- Preserve comments, scalar styles, and ordering where they do not conflict with the canonical formatter.
- Correct duplicate keys, unknown fields, invalid tool-specific fields, broken local references, stale named references, and unsafe literal secrets.
- Do not solve a schema error by deleting an unfamiliar feature until checking the current official schema and documentation. The main-branch documentation may describe features newer than a user's installed runtime.
- When schema validation passes but runtime dry-run fails, separate configuration errors from environment errors such as absent credentials, unavailable local models, missing MCP binaries, or inaccessible files.

## Validation commands

Full required validation:

```bash
python3 <skill-dir>/scripts/validate_agent_yaml.py agent.yaml --fix --schema-mode official --docker-check auto
```

Offline validation with an already cached official schema:

```bash
python3 <skill-dir>/scripts/validate_agent_yaml.py agent.yaml --fix --schema-mode official --offline
```

Diagnostic fallback only, never sufficient for a final validity claim:

```bash
python3 <skill-dir>/scripts/validate_agent_yaml.py agent.yaml --fix --schema-mode core --docker-check off
```

Machine-readable report:

```bash
python3 <skill-dir>/scripts/validate_agent_yaml.py agent.yaml --schema-mode official --json
```

Refresh official sources into the local cache:

```bash
python3 <skill-dir>/scripts/refresh_official_sources.py
```

## Resource map

- `references/authoring-guide.md`: concise Docker Agent patterns, cross-reference rules, tool requirements, security rules, and formatting conventions.
- `references/validation-contract.md`: exact acceptance gates and interpretation of validator results.
- `references/source-manifest.md`: upstream sources, snapshot hashes, source precedence, and update policy.
- `references/upstream-mcp-definitions.yaml`: official repository example for reusable MCP definitions.
- `references/THIRD_PARTY_NOTICES.md` and `references/docker-agent-LICENSE.txt`: attribution and upstream Apache License 2.0 text.
- `references/core-schema.json`: conservative offline diagnostic schema; never treat it as the full official schema.
- `assets/templates/`: minimal, multi-agent, MCP, RAG, structured-output, and constrained read-only examples.
- `scripts/validate_agent_yaml.py`: formatter, official JSON Schema validator, semantic linter, secret scanner, and optional Docker Agent dry-run gate.
- `scripts/refresh_official_sources.py`: refresh the official schema and `llms.txt` cache with hashes and metadata.
- `scripts/test_validator.py`: regression tests for the bundled validator.
