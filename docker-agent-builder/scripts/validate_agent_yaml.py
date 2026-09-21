#!/usr/bin/env python3
"""Format and validate Docker Agent configurations.

Validation layers:
  1. YAML syntax and duplicate keys
  2. Canonical clean formatting
  3. Docker's official JSON Schema (default)
  4. Cross-reference and security semantics
  5. Optional `docker agent run --dry-run`

Exit codes:
  0 - validation passed (warnings may remain)
  1 - file is invalid or unsafe
  2 - validation could not be completed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator, Optional

try:
    from jsonschema import FormatChecker
    from jsonschema.validators import validator_for
except ImportError:  # pragma: no cover - handled in main
    FormatChecker = None
    validator_for = None

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.constructor import DuplicateKeyError
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString
except ImportError:  # pragma: no cover - handled in main
    YAML = None
    CommentedMap = None
    CommentedSeq = None
    DuplicateKeyError = Exception
    DoubleQuotedScalarString = str
    LiteralScalarString = str


OFFICIAL_SCHEMA_URL = (
    "https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json"
)
OFFICIAL_SCHEMA_COMMENT = f"# yaml-language-server: $schema={OFFICIAL_SCHEMA_URL}"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "docker-agent-builder"
BUNDLED_CORE_SCHEMA = Path(__file__).resolve().parent.parent / "references" / "core-schema.json"

TOP_LEVEL_ORDER = [
    "version",
    "metadata",
    "providers",
    "models",
    "mcps",
    "rag",
    "commands",
    "skills",
    "toolsets",
    "permissions",
    "runtime",
    "budget",
    "budgets",
    "flavors",
    "agents",
]

AGENT_ORDER = [
    "model",
    "fallback",
    "description",
    "welcome_message",
    "instruction",
    "instruction_file",
    "harness",
    "code_mode_tools",
    "sub_agents",
    "handoffs",
    "force_handoff",
    "toolsets",
    "use_toolsets",
    "max_iterations",
    "budgets",
    "max_consecutive_tool_calls",
    "max_old_tool_call_tokens",
    "max_tool_result_tokens",
    "auto_compact",
    "compaction_threshold",
    "compaction_model",
    "add_prompt_files",
    "commands",
    "use_commands",
    "skills",
    "use_skills",
    "structured_output",
    "add_description_parameter",
    "hooks",
    "cache",
    "redact_secrets",
]

MODEL_ORDER = [
    "provider",
    "model",
    "description",
    "base_url",
    "token_key",
    "bypass_models_gateway",
    "temperature",
    "max_tokens",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "parallel_tool_calls",
    "thinking_budget",
    "task_budget",
    "provider_opts",
    "fallback",
    "routing",
    "first_available",
    "title_model",
    "compaction_model",
    "compaction_threshold",
    "capabilities",
    "cost",
    "auth",
]

TOOLSET_ORDER = [
    "type",
    "ref",
    "name",
    "command",
    "remote",
    "args",
    "url",
    "model",
    "instruction",
    "tools",
    "env",
    "headers",
    "config",
    "path",
    "working_dir",
    "version",
    "readonly",
    "allow_list",
    "deny_list",
    "ignore_vcs",
    "post_edit",
    "defer",
    "lifecycle",
    "shell",
    "api_config",
    "webhook_config",
    "rag_config",
    "models",
]

TOOLSET_TYPES = {
    "mcp",
    "mcp_catalog",
    "script",
    "think",
    "memory",
    "filesystem",
    "file",
    "shell",
    "background_jobs",
    "tasks",
    "plan",
    "session_plan",
    "session_context",
    "todo",
    "fetch",
    "api",
    "a2a",
    "lsp",
    "user_prompt",
    "openapi",
    "open_url",
    "model_picker",
    "background_agents",
    "scheduler",
    "rag",
    "git",
    "webhook",
}

BUILTIN_PROVIDERS = {
    "openai",
    "anthropic",
    "google",
    "amazon-bedrock",
    "dmr",
    "requesty",
    "openrouter",
    "azure",
    "xai",
    "ollama",
    "mistral",
    "baseten",
    "ovhcloud",
    "groq",
    "fireworks",
    "deepseek",
    "cerebras",
    "together",
    "huggingface",
    "moonshot",
    "vercel",
    "cloudflare-workers-ai",
    "cloudflare-ai-gateway",
    "nvidia",
    "github-copilot",
    "chatgpt",
}

SPECIAL_MODEL_REFS = {"auto"}
ENV_REF_RE = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}")
SCHEMA_COMMENT_RE = re.compile(
    r"^\s*#\s*yaml-language-server:\s*\$schema=\S*agent-schema\.json(?:\?\S*)?\s*$",
    re.IGNORECASE,
)
SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|(?:access[_-]?)?token|auth(?:orization)?|password|passwd|secret|private[_-]?key|credentials?)$",
    re.IGNORECASE,
)
SENSITIVE_TEMPLATE_RE = re.compile(
    r"^(?:Bearer\s+)?\$\{env\.[A-Za-z_][A-Za-z0-9_]*\}$",
    re.IGNORECASE,
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KNOWN_SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai-style-token", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
]
ENVIRONMENT_FAILURE_RE = re.compile(
    r"the following environment variables must be set|"
    r"(?:environment variable|api key|credential|authentication|token).{0,80}(?:missing|not set|required|unavailable)|"
    r"(?:missing|not set|required).{0,80}(?:environment variable|api key|credential|token)",
    re.IGNORECASE | re.DOTALL,
)
CONFIGURATION_FAILURE_RE = re.compile(
    r"configuration invalid|invalid configuration|unknown field|additional propert(?:y|ies)|"
    r"schema (?:error|validation)|yaml (?:error|parse)|parse error|failed to (?:load|parse) config",
    re.IGNORECASE,
)


@dataclass
class Issue:
    severity: str
    gate: str
    code: str
    path: str
    message: str


@dataclass
class SchemaInfo:
    source_kind: str
    source: str
    sha256: str
    latest_version: Optional[str]
    fetched_at: Optional[str]
    official: bool
    schema: dict[str, Any] = field(repr=False)
    warning: Optional[str] = None


@dataclass
class DockerCheck:
    status: str
    command: Optional[list[str]] = None
    returncode: Optional[int] = None
    output: Optional[str] = None


@dataclass
class ValidationReport:
    file: Optional[str]
    validation_level: str
    schema: dict[str, Any]
    docker: dict[str, Any]
    required_environment: list[str]
    changed: bool
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "pass" if self.passed else "fail",
            "file": self.file,
            "validation_level": self.validation_level,
            "schema": self.schema,
            "docker": self.docker,
            "required_environment": self.required_environment,
            "changed": self.changed,
            "issues": [asdict(issue) for issue in self.issues],
        }


class ValidationIncomplete(RuntimeError):
    """Raised when a required validation dependency or source is unavailable."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_name = handle.name
    os.replace(temp_name, path)


