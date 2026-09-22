# Third-party notices

This skill includes reference material derived from the Docker Agent project and documentation:

- Project: Docker Agent
- Copyright: Docker, Inc. and Docker Agent contributors
- Upstream repository: https://github.com/docker/docker-agent
- License: Apache License 2.0

The complete upstream license text is included in `references/docker-agent-LICENSE.txt`.

Bundled upstream snapshot:

- `references/upstream-mcp-definitions.yaml` from the official repository examples (verbatim).

Not bundled: the official `agent-schema.json` and `llms.txt` are downloaded on demand by `scripts/refresh_official_sources.py`. A local copy may appear under `references/` when the script is run with `--vendor-dir references`; such copies are optional local cache snapshots, are excluded from version control, and remain Docker's Apache-2.0-licensed material.

Docker and Docker Agent are trademarks or product names of Docker, Inc. This independent skill package is not an official Docker distribution.
