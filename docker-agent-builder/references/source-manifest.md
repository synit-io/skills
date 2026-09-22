# Source manifest

Snapshot date: 2026-09-20

## Authoritative upstream sources

- Repository: https://github.com/docker/docker-agent
- Main documentation: https://docker.github.io/docker-agent/
- Documentation index: https://docker.github.io/docker-agent/llms.txt
- Stable Docker documentation: https://docs.docker.com/ai/docker-agent/
- Official JSON Schema: https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json
- Repository examples: https://github.com/docker/docker-agent/tree/main/examples
- CLI reference: https://docker.github.io/docker-agent/features/cli/

## Source precedence

1. The schema and runtime installed for the user's target Docker Agent version.
2. Stable Docker documentation for released behavior.
3. Main-branch schema, documentation, source code, and examples for current development behavior.
4. Bundled reference notes and templates in this skill.

The official `llms.txt` index notes that the main documentation tracks the `main` branch and can describe unreleased features. When a user's installed runtime rejects a main-branch feature, inspect the runtime version and stable documentation instead of forcing the main-branch shape.

## Refresh policy

This section is the single definition of the cache refresh policy.

- The local cache lives in `~/.cache/docker-agent-builder` (`--cache-dir` on both scripts). It holds `agent-schema.json`, `llms.txt`, and one `*.meta.json` record per file with `source`, `fetched_at`, `sha256`, `bytes`, and (for the schema) `latest_version`.
- The validator refreshes `agent-schema.json` on its own: it reuses the cache while the record is younger than `--max-age` hours (default 24), downloads otherwise, and falls back to the cache with a warning when the download fails. `--refresh-schema` forces a download; `--offline` forbids one.
- `llms.txt` is refreshed only by `scripts/refresh_official_sources.py`. Run it once at the start of a task when network access is available; it is cheap and refreshes both sources. If it fails, use the existing cache. If no cache exists, work from the bundled authoring guide and disclose that current official documentation was unavailable.
- A refresh that yields a different digest than the previously cached copy prints a notice with both digests and versions (the validator reports it as an `info` finding).
- Release artifacts and the repository never contain these caches; each installation maintains its own. Do not replace official-source content with third-party summaries.

## Cache integrity

The digest in a `*.meta.json` record is the SHA-256 of the bytes that were downloaded, written by the same process next to the file. Comparing the file against it detects local corruption or accidental edits. It does not establish that the download was authentic: that trust comes from the verified TLS connection to the official URL at download time, and from the cache directory being under your control rather than shipped alongside the file being validated.

## Bundled snapshot inventory

- `references/upstream-mcp-definitions.yaml` — official reusable-MCP example, SHA-256 `225cedda1da0673ae8a360d05af3d2e9d1cdef8f10c7692a50608f3df853d4bc`. It is a verbatim upstream reference excerpt, not a complete agent file: it declares no `version` and uses a relative schema comment, so it is not expected to pass the bundled validator. Do not edit it.
- `references/docker-agent-LICENSE.txt` — upstream Apache License 2.0 text, SHA-256 `58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd`.
- `references/core-schema.json` — skill-authored conservative diagnostic subset, SHA-256 `281d4c1c57095f45978c5cb71d6b20a78f869e2b32016ed4f0718fb8a2d31f16`; it is not an upstream or complete schema.

The official schema and `llms.txt` are fetched from Docker and stored only in the local cache (or in a directory named with `--vendor-dir`, which is excluded from version control). This keeps final validation tied to current upstream sources while retaining the bundled core schema as a clearly labeled offline diagnostic fallback.
