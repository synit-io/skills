# Validation contract

This file is the single definition of the validator's gates, result states, and exit codes. Other documents link here instead of restating them.

A Docker Agent file is complete only after all mandatory gates pass.

## Mandatory gates

1. **YAML parse gate** (`yaml/*`)
   - The file is one YAML document whose root is a mapping.
   - Duplicate keys, malformed scalars, and invalid YAML fail. Duplicate-key reports name the key and line only, never the values.
   - Anchors and aliases are rejected before the document is constructed (reported as `format/yaml-anchor`).
2. **Clean-format gate** (`format/*`)
   - Official schema comment on the first non-empty line, canonical section order, block style, LF endings, no BOM, no tabs outside scalar content, no trailing whitespace outside scalar content, one final newline. The rules are listed in `authoring-guide.md`, section 11.
   - `--fix` rewrites the file to this shape. It only writes when the rewritten text re-parses to exactly the same values (the one intended change is quoting `version`); otherwise it leaves the file untouched and reports INCOMPLETE. Trailing spaces, tabs, and control characters inside scalars are content and are preserved.
3. **Official-schema gate** (`schema/*`)
   - Validate with Docker's current `agent-schema.json` using the JSON Schema draft declared by the schema's own `$schema` field. URI format checks run only when `jsonschema[format-nongpl]` is installed; otherwise a `schema/uri-format-unavailable` warning is emitted.
   - A cached copy previously downloaded from the official URL is used when its metadata names that URL and its SHA-256 matches the recorded digest. This detects cache corruption or local edits; it does not prove authenticity. Trust in the schema rests on the TLS connection to the official URL at download time and on the cache directory being under your control. When `--cache-dir` lies inside the validated file's directory tree, the validation level is reported as `official (untrusted cache)` with a warning.
   - Schema error messages describe the constraint (expected type, allowed values, missing or unknown property names) and never echo the offending value.
   - An explicit `--schema` file is caller-controlled and is reported as `explicit`, not `official`.
   - `references/core-schema.json` (`--schema-mode core`) is never a substitute for this gate.
4. **Semantic gate** (`semantic/*`)
   - Local agent, named model, toolset, command, skill, budget, MCP, and RAG references resolve. A bare primary `model` name that is not defined under `models` is a warning with a closest-match hint (Docker Agent may resolve it through its catalogue); the same value in any other model field is an error.
   - Every `instruction_file` exists as a regular file and resolves inside the configuration directory.
   - `force_handoff` does not target itself or create a local cycle.
   - Type-specific toolset requirements are present.
   - The config `version` equals the latest version advertised by the loaded schema. A different version is an error, or a warning with `--allow-legacy-version`.
5. **Security gate** (`security/*`)
   - Known credential shapes (private-key headers, OpenAI-, GitHub-, AWS-, Google-, Slack-style tokens, JWTs, passwords in URL userinfo) are errors anywhere in the file.
   - Inside credential-carrying subtrees (`env`, `headers`, `auth`, `remote`, `api_config`, `webhook_config`, `provider_opts`, `config`) a key that looks sensitive must hold `${env.NAME}`, optionally prefixed by an auth scheme such as `Bearer`. Prompt text and `commands` entries are not subject to this heuristic.
   - `args` entries such as `--token=value` or `--api-key value` with a literal value are errors.
   - Findings name the path and pattern only; the value is never printed.
6. **Runtime gate when available** (`runtime/*`)
   - `--docker-check auto` (the default) and `required` execute `docker agent run <file> --dry-run` or `docker-agent run <file> --dry-run` against the file. Treat this as running the file: use `--docker-check off` for untrusted files under review.
   - A missing CLI is a warning (`auto`) or an error (`required`).
   - Missing credentials or environment variables are warnings only when the runtime output clearly names an environment variable, API key, or credential and does not also report a configuration problem (`--runtime-env-policy error` makes them errors). Every other dry-run failure is an error.

## Result interpretation

- **PASS**: no errors and no warnings.
- **PASS WITH WARNINGS**: no errors; warnings remain. Typical causes: runtime verification unavailable or environment-blocked, cached or core schema in use, non-current version with `--allow-legacy-version`, unresolved bare model name.
- **FAIL**: any YAML, format, official-schema, semantic, security, or non-environment runtime error exists.
- **INCOMPLETE**: validation could not run to completion: a dependency is missing, the official schema could not be fetched and no valid cache exists, the file is unreadable, `--fix` could not produce a verified rewrite, or an unexpected internal error occurred. Do not present the YAML as validated.

A PASS against the main-branch schema proves current-schema validity only. Claim compatibility with a named Docker Agent release only after validating against that release's schema (`--schema`) and passing its runtime dry-run with `--docker-check required`.

## Exit codes

- `0`: PASS or PASS WITH WARNINGS.
- `1`: FAIL (the file is invalid or unsafe).
- `2`: INCOMPLETE.

## Required author behavior

After changing a YAML file, rerun the validator against the saved file. Do not rely on an in-memory draft, visual inspection, or the bundled core schema. Include the validation level and Docker dry-run status in the delivery note.