def fetch_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "docker-agent-builder/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError as urllib_error:
        curl = shutil.which("curl")
        if not curl:
            raise
        try:
            result = subprocess.run(
                [
                    curl,
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--location",
                    "--max-time",
                    str(timeout),
                    "--user-agent",
                    "docker-agent-builder/1.0",
                    url,
                ],
                capture_output=True,
                timeout=timeout + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as curl_error:
            raise urllib.error.URLError(
                f"urllib failed ({urllib_error}); curl fallback failed ({curl_error})"
            ) from curl_error
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise urllib.error.URLError(
                f"urllib failed ({urllib_error}); curl fallback failed ({message})"
            )
        return result.stdout


def load_json_bytes(data: bytes, source: str) -> dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationIncomplete(f"Invalid JSON schema from {source}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationIncomplete(f"Schema from {source} is not a JSON object")
    return parsed


def schema_latest_version(schema: Mapping[str, Any]) -> Optional[str]:
    try:
        values = schema["properties"]["version"]["enum"]
    except (KeyError, TypeError):
        return None
    if not isinstance(values, list):
        return None
    versions = [str(value) for value in values]
    numeric = [value for value in versions if value.isdigit()]
    if numeric:
        return str(max(int(value) for value in numeric))
    return versions[-1] if versions else None


def schema_toolset_types(schema: Mapping[str, Any]) -> set[str]:
    try:
        values = schema["definitions"]["Toolset"]["properties"]["type"]["enum"]
    except (KeyError, TypeError):
        try:
            values = schema["definitions"]["toolset"]["properties"]["type"]["enum"]
        except (KeyError, TypeError):
            return set(TOOLSET_TYPES)
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return set(TOOLSET_TYPES)
    return set(values)


def is_official_schema(schema: Mapping[str, Any]) -> bool:
    title = str(schema.get("title", ""))
    schema_id = str(schema.get("$id", ""))
    description = str(schema.get("description", ""))
    return (
        title == "Docker Agent Configuration"
        and (
            "docker/docker-agent" in schema_id
            or "Docker Agent" in description
        )
    )


def load_schema(
    *,
    mode: str,
    cache_dir: Path,
    schema_path: Optional[Path],
    offline: bool,
    refresh: bool,
    timeout: float,
) -> SchemaInfo:
    if schema_path is not None:
        try:
            data = schema_path.read_bytes()
        except OSError as exc:
            raise ValidationIncomplete(f"Cannot read schema {schema_path}: {exc}") from exc
        schema = load_json_bytes(data, str(schema_path))
        return SchemaInfo(
            source_kind="explicit",
            source=str(schema_path.resolve()),
            sha256=sha256_bytes(data),
            latest_version=schema_latest_version(schema),
            fetched_at=None,
            official=False,
            schema=schema,
            warning=(
                "An explicit schema was used. Its provenance is caller-controlled, so the "
                "validator does not label this result as official."
            ),
        )

    if mode == "core":
        try:
            data = BUNDLED_CORE_SCHEMA.read_bytes()
        except OSError as exc:
            raise ValidationIncomplete(f"Cannot read bundled core schema: {exc}") from exc
        schema = load_json_bytes(data, str(BUNDLED_CORE_SCHEMA))
        return SchemaInfo(
            source_kind="bundled-core",
            source=str(BUNDLED_CORE_SCHEMA),
            sha256=sha256_bytes(data),
            latest_version=schema_latest_version(schema),
            fetched_at=None,
            official=False,
            schema=schema,
        )

    cache_schema = cache_dir / "agent-schema.json"
    cache_meta = cache_dir / "agent-schema.meta.json"
    fetch_error: Optional[str] = None

    if not offline:
        try:
            data = fetch_bytes(OFFICIAL_SCHEMA_URL, timeout)
            schema = load_json_bytes(data, OFFICIAL_SCHEMA_URL)
            if not is_official_schema(schema):
                raise ValidationIncomplete(
                    "Downloaded schema did not identify itself as Docker Agent Configuration"
                )
            metadata = {
                "source": OFFICIAL_SCHEMA_URL,
                "fetched_at": utc_now(),
                "sha256": sha256_bytes(data),
                "latest_version": schema_latest_version(schema),
            }
            atomic_write(cache_schema, data)
            atomic_write(
                cache_meta,
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            return SchemaInfo(
                source_kind="official-live",
                source=OFFICIAL_SCHEMA_URL,
                sha256=metadata["sha256"],
                latest_version=metadata["latest_version"],
                fetched_at=metadata["fetched_at"],
                official=True,
                schema=schema,
            )
        except (OSError, urllib.error.URLError, ValidationIncomplete) as exc:
            fetch_error = str(exc)
            if refresh:
                raise ValidationIncomplete(
                    f"Could not refresh the official Docker Agent schema: {exc}"
                ) from exc

    if cache_schema.exists():
        try:
            data = cache_schema.read_bytes()
            schema = load_json_bytes(data, str(cache_schema))
            if not is_official_schema(schema):
                raise ValidationIncomplete(
                    f"Cached file is not an official Docker Agent schema: {cache_schema}"
                )
            if not cache_meta.exists():
                raise ValidationIncomplete(
                    f"Cached official schema metadata is missing: {cache_meta}"
                )
            try:
                raw_meta = json.loads(cache_meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValidationIncomplete(
                    f"Cannot read cached official schema metadata: {exc}"
                ) from exc
            if not isinstance(raw_meta, dict):
                raise ValidationIncomplete("Cached official schema metadata is not an object")
            metadata = raw_meta
            actual_digest = sha256_bytes(data)
            if metadata.get("source") != OFFICIAL_SCHEMA_URL:
                raise ValidationIncomplete(
                    "Cached schema metadata does not name the official Docker Agent schema URL"
                )
            if metadata.get("sha256") != actual_digest:
                raise ValidationIncomplete(
                    "Cached official schema digest does not match agent-schema.meta.json"
                )
            warning = None
            if fetch_error:
                warning = (
                    "Live schema refresh failed; using the cached official schema. "
                    f"Refresh error: {fetch_error}"
                )
            elif offline:
                warning = "Offline mode: using the cached official schema."
            return SchemaInfo(
                source_kind="official-cache",
                source=str(cache_schema),
                sha256=actual_digest,
                latest_version=schema_latest_version(schema),
                fetched_at=metadata.get("fetched_at"),
                official=True,
                schema=schema,
                warning=warning,
            )
        except OSError as exc:
            raise ValidationIncomplete(f"Cannot read cached schema: {exc}") from exc

    reason = "offline mode was requested" if offline else (fetch_error or "download failed")
    raise ValidationIncomplete(
        "The official Docker Agent schema is unavailable and no official cache exists; "
        f"{reason}. Run scripts/refresh_official_sources.py with network access."
    )


def yaml_parser() -> Any:
    if YAML is None:
        raise ValidationIncomplete(
            "Missing ruamel.yaml. Install scripts/requirements.txt before validating."
        )
    parser = YAML(typ="rt")
    parser.version = (1, 2)
    parser.preserve_quotes = True
    parser.allow_duplicate_keys = False
    parser.indent(mapping=2, sequence=4, offset=2)
    parser.width = 100
    parser.default_flow_style = False
    return parser


def strip_schema_comment(text: str) -> str:
    lines: list[str] = []
    in_preamble = True
    for line in text.splitlines():
        if in_preamble and SCHEMA_COMMENT_RE.match(line):
            continue
        if in_preamble and line.strip() and not line.lstrip().startswith("#"):
            in_preamble = False
        lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines) + ("\n" if lines else "")


def parse_yaml(text: str, source: str) -> Any:
    parser = yaml_parser()
    try:
        documents = list(parser.load_all(text))
    except DuplicateKeyError as exc:
        raise ValueError(f"Duplicate YAML key: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"YAML parse error in {source}: {exc}") from exc
    if len(documents) != 1:
        raise ValueError(
            f"Docker Agent config must contain exactly one YAML document; found {len(documents)}"
        )
    data = documents[0]
    if not isinstance(data, Mapping):
        raise ValueError("Docker Agent config root must be a YAML mapping")
    return data


def to_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_plain(item) for item in value]
    return value


def set_block_style(value: Any) -> None:
    if CommentedMap is not None and isinstance(value, CommentedMap):
        value.fa.set_block_style()
        for item in value.values():
            set_block_style(item)
    elif CommentedSeq is not None and isinstance(value, CommentedSeq):
        value.fa.set_block_style()
        for item in value:
            set_block_style(item)


def reorder_mapping(mapping: Any, order: list[str]) -> None:
    if not isinstance(mapping, Mapping) or not hasattr(mapping, "move_to_end"):
        return
    for key in reversed(order):
        if key in mapping:
            mapping.move_to_end(key, last=False)


def normalize_structure(data: Any) -> None:
    set_block_style(data)
    reorder_mapping(data, TOP_LEVEL_ORDER)

    if "version" in data:
        data["version"] = DoubleQuotedScalarString(str(data["version"]))

    models = data.get("models")
    if isinstance(models, Mapping):
        for model in models.values():
            reorder_mapping(model, MODEL_ORDER)

    providers = data.get("providers")
    if isinstance(providers, Mapping):
        for provider in providers.values():
            reorder_mapping(provider, MODEL_ORDER)

    mcps = data.get("mcps")
    if isinstance(mcps, Mapping):
        for definition in mcps.values():
            reorder_mapping(definition, TOOLSET_ORDER)

    reusable_toolsets = data.get("toolsets")
    if isinstance(reusable_toolsets, Mapping):
        for toolset in reusable_toolsets.values():
            reorder_mapping(toolset, TOOLSET_ORDER)

    agents = data.get("agents")
    if isinstance(agents, Mapping):
        for agent in agents.values():
            if not isinstance(agent, Mapping):
                continue
            reorder_mapping(agent, AGENT_ORDER)
            if "instruction" in agent and isinstance(agent["instruction"], str):
                instruction = str(agent["instruction"])
                if "\n" in instruction or len(instruction) > 80:
                    # LiteralScalarString preserves the scalar value. Do not append a
                    # newline merely to obtain the visual `|` chomping style.
                    agent["instruction"] = LiteralScalarString(instruction)
            toolsets = agent.get("toolsets")
            if isinstance(toolsets, Sequence) and not isinstance(toolsets, str):
                for toolset in toolsets:
                    reorder_mapping(toolset, TOOLSET_ORDER)


def dump_yaml(data: Any) -> str:
    parser = yaml_parser()
    # Keep YAML 1.2 parsing semantics without emitting a document directive/header.
    parser.version = None
    parser.explicit_start = False
    stream = StringIO()
    parser.dump(data, stream)
    body = strip_schema_comment(stream.getvalue())
    body_lines = [line.rstrip(" \t") for line in body.splitlines()]
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    rendered = OFFICIAL_SCHEMA_COMMENT + "\n"
    if body_lines:
        rendered += "\n" + "\n".join(body_lines) + "\n"
    return rendered


def json_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        elif isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", part):
            result += f".{part}"
        else:
            escaped = str(part).replace("\\", "\\\\").replace("'", "\\'")
            result += f"['{escaped}']"
    return result


def iter_nodes(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from iter_nodes(item, path + (str(key),))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            yield from iter_nodes(item, path + (index,))


def contains_flow_style(value: Any) -> list[str]:
    paths: list[str] = []
    for path, node in iter_nodes(value):
        flow = getattr(getattr(node, "fa", None), "flow_style", None)
        if callable(flow):
            try:
                if flow() is True:
                    paths.append(json_path(path))
            except Exception:
                pass
    return paths


def contains_anchors(value: Any) -> list[str]:
    paths: list[str] = []
    for path, node in iter_nodes(value):
        anchor = getattr(node, "anchor", None)
        anchor_value = getattr(anchor, "value", None)
        if anchor_value:
            paths.append(json_path(path))
    return paths


def relative_order_is_canonical(mapping: Mapping[str, Any], order: list[str]) -> bool:
    positions = {key: index for index, key in enumerate(order)}
    seen = [positions[key] for key in mapping.keys() if key in positions]
    return seen == sorted(seen)


def indentation_jump_lines(text: str) -> list[int]:
    issues: list[int] = []
    previous_indent = 0
    block_scalar_indent: Optional[int] = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_scalar_indent is not None:
            if indent > block_scalar_indent:
                continue
            block_scalar_indent = None
        stripped = line.lstrip(" ")
        if stripped.startswith("#"):
            continue
        if indent > previous_indent + 2:
            issues.append(line_number)
        previous_indent = indent
        if re.search(r"[|>]\s*(?:[+-]?\d*[+-]?\s*)?(?:#.*)?$", stripped):
            block_scalar_indent = indent
    return issues


def format_issues(text: str, data: Any) -> list[Issue]:
    issues: list[Issue] = []
    if text.startswith("\ufeff"):
        issues.append(Issue("error", "format", "utf8-bom", "$", "Remove the UTF-8 BOM."))
    if "\r" in text:
        issues.append(Issue("error", "format", "line-endings", "$", "Use LF line endings."))
    if "\t" in text:
        issues.append(Issue("error", "format", "tabs", "$", "Tabs are not allowed."))
    indentation = indentation_jump_lines(text)
    if indentation:
        sample = ", ".join(str(line) for line in indentation[:8])
        issues.append(
            Issue(
                "error",
                "format",
                "indentation",
                "$",
                f"Use two-space indentation; invalid indentation jump on line(s): {sample}.",
            )
        )
    trailing = [index + 1 for index, line in enumerate(text.splitlines()) if line.rstrip(" \t") != line]
    if trailing:
        sample = ", ".join(str(line) for line in trailing[:8])
        issues.append(
            Issue(
                "error",
                "format",
                "trailing-whitespace",
                "$",
                f"Remove trailing whitespace on line(s): {sample}.",
            )
        )
    if text and not text.endswith("\n"):
        issues.append(Issue("error", "format", "final-newline", "$", "Add one final newline."))
    first_nonempty = next((line for line in text.splitlines() if line.strip()), "")
    if first_nonempty.strip() != OFFICIAL_SCHEMA_COMMENT:
        issues.append(
            Issue(
                "error",
                "format",
                "schema-comment",
                "$",
                "Place the official Docker Agent schema comment on the first non-empty line.",
            )
        )
    if not relative_order_is_canonical(data, TOP_LEVEL_ORDER):
        issues.append(
            Issue(
                "error",
                "format",
                "top-level-order",
                "$",
                "Reorder top-level sections to the canonical Docker Agent order.",
            )
        )
    agents = data.get("agents") if isinstance(data, Mapping) else None
    if isinstance(agents, Mapping):
        for name, agent in agents.items():
            if isinstance(agent, Mapping) and not relative_order_is_canonical(agent, AGENT_ORDER):
                issues.append(
                    Issue(
                        "error",
                        "format",
                        "agent-order",
                        json_path(("agents", str(name))),
                        "Reorder agent fields to the canonical order.",
                    )
                )
    for path in contains_flow_style(data):
        issues.append(
            Issue("error", "format", "flow-style", path, "Use block-style YAML instead of flow style.")
        )
    for path in contains_anchors(data):
        issues.append(
            Issue("error", "format", "yaml-anchor", path, "YAML anchors and aliases are not allowed.")
        )
    version = data.get("version") if isinstance(data, Mapping) else None
    if version is not None and not isinstance(version, str):
        issues.append(
            Issue(
                "error",
                "format",
                "version-string",
                "$.version",
                "Quote the configuration version because the official schema declares it as a string.",
            )
        )
    return issues


def leaf_schema_errors(error: Any) -> Iterator[Any]:
    if getattr(error, "context", None):
        for child in error.context:
            yield from leaf_schema_errors(child)
    else:
        yield error


def validate_json_schema(data: Any, schema_info: SchemaInfo) -> list[Issue]:
    if validator_for is None or FormatChecker is None:
        raise ValidationIncomplete(
            "Missing jsonschema. Install scripts/requirements.txt before validating."
        )
    try:
        validator_cls = validator_for(schema_info.schema)
        validator_cls.check_schema(schema_info.schema)
        validator = validator_cls(schema_info.schema, format_checker=FormatChecker())
    except Exception as exc:
        raise ValidationIncomplete(f"Cannot initialize JSON Schema validator: {exc}") from exc

    raw_errors = sorted(
        validator.iter_errors(to_plain(data)),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), item.message),
    )
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for top_error in raw_errors:
        candidates = list(leaf_schema_errors(top_error)) or [top_error]
        # Limit noisy oneOf/anyOf expansion while keeping actionable leaf errors.
        for error in candidates[:12]:
            path = json_path(error.absolute_path)
            key = (path, error.message)
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                Issue(
                    "error",
                    "schema",
                    f"jsonschema-{error.validator or 'validation'}",
                    path,
                    error.message,
                )
            )
            if len(issues) >= 100:
                issues.append(
                    Issue(
                        "error",
                        "schema",
                        "too-many-errors",
                        "$",
                        "Schema validation produced more than 100 errors; fix earlier errors first.",
                    )
                )
                return issues
    return issues


def is_external_reference(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "oci://")):
        return True
    if "/" in value or "@sha256:" in lowered:
        return True
    if ":" in value:
        return True
    return False


