# Docker Agent authoring guide

## Contents

1. Scope and source policy
2. Canonical document shape
3. Agents and orchestration
4. Models and providers
5. Toolsets
6. Reusable definitions
7. MCP and RAG
8. Commands and skills
9. Structured output, hooks, permissions, budgets, and flavors
10. Environment variables and secrets
11. Formatting and validation
12. Common failure modes

## 1. Scope and source policy

Docker Agent configuration is declarative YAML consumed by `docker agent` or `docker-agent`. This guide is a compact control reference, not a replacement for the official schema. For any field not shown here, locate its official page through `llms.txt` and validate against the current schema.

The main documentation tracks the repository's `main` branch and may be ahead of a released binary. When compatibility matters, compare the user's CLI version with stable Docker documentation.

## 2. Canonical document shape

Use only required sections and keep this order:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json
version: "16"

metadata: {}
providers: {}
models: {}
mcps: {}
rag: {}
commands: {}
skills: {}
toolsets: {}
permissions: {}
runtime: {}
budget: {}
budgets: {}
flavors: {}
agents: {}
```

`agents` is required and must contain at least one agent. The current official schema determines the latest accepted version; `16` is the bundled snapshot value, not a permanent constant.

Minimal pattern:

```yaml
version: "16"

agents:
  root:
    model: openai/gpt-5-mini
    description: General-purpose assistant
    instruction: |
      Help the user accurately and concisely.
    toolsets:
      - type: think
```

## 3. Agents and orchestration

An agent normally contains:

- `model`: named model or inline `provider/model` reference.
- `description`: concise capability summary used for delegation.
- `instruction` or `instruction_file`: behavior definition. Do not set both.
- `toolsets`: inline tools.
- `use_toolsets`, `use_commands`, `use_skills`: references to reusable top-level groups.
- `sub_agents`: agents the current agent can delegate work to.
- `handoffs`: agents that can receive the active conversation.
- `force_handoff`: deterministic next agent after a final response.

Local `sub_agents`, `handoffs`, and `force_handoff` values must match keys under `agents`. External agents use URLs or OCI-style references. Prefer immutable OCI digests for reproducibility.

Use `root` for the entry agent unless the target runtime or invocation explicitly selects another agent.

`instruction_file` paths must be local relative paths inside the configuration directory. Avoid absolute paths and `..` traversal. Use inline block scalars for portable single-file configurations.

## 4. Models and providers

Use an inline model for a simple one-off configuration:

```yaml
model: openai/gpt-5-mini
```

Use named models for reuse or custom settings:

```yaml
models:
  primary:
    provider: openai
    model: gpt-5-mini
    temperature: 0.2

agents:
  root:
    model: primary
    description: Coding assistant
    instruction: |
      Implement and review code carefully.
```

Custom providers define reusable connection defaults:

```yaml
providers:
  internal:
    provider: openai
    base_url: "${env.INTERNAL_LLM_BASE_URL}"
    token_key: INTERNAL_LLM_API_KEY

models:
  internal-fast:
    provider: internal
    model: fast-model
```

`token_key` is the name of an environment variable, not `${env...}`. `base_url`, headers, and tool environments can use interpolation. Do not guess provider-specific fields; consult the model and provider documentation.

Docker Agent also accepts a bare primary model name such as `gpt-4` or `claude`, which the target runtime may resolve through its model catalogue or default provider. Prefer an explicit `provider/model` value or a named entry under `models` for deterministic, portable configurations. In fields documented specifically as “named model or inline provider/model” (for example `compaction_model`, routing targets, and `first_available` candidates), define every bare named reference under `models`.

## 5. Toolsets

Current main-branch schema toolset types include:

```text
mcp, mcp_catalog, script, think, memory, filesystem, file, shell,
background_jobs, tasks, plan, session_plan, session_context, todo,
fetch, api, a2a, lsp, user_prompt, openapi, open_url, model_picker,
background_agents, scheduler, rag, git, webhook
```

Type-specific minimums:

- `mcp`: one of `ref`, `command`, or `remote`.
- `lsp`: `command`.
- `api`: `api_config`.
- `webhook`: `webhook_config`.
- `a2a`, `openapi`, `open_url`: `url`.
- `model_picker`: `models`.
- `rag`: typically `ref` to a top-level RAG source or an inline `rag_config` where supported.
- `script`: `shell` definitions.

Use only fields accepted for that tool type. For example, `path` is intended for stateful tools such as `memory` or `tasks`; it is not a generic tool field.

Least-privilege examples:

```yaml
toolsets:
  - type: filesystem
    readonly: true
    allow_list:
      - .
  - type: think
```

```yaml
toolsets:
  - type: shell
  - type: filesystem
