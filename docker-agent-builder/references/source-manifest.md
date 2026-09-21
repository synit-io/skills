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

Use `scripts/refresh_official_sources.py` to cache the latest official schema and `llms.txt`. The validator records the source URL, retrieval time, SHA-256 digest, and latest config version. Do not replace official-source content with third-party summaries.

Agents using this skill should inspect `agent-schema.meta.json` and `llms.meta.json` in `~/.cache/docker-agent-builder`. When either cache entry is missing or older than seven days, refresh both sources when network access is available. Release artifacts exclude these caches; each agent maintains them locally.

## Bundled snapshot inventory

- `references/upstream-mcp-definitions.yaml` — official reusable-MCP example, SHA-256 `225cedda1da0673ae8a360d05af3d2e9d1cdef8f10c7692a50608f3df853d4bc`.
- `references/docker-agent-LICENSE.txt` — upstream Apache License 2.0 text, SHA-256 `58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd`.
- `references/core-schema.json` — skill-authored conservative diagnostic subset, SHA-256 `281d4c1c57095f45978c5cb71d6b20a78f869e2b32016ed4f0718fb8a2d31f16`; it is not an upstream or complete schema.

The official schema and `llms.txt` are fetched from Docker and stored only in the agent's local cache. They are intentionally excluded from release artifacts. This keeps final validation tied to current upstream sources while retaining the bundled core schema as a clearly labeled offline diagnostic fallback.