def is_inline_model(value: str) -> bool:
    return "/" in value or value in SPECIAL_MODEL_REFS


def list_strings(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def add_missing_reference(
    issues: list[Issue],
    *,
    path: tuple[Any, ...],
    kind: str,
    value: str,
    available: set[str],
) -> None:
    if value not in available:
        issues.append(
            Issue(
                "error",
                "semantic",
                f"unknown-{kind}",
                json_path(path),
                f"Unknown {kind} reference {value!r}; define it at the corresponding top level.",
            )
        )


def check_model_ref(
    issues: list[Issue],
    value: Any,
    path: tuple[Any, ...],
    model_names: set[str],
    *,
    allow_bare_catalog_model: bool = False,
) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    if is_inline_model(value) or value in model_names:
        return
    # The primary AgentConfig.model field explicitly permits a bare model name
    # such as `gpt-4` or `claude`; the runtime may resolve it through its model
    # catalogue/default provider. Other model-reference fields are documented as
    # named entries or inline provider/model specs and should resolve statically.
    if allow_bare_catalog_model and "/" not in value:
        return
    add_missing_reference(
        issues,
        path=path,
        kind="model",
        value=value,
        available=model_names,
    )


def check_instruction_path(
    issues: list[Issue],
    value: str,
    path: tuple[Any, ...],
    *,
    config_dir: Path,
) -> None:
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        issues.append(
            Issue(
                "error",
                "semantic",
                "instruction-file-url",
                json_path(path),
                "instruction_file must be a local relative path, not a URL.",
            )
        )
        return
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or ".." in posix.parts or ".." in windows.parts:
        issues.append(
            Issue(
                "error",
                "semantic",
                "unsafe-instruction-file",
                json_path(path),
                "instruction_file must stay inside the configuration directory and use a relative path.",
            )
        )
        return
    candidate = config_dir / value
    try:
        resolved_base = config_dir.resolve()
        resolved_candidate = candidate.resolve()
    except OSError as exc:
        issues.append(
            Issue(
                "error",
                "semantic",
                "instruction-file-resolution",
                json_path(path),
                f"Cannot resolve instruction_file: {exc}",
            )
        )
        return
    if not resolved_candidate.is_relative_to(resolved_base):
        issues.append(
            Issue(
                "error",
                "semantic",
                "unsafe-instruction-file",
                json_path(path),
                "instruction_file resolves outside the configuration directory.",
            )
        )
    elif not resolved_candidate.exists():
        issues.append(
            Issue(
                "error",
                "semantic",
                "missing-instruction-file",
                json_path(path),
                f"instruction_file does not exist: {value}",
            )
        )
    elif not resolved_candidate.is_file():
        issues.append(
            Issue(
                "error",
                "semantic",
                "invalid-instruction-file",
                json_path(path),
                f"instruction_file is not a regular file: {value}",
            )
        )


def force_handoff_cycles(agents: Mapping[str, Any]) -> list[list[str]]:
    graph: dict[str, str] = {}
    names = {str(name) for name in agents.keys()}
    for raw_name, config in agents.items():
        name = str(raw_name)
        if not isinstance(config, Mapping):
            continue
        target = config.get("force_handoff")
        if isinstance(target, str) and target in names:
            graph[name] = target

    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        target = graph.get(node)
        if target is not None:
            if state.get(target, 0) == 0:
                visit(target)
            elif state.get(target) == 1:
                start = stack.index(target)
                cycles.append(stack[start:] + [target])
        stack.pop()
        state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            visit(node)
    return cycles


def semantic_issues(
    data: Any,
    *,
    latest_version: Optional[str],
    allow_legacy_version: bool,
    config_dir: Path,
    toolset_types: set[str],
) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(data, Mapping):
        return [Issue("error", "semantic", "root-type", "$", "Root must be a mapping.")]

    version = data.get("version")
    if version is None:
        issues.append(
            Issue(
                "error",
                "semantic",
                "missing-version",
                "$.version",
                "New and maintained configs must declare the current Docker Agent config version.",
            )
        )
    elif latest_version and str(version) != latest_version:
        severity = "warning" if allow_legacy_version else "error"
        issues.append(
            Issue(
                severity,
                "semantic",
                "non-current-version",
                "$.version",
                f"Config version is {version!r}; the loaded schema advertises {latest_version!r} as latest.",
            )
        )

    agents = data.get("agents")
    if not isinstance(agents, Mapping) or not agents:
        issues.append(
            Issue("error", "semantic", "missing-agents", "$.agents", "Define at least one agent.")
        )
        return issues

    agent_names = {str(name) for name in agents.keys()}
    model_names = set(str(name) for name in (data.get("models") or {}).keys()) if isinstance(data.get("models"), Mapping) else set()
    provider_names = set(str(name) for name in (data.get("providers") or {}).keys()) if isinstance(data.get("providers"), Mapping) else set()
    mcp_names = set(str(name) for name in (data.get("mcps") or {}).keys()) if isinstance(data.get("mcps"), Mapping) else set()
    rag_names = set(str(name) for name in (data.get("rag") or {}).keys()) if isinstance(data.get("rag"), Mapping) else set()
    command_names = set(str(name) for name in (data.get("commands") or {}).keys()) if isinstance(data.get("commands"), Mapping) else set()
    skill_names = set(str(name) for name in (data.get("skills") or {}).keys()) if isinstance(data.get("skills"), Mapping) else set()
    reusable_toolset_names = set(str(name) for name in (data.get("toolsets") or {}).keys()) if isinstance(data.get("toolsets"), Mapping) else set()
    budget_names = set(str(name) for name in (data.get("budgets") or {}).keys()) if isinstance(data.get("budgets"), Mapping) else set()

    if "root" not in agent_names:
        issues.append(
            Issue(
                "warning",
                "semantic",
                "no-root-agent",
                "$.agents",
                "No agent named 'root' exists; ensure invocation explicitly selects the intended entry agent.",
            )
        )

    models = data.get("models")
    if isinstance(models, Mapping):
        for model_name, model in models.items():
            if not isinstance(model, Mapping):
                continue
            provider = model.get("provider")
            if isinstance(provider, str) and provider not in BUILTIN_PROVIDERS and provider not in provider_names:
                issues.append(
                    Issue(
                        "warning",
                        "semantic",
                        "unverified-provider",
                        json_path(("models", str(model_name), "provider")),
                        f"Provider {provider!r} is not in the bundled known-provider list and is not defined under providers; confirm that the target runtime supports it.",
                    )
                )
            for field_name in ("title_model", "compaction_model"):
                check_model_ref(
                    issues,
                    model.get(field_name),
                    ("models", str(model_name), field_name),
                    model_names,
                )
            for index, ref in enumerate(list_strings(model.get("first_available"))):
                check_model_ref(
                    issues,
                    ref,
                    ("models", str(model_name), "first_available", index),
                    model_names,
                )
            fallback = model.get("fallback")
            if isinstance(fallback, Mapping):
                for index, ref in enumerate(list_strings(fallback.get("models"))):
                    check_model_ref(
                        issues,
                        ref,
                        ("models", str(model_name), "fallback", "models", index),
                        model_names,
                    )
            routing = model.get("routing")
            if isinstance(routing, Sequence) and not isinstance(routing, str):
                for index, rule in enumerate(routing):
                    if isinstance(rule, Mapping):
                        check_model_ref(
                            issues,
                            rule.get("model"),
                            ("models", str(model_name), "routing", index, "model"),
                            model_names,
                        )

    providers = data.get("providers")
    if isinstance(providers, Mapping):
        for provider_name, provider in providers.items():
            if isinstance(provider, Mapping):
                check_model_ref(
                    issues,
                    provider.get("compaction_model"),
                    ("providers", str(provider_name), "compaction_model"),
                    model_names,
                )

    for agent_name, agent in agents.items():
        name = str(agent_name)
        base = ("agents", name)
        if not isinstance(agent, Mapping):
            issues.append(
                Issue("error", "semantic", "agent-type", json_path(base), "Agent must be a mapping.")
            )
            continue
        description = agent.get("description")
        if not isinstance(description, str) or not description.strip():
            issues.append(
                Issue(
                    "warning",
                    "semantic",
                    "missing-description",
                    json_path(base + ("description",)),
                    "Give every agent a non-empty description.",
                )
            )
        if "instruction" in agent and "instruction_file" in agent:
            issues.append(
                Issue(
                    "error",
                    "semantic",
                    "instruction-conflict",
                    json_path(base),
                    "Use instruction or instruction_file, not both.",
                )
            )
        if "model" not in agent and "harness" not in agent:
            issues.append(
                Issue(
                    "error",
                    "semantic",
                    "missing-agent-runtime",
                    json_path(base),
                    "Agent must define a model or an external harness.",
                )
            )
        check_model_ref(
            issues,
            agent.get("model"),
            base + ("model",),
            model_names,
            allow_bare_catalog_model=True,
        )
        check_model_ref(
            issues,
            agent.get("compaction_model"),
            base + ("compaction_model",),
            model_names,
        )
        fallback = agent.get("fallback")
        if isinstance(fallback, Mapping):
            for index, ref in enumerate(list_strings(fallback.get("models"))):
                check_model_ref(
                    issues,
                    ref,
                    base + ("fallback", "models", index),
                    model_names,
                )

        structured_output = agent.get("structured_output")
        if isinstance(structured_output, Mapping) and structured_output.get("strict") is True:
            output_schema = structured_output.get("schema")
            if isinstance(output_schema, Mapping):
                properties = output_schema.get("properties")
                required = output_schema.get("required")
                if isinstance(properties, Mapping):
                    required_names = set(list_strings(required))
                    missing_required = [
                        str(key) for key in properties.keys() if str(key) not in required_names
                    ]
                    if missing_required:
                        issues.append(
                            Issue(
                                "error",
                                "semantic",
                                "strict-output-required-properties",
                                json_path(base + ("structured_output", "schema", "required")),
                                "strict structured output must list every property in required; missing: "
                                + ", ".join(sorted(missing_required)),
                            )
                        )

        instruction_file = agent.get("instruction_file")
        if isinstance(instruction_file, str):
            check_instruction_path(
                issues,
                instruction_file,
                base + ("instruction_file",),
                config_dir=config_dir,
            )
        elif isinstance(instruction_file, Sequence) and not isinstance(instruction_file, str):
            for index, item in enumerate(instruction_file):
                if isinstance(item, str):
                    check_instruction_path(
                        issues,
                        item,
                        base + ("instruction_file", index),
                        config_dir=config_dir,
                    )

        for field_name in ("sub_agents", "handoffs"):
            refs = agent.get(field_name)
            if not isinstance(refs, Sequence) or isinstance(refs, str):
                continue
            seen_refs: set[str] = set()
            for index, ref in enumerate(refs):
                if not isinstance(ref, str):
                    continue
                path = base + (field_name, index)
                if ref in seen_refs:
                    issues.append(
                        Issue(
                            "warning",
                            "semantic",
                            "duplicate-reference",
                            json_path(path),
                            f"Duplicate {field_name} reference {ref!r}.",
                        )
                    )
                seen_refs.add(ref)
                if ref == name:
                    issues.append(
                        Issue(
                            "error",
                            "semantic",
                            "self-agent-reference",
                            json_path(path),
                            f"Agent {name!r} cannot reference itself in {field_name}.",
                        )
                    )
                elif not is_external_reference(ref) and ref not in agent_names:
                    add_missing_reference(
                        issues,
                        path=path,
                        kind="agent",
                        value=ref,
                        available=agent_names,
                    )

        force_handoff = agent.get("force_handoff")
        if isinstance(force_handoff, str):
            path = base + ("force_handoff",)
            if force_handoff == name:
                issues.append(
                    Issue(
                        "error",
                        "semantic",
                        "self-force-handoff",
                        json_path(path),
                        "force_handoff cannot target the same agent.",
                    )
                )
            elif not is_external_reference(force_handoff) and force_handoff not in agent_names:
                add_missing_reference(
                    issues,
                    path=path,
                    kind="agent",
                    value=force_handoff,
                    available=agent_names,
                )

        reference_fields = [
            ("use_toolsets", reusable_toolset_names, "toolset"),
            ("use_commands", command_names, "command-group"),
            ("use_skills", skill_names, "skill-group"),
            ("budgets", budget_names, "budget"),
        ]
        for field_name, available, kind in reference_fields:
            for index, ref in enumerate(list_strings(agent.get(field_name))):
                add_missing_reference(
                    issues,
                    path=base + (field_name, index),
                    kind=kind,
                    value=ref,
                    available=available,
                )

        toolsets = agent.get("toolsets")
        if isinstance(toolsets, Sequence) and not isinstance(toolsets, str):
            for index, toolset in enumerate(toolsets):
                if isinstance(toolset, Mapping):
                    issues.extend(
                        validate_toolset_semantics(
                            toolset,
                            base + ("toolsets", index),
                            model_names=model_names,
                            mcp_names=mcp_names,
                            rag_names=rag_names,
                            toolset_types=toolset_types,
                        )
                    )

    reusable_toolsets = data.get("toolsets")
    if isinstance(reusable_toolsets, Mapping):
        for name, toolset in reusable_toolsets.items():
            if isinstance(toolset, Mapping):
                issues.extend(
                    validate_toolset_semantics(
                        toolset,
                        ("toolsets", str(name)),
                        model_names=model_names,
                        mcp_names=mcp_names,
                        rag_names=rag_names,
                        toolset_types=toolset_types,
                    )
                )

    mcps = data.get("mcps")
    if isinstance(mcps, Mapping):
        for name, definition in mcps.items():
            if not isinstance(definition, Mapping):
                continue
            if not any(key in definition for key in ("ref", "command", "remote")):
                issues.append(
                    Issue(
                        "error",
                        "semantic",
                        "mcp-connection",
                        json_path(("mcps", str(name))),
                        "Reusable MCP definition needs ref, command, or remote.",
                    )
                )

    for cycle in force_handoff_cycles(agents):
        issues.append(
            Issue(
                "error",
                "semantic",
                "force-handoff-cycle",
                "$.agents",
                "force_handoff cycle detected: " + " -> ".join(cycle),
            )
        )

    return issues


def validate_toolset_semantics(
    toolset: Mapping[str, Any],
    path: tuple[Any, ...],
    *,
    model_names: set[str],
    mcp_names: set[str],
    rag_names: set[str],
    toolset_types: set[str],
) -> list[Issue]:
    issues: list[Issue] = []
    tool_type = toolset.get("type")
    if not isinstance(tool_type, str):
        issues.append(
            Issue("error", "semantic", "missing-tool-type", json_path(path), "Toolset needs type.")
        )
        return issues
    if tool_type not in toolset_types:
        issues.append(
            Issue(
                "error",
                "semantic",
                "unknown-tool-type",
                json_path(path + ("type",)),
                f"Unknown toolset type {tool_type!r}.",
            )
        )
        return issues

    requirements: dict[str, tuple[str, ...]] = {
        "lsp": ("command",),
        "api": ("api_config",),
        "webhook": ("webhook_config",),
        "a2a": ("url",),
        "openapi": ("url",),
        "open_url": ("url",),
        "model_picker": ("models",),
        "script": ("shell",),
    }
    if tool_type == "mcp" and not any(key in toolset for key in ("ref", "command", "remote")):
        issues.append(
            Issue(
                "error",
                "semantic",
                "mcp-connection",
                json_path(path),
                "MCP toolset needs ref, command, or remote.",
            )
        )
    if tool_type in {"filesystem", "file"} and not toolset.get("allow_list"):
        issues.append(
            Issue(
                "warning",
                "security",
                "unconstrained-filesystem",
                json_path(path + ("allow_list",)),
                "Filesystem access is not path-confined; add allow_list unless broad host access is intentional.",
            )
        )
    for key in requirements.get(tool_type, ()):
        if key not in toolset or toolset.get(key) in (None, "", [], {}):
            issues.append(
                Issue(
                    "error",
                    "semantic",
                    "missing-tool-field",
                    json_path(path + (key,)),
                    f"Toolset type {tool_type!r} requires {key!r}.",
                )
            )

    check_model_ref(issues, toolset.get("model"), path + ("model",), model_names)
    if tool_type == "model_picker":
        for index, ref in enumerate(list_strings(toolset.get("models"))):
            check_model_ref(issues, ref, path + ("models", index), model_names)

    ref = toolset.get("ref")
    if tool_type == "mcp" and isinstance(ref, str):
        if not ref.startswith("docker:") and not is_external_reference(ref) and ref not in mcp_names:
            add_missing_reference(
                issues,
                path=path + ("ref",),
                kind="MCP",
                value=ref,
                available=mcp_names,
            )
    if tool_type == "rag" and isinstance(ref, str):
        if not is_external_reference(ref) and ref not in rag_names:
            add_missing_reference(
                issues,
                path=path + ("ref",),
                kind="RAG",
                value=ref,
                available=rag_names,
            )
    return issues


def security_issues(data: Any) -> tuple[list[Issue], set[str]]:
    issues: list[Issue] = []
    required_env: set[str] = set()

    for path, value in iter_nodes(data):
        if isinstance(value, str):
            required_env.update(ENV_REF_RE.findall(value))
            for pattern_name, pattern in KNOWN_SECRET_PATTERNS:
                if pattern.search(value):
                    issues.append(
                        Issue(
                            "error",
                            "security",
                            "literal-secret",
                            json_path(path),
                            f"Possible embedded {pattern_name}; replace it with an environment reference.",
                        )
                    )
            if path and str(path[-1]) == "token_key":
                if ENV_NAME_RE.fullmatch(value):
                    required_env.add(value)
                else:
                    issues.append(
                        Issue(
                            "error",
                            "security",
                            "invalid-token-key",
                            json_path(path),
                            "token_key must be an environment variable name, not a token or interpolation.",
                        )
                    )
                continue
            if path and SENSITIVE_KEY_RE.search(str(path[-1])):
                if not SENSITIVE_TEMPLATE_RE.fullmatch(value):
                    issues.append(
                        Issue(
                            "error",
                            "security",
                            "literal-sensitive-value",
                            json_path(path),
                            "Sensitive values must be ${env.NAME} or Bearer ${env.NAME}.",
                        )
                    )

    return issues, required_env


def is_environment_only_failure(output: str) -> bool:
    return bool(ENVIRONMENT_FAILURE_RE.search(output)) and not bool(
        CONFIGURATION_FAILURE_RE.search(output)
    )


def detect_docker_agent(timeout: float) -> Optional[list[str]]:
    docker = shutil.which("docker")
    if docker:
        try:
            result = subprocess.run(
                [docker, "agent", "version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=min(timeout, 15),
                check=False,
            )
            if result.returncode == 0:
                return [docker, "agent"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    direct = shutil.which("docker-agent")
    if direct:
        return [direct]
    return None


def docker_dry_run(
    path: Path,
    *,
    mode: str,
    timeout: float,
    environment_policy: str,
) -> tuple[DockerCheck, list[Issue]]:
    if mode == "off":
        return DockerCheck(status="skipped"), []

    prefix = detect_docker_agent(timeout)
    if prefix is None:
        severity = "error" if mode == "required" else "warning"
        issue = Issue(
            severity,
            "runtime",
            "docker-agent-unavailable",
            "$",
            "Docker Agent CLI was not found; runtime dry-run was skipped.",
        )
        return DockerCheck(status="unavailable"), [issue]

    command = prefix + ["run", path.name, "--dry-run"]
    try:
        result = subprocess.run(
            command,
            cwd=path.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
        return (
            DockerCheck(status="timeout", command=command, output=output[-4000:]),
            [
                Issue(
                    "error",
                    "runtime",
                    "docker-agent-timeout",
                    "$",
                    f"Docker Agent dry-run exceeded {timeout:g} seconds.",
                )
            ],
        )
    except OSError as exc:
        return (
            DockerCheck(status="error", command=command, output=str(exc)),
            [Issue("error", "runtime", "docker-agent-exec", "$", str(exc))],
        )

    output = (result.stdout or "").strip()
    check = DockerCheck(
        status="passed" if result.returncode == 0 else "failed",
        command=command,
        returncode=result.returncode,
        output=output[-4000:] if output else None,
    )
    if result.returncode == 0:
        return check, []

    environment_only = is_environment_only_failure(output)
    if environment_only and environment_policy == "warn":
        return (
            DockerCheck(
                status="environment-blocked",
                command=command,
                returncode=result.returncode,
                output=check.output,
            ),
            [
                Issue(
                    "warning",
                    "runtime",
                    "runtime-environment",
                    "$",
                    "Docker dry-run was blocked by missing credentials or another environment dependency.",
                )
            ],
        )

    return (
        check,
        [
            Issue(
                "error",
                "runtime",
                "docker-agent-dry-run",
                "$",
                "Docker Agent dry-run failed. Inspect the captured runtime output.",
            )
        ],
    )


def issue_sort_key(issue: Issue) -> tuple[int, str, str, str]:
    severity_order = {"error": 0, "warning": 1, "info": 2}
    gate_order = {"yaml": 0, "format": 1, "schema": 2, "semantic": 3, "security": 4, "runtime": 5}
    return (
        severity_order.get(issue.severity, 9),
        f"{gate_order.get(issue.gate, 9):02d}-{issue.gate}",
        issue.path,
        issue.code,
    )


def deduplicate_issues(issues: Iterable[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str, str, str]] = set()
    result: list[Issue] = []
    for issue in issues:
        key = (issue.severity, issue.gate, issue.code, issue.path, issue.message)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return sorted(result, key=issue_sort_key)


def schema_public_info(info: SchemaInfo) -> dict[str, Any]:
    return {
        "source_kind": info.source_kind,
        "source": info.source,
        "sha256": info.sha256,
        "latest_version": info.latest_version,
        "fetched_at": info.fetched_at,
        "official": info.official,
    }


def schema_validation_level(info: SchemaInfo) -> str:
    if info.official:
        return "official"
    if info.source_kind == "explicit":
        return "explicit"
    return "core"


def validate_file(args: argparse.Namespace, schema_info: SchemaInfo) -> ValidationReport:
    path = Path(args.file).expanduser().resolve()
    if not path.exists():
        raise ValidationIncomplete(f"YAML file does not exist: {path}")
    if not path.is_file():
        raise ValidationIncomplete(f"YAML path is not a file: {path}")

    try:
        original_bytes = path.read_bytes()
        original_text = original_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationIncomplete(f"Cannot read YAML file {path}: {exc}") from exc

    issues: list[Issue] = []
    changed = False
    parse_source = original_text.removeprefix("\ufeff") if args.fix else original_text
    parse_text = strip_schema_comment(parse_source) if args.fix else parse_source
    try:
        data = parse_yaml(parse_text, str(path))
    except ValueError as exc:
        issues.append(Issue("error", "yaml", "parse", "$", str(exc)))
        return ValidationReport(
            file=str(path),
            validation_level=schema_validation_level(schema_info),
            schema=schema_public_info(schema_info),
            docker=asdict(DockerCheck(status="not-run")),
            required_environment=[],
            changed=False,
            issues=issues,
        )

    if args.fix:
        normalize_structure(data)
        rendered = dump_yaml(data)
        if rendered != original_text:
            try:
                path.write_text(rendered, encoding="utf-8", newline="\n")
            except OSError as exc:
                raise ValidationIncomplete(f"Cannot write formatted YAML {path}: {exc}") from exc
            changed = True
        current_text = rendered
        try:
            data = parse_yaml(current_text, str(path))
        except ValueError as exc:  # should never happen; protects formatter integrity
            raise ValidationIncomplete(f"Formatter produced invalid YAML: {exc}") from exc
    else:
        current_text = original_text

    issues.extend(format_issues(current_text, data))
    issues.extend(validate_json_schema(data, schema_info))
    issues.extend(
        semantic_issues(
            data,
            latest_version=schema_info.latest_version,
            allow_legacy_version=args.allow_legacy_version,
            config_dir=path.parent,
            toolset_types=schema_toolset_types(schema_info.schema),
        )
    )
    secret_findings, required_env = security_issues(data)
    issues.extend(secret_findings)

    if schema_info.warning:
        issues.append(
            Issue(
                "warning",
                "schema",
                "explicit-schema" if schema_info.source_kind == "explicit" else "schema-cache",
                "$",
                schema_info.warning,
            )
        )
    if schema_info.source_kind == "bundled-core":
        issues.append(
            Issue(
                "warning",
                "schema",
                "core-schema-only",
                "$",
                "Only the bundled diagnostic core schema was used; final official-schema validation is still required.",
            )
        )

    static_errors = [issue for issue in issues if issue.severity == "error"]
    if static_errors:
        docker_check = DockerCheck(status="not-run-static-errors")
    else:
        docker_check, docker_issues = docker_dry_run(
            path,
            mode=args.docker_check,
            timeout=args.docker_timeout,
            environment_policy=args.runtime_env_policy,
        )
        issues.extend(docker_issues)

    return ValidationReport(
        file=str(path),
        validation_level=schema_validation_level(schema_info),
        schema=schema_public_info(schema_info),
        docker=asdict(docker_check),
        required_environment=sorted(required_env),
        changed=changed,
        issues=deduplicate_issues(issues),
    )


def print_human(report: ValidationReport) -> None:
    status = "PASS" if report.passed else "FAIL"
    if report.passed and report.warnings:
        status = "PASS WITH WARNINGS"
    print(f"Docker Agent validation: {status}")
    if report.file:
        print(f"File: {report.file}")
    schema = report.schema
    latest = schema.get("latest_version") or "unknown"
    print(
        "Schema: "
        f"{schema.get('source_kind')} (latest config version {latest}, sha256 {schema.get('sha256', '')[:12]})"
    )
    print(f"Validation level: {report.validation_level}")
    print(f"Formatter changed file: {'yes' if report.changed else 'no'}")
    print(f"Docker dry-run: {report.docker.get('status')}")
    if report.required_environment:
        print("Explicit YAML environment references: " + ", ".join(report.required_environment))
    else:
        print("Explicit YAML environment references: none")
    if report.issues:
        print("\nFindings:")
        for issue in report.issues:
            print(
                f"- {issue.severity.upper()} [{issue.gate}/{issue.code}] "
                f"{issue.path}: {issue.message}"
            )
    output = report.docker.get("output")
    if output and report.docker.get("status") not in {"passed", "skipped", "unavailable"}:
        print("\nDocker dry-run output (tail):")
        print(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Format and validate a Docker Agent configuration."
    )
    parser.add_argument("file", nargs="?", help="Docker Agent file")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Rewrite the file using canonical clean formatting before validation",
    )
    parser.add_argument(
        "--schema-mode",
        choices=("official", "core"),
        default="official",
        help="Use the official schema (required for final validation) or bundled core diagnostic schema",
    )
    parser.add_argument("--schema", type=Path, help="Explicit JSON Schema file")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Official source cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not fetch the official schema; require a cached official copy",
    )
    parser.add_argument(
        "--refresh-schema",
        action="store_true",
        help="Require a fresh official schema download instead of cache fallback",
    )
    parser.add_argument(
        "--schema-timeout",
        type=float,
        default=30.0,
        help="Official schema download timeout in seconds",
    )
    parser.add_argument(
        "--schema-info",
        action="store_true",
        help="Print schema source, digest, and latest config version, then exit",
    )
    parser.add_argument(
        "--allow-legacy-version",
        action="store_true",
        help="Warn instead of fail when the config version is older than the loaded schema's latest version",
    )
    parser.add_argument(
        "--docker-check",
        choices=("auto", "required", "off"),
        default="auto",
        help="Run Docker Agent dry-run automatically, require it, or disable it",
    )
    parser.add_argument(
        "--docker-timeout",
        type=float,
        default=90.0,
        help="Docker Agent dry-run timeout in seconds",
    )
    parser.add_argument(
        "--runtime-env-policy",
        choices=("warn", "error"),
        default="warn",
        help="Treat clearly missing runtime credentials as a warning or error",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON report")
    return parser


def check_dependencies() -> None:
    missing: list[str] = []
    if YAML is None:
        missing.append("ruamel.yaml")
    if validator_for is None:
        missing.append("jsonschema")
    if missing:
        raise ValidationIncomplete(
            "Missing Python package(s): "
            + ", ".join(missing)
            + ". Install scripts/requirements.txt."
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        check_dependencies()
        schema_info = load_schema(
            mode=args.schema_mode,
            cache_dir=args.cache_dir.expanduser(),
            schema_path=args.schema.expanduser() if args.schema else None,
            offline=args.offline,
            refresh=args.refresh_schema,
            timeout=args.schema_timeout,
        )
        if args.schema_info:
            payload = schema_public_info(schema_info)
            if schema_info.warning:
                payload["warning"] = schema_info.warning
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"Source kind: {payload['source_kind']}")
                print(f"Source: {payload['source']}")
                print(f"Official: {'yes' if payload['official'] else 'no'}")
                print(f"Latest config version: {payload['latest_version'] or 'unknown'}")
                print(f"SHA-256: {payload['sha256']}")
                if payload.get("fetched_at"):
                    print(f"Fetched at: {payload['fetched_at']}")
                if payload.get("warning"):
                    print(f"Warning: {payload['warning']}")
            return 0
        if not args.file:
            parser.error("file is required unless --schema-info is used")
        report = validate_file(args, schema_info)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            print_human(report)
        return 0 if report.passed else 1
    except ValidationIncomplete as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "incomplete",
                        "error": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"Docker Agent validation: INCOMPLETE\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