```

`readonly` controls mutation; `allow_list` controls which paths can be read. Omitting `allow_list` permits every path reachable by the process. The second example grants broad local execution and write capabilities. Add it only when the task requires them and combine it with permissions and sandboxing appropriate to the environment.

## 6. Reusable definitions

Top-level maps let several agents share one definition:

- `models`: reusable model configurations.
- `mcps`: reusable MCP servers.
- `rag`: reusable knowledge sources.
- `commands`: reusable slash-command groups.
- `skills`: reusable Docker Agent skill groups.
- `toolsets`: reusable toolset configurations.
- `budgets`: shared named resource ceilings.

References must resolve exactly. Inline entries take precedence where the official documentation says overrides are allowed.

## 7. MCP and RAG

Reusable MCP pattern:

```yaml
mcps:
  docs:
    ref: docker:context7

  github:
    ref: docker:github-official
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${env.GITHUB_PERSONAL_ACCESS_TOKEN}"

agents:
  root:
    model: openai/gpt-5-mini
    description: Repository assistant
    instruction: |
      Research documentation and repository state before editing.
    toolsets:
      - type: mcp
        ref: docs
      - type: mcp
        ref: github
```

An MCP `ref` beginning with `docker:` refers to a Docker MCP catalog item. A plain local name should exist under top-level `mcps`.

Reusable RAG pattern:

```yaml
rag:
  project_docs:
    tool:
      description: Search project documentation
    docs:
      - ./docs
    strategies:
      - type: bm25

agents:
  root:
    model: openai/gpt-5-mini
    description: Documentation assistant
    instruction: |
      Ground answers in the indexed project documentation.
    toolsets:
      - type: rag
        ref: project_docs
```

RAG strategy fields change more often than the core agent shape. Consult the current RAG page and schema before adding embeddings, dimensions, hybrid fusion, or reranking.

## 8. Commands and skills

Define reusable command groups under `commands` and reference them with `use_commands`. Define reusable Docker Agent skill groups under `skills` and reference them with `use_skills`.

Do not confuse this packaged Codex/ChatGPT skill with Docker Agent's own runtime `skills` configuration. The generated YAML must follow Docker Agent's schema for that section.

## 9. Structured output, hooks, permissions, budgets, and flavors

`structured_output` belongs on an agent and requires a `name` plus a JSON Schema object. When strict output is requested, define every required property explicitly and set `additionalProperties` deliberately.

Hooks execute deterministic commands or built-ins at lifecycle points. Treat command hooks as executable code: quote safely, avoid interpolating untrusted input into a shell, and use the narrowest event and matcher.

Permissions control whether tools are allowed, denied, or require approval. They are client-side policy, not a security boundary or filesystem sandbox. Do not enable autonomous or broad allow behavior unless the user explicitly requests it and understands the risk.

Top-level `budget` applies run-wide ceilings. Named top-level `budgets` are shared pots referenced from an agent's `budgets` list; every referenced name must exist.

`flavors` are named merge patches applied at runtime. Arrays replace by default; `key+` appends and `key-` removes. Validate the base document and each intended flavor combination with the target runtime.

## 10. Environment variables and secrets

Canonical interpolation:

```yaml
env:
  SERVICE_TOKEN: "${env.SERVICE_TOKEN}"
headers:
  Authorization: "Bearer ${env.SERVICE_TOKEN}"
```

Never write real secret values. Avoid example strings that look like live keys. Document required variable names separately.

Common provider defaults include environment variables such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GOOGLE_API_KEY`, but confirm the selected provider's official documentation.

## 11. Formatting and validation

Formatting rules:

- two spaces per mapping level;
- block-style sequences, not flow arrays, in final output;
- `|` block scalars for multi-line instructions;
- quote values containing `${...}`, colons that could be ambiguous, or version numbers that the schema declares as strings;
- no YAML anchors or aliases;
- no duplicate keys;
- no tabs or trailing spaces;
- LF line endings and one final newline.

Run the bundled validator with the official schema. The validator also checks local references, type-specific tool requirements, obvious embedded secrets, formatting, and optional Docker Agent dry-run.

## 12. Common failure modes

- **`Additional properties are not allowed`**: a field is misspelled, placed at the wrong level, or unsupported by the target schema.
- **Named model not found**: use `provider/model` inline or define the bare name under `models`.
- **Local sub-agent not found**: add the agent under `agents` or change the value to an explicit external reference.
- **MCP missing ref/command/remote**: provide exactly the intended connection method.
- **Tool-specific field rejected**: move or remove a field that is not valid for the selected `type`.
- **Environment variable missing**: keep the YAML secret-free, document and set the variable outside the file, then rerun dry-run.
- **Main docs feature rejected by installed CLI**: inspect `docker agent version`, use stable docs for that release, or upgrade intentionally.
- **Schema passes but execution fails**: check model credentials, local model availability, command paths, MCP binaries, network access, and referenced files.
