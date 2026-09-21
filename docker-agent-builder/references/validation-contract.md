# Validation contract

A Docker Agent file is complete only after all mandatory gates pass.

## Mandatory gates

1. **YAML parse gate**
   - The document is a mapping.
   - Duplicate keys, malformed scalars, tabs, and invalid YAML fail.
2. **Clean-format gate**
   - Official schema comment is present.
   - Two-space block style, LF endings, canonical section order, no anchors, no trailing whitespace, and final newline.
3. **Official-schema gate**
   - Validate with Docker's current `agent-schema.json` using JSON Schema Draft 7 and URI format checking.
   - The validator may use a cached copy previously fetched from the official URL only when its source metadata and SHA-256 digest match.
   - An explicit `--schema` file is caller-controlled and is reported as `explicit`, not `official`.
   - `references/core-schema.json` is never a substitute for this gate.
4. **Semantic gate**
   - Local agent, named model, toolset, command, skill, budget, MCP, and RAG references resolve. Bare primary model names allowed by Docker Agent are left for official-schema/runtime resolution.
   - Every `instruction_file` exists as a regular file and resolves inside the configuration directory.
   - `force_handoff` does not target itself or create a local cycle.
   - Type-specific toolset requirements are present.
   - No obvious literal credentials or private keys are embedded.
5. **Runtime gate when available**
   - Run `docker agent run --dry-run` or `docker-agent run --dry-run`.
   - A missing CLI is a warning when all mandatory static gates passed.
   - Missing credentials or unavailable external dependencies are environment warnings only when the failure is clearly limited to those causes. Other dry-run failures are errors.

## Result interpretation

- **PASS**: no errors; official schema and semantic gates passed. Docker dry-run either passed or was explicitly unavailable/environment-blocked.
- **PASS WITH WARNINGS**: structural validity passed, but runtime verification was unavailable or an environment dependency was missing.
- **FAIL**: any YAML, format, official-schema, semantic, secret, or non-environment runtime error exists.
- **INCOMPLETE**: the official schema could not be fetched or loaded and no valid official cache was available. Do not present the YAML as validated.

A PASS against the main-branch schema proves current-schema validity only. Claim compatibility with a named Docker Agent release only after validating against that release's schema when available and passing its runtime dry-run with `--docker-check required`.

## Exit codes

- `0`: validation passed; warnings may exist.
- `1`: the file is invalid or unsafe.
- `2`: validation could not be completed because required tooling, dependencies, or the official schema were unavailable.

## Required author behavior

After changing a YAML file, rerun the validator against the saved file. Do not rely on an in-memory draft, visual inspection, or the bundled core schema. Include the validation level and Docker dry-run status in the delivery note.
