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

See references/validation-contract.md for the full contract.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _sources as sources  # noqa: E402

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
    from ruamel.yaml.error import MarkedYAMLError
    from ruamel.yaml.events import AliasEvent
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString
    from ruamel.yaml.tokens import ScalarToken
except ImportError:  # pragma: no cover - handled in main
    YAML = None
    CommentedMap = None
    CommentedSeq = None
    DuplicateKeyError = Exception
    MarkedYAMLError = Exception
    AliasEvent = None
    DoubleQuotedScalarString = str
    LiteralScalarString = str
    ScalarToken = None


OFFICIAL_SCHEMA_URL = sources.OFFICIAL_SCHEMA_URL
OFFICIAL_SCHEMA_COMMENT = f"# yaml-language-server: $schema={OFFICIAL_SCHEMA_URL}"
DEFAULT_CACHE_DIR = sources.DEFAULT_CACHE_DIR
DEFAULT_MAX_CACHE_AGE_HOURS = 24.0
BUNDLED_CORE_SCHEMA = SCRIPT_DIR.parent / "references" / "core-schema.json"

# Configuration version used by the bundled documentation snapshot, templates,
# and core schema. The official schema decides the actual latest version.
BUNDLED_SNAPSHOT_VERSION = "16"

MAX_REPORTED_ISSUES = 200
MAX_SCHEMA_ISSUES = 100

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

# Key names that usually hold credentials. The heuristic only applies inside
# configuration subtrees that carry credentials (see SENSITIVE_SUBTREE_KEYS),
# never to free-text prompt fields or command definitions.
SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|(?:access[_-]?)?token|auth(?:orization)?|password|passwd|secret|private[_-]?key|credentials?)$",
    re.IGNORECASE,
)
SENSITIVE_SUBTREE_KEYS = {
    "env",
    "environment",
    "headers",
    "auth",
    "api_config",
    "webhook_config",
    "provider_opts",
    "config",
    "remote",
}
# `${env.NAME}` optionally prefixed by an authentication scheme such as
# `Bearer`, `Token`, or `Basic`.
SENSITIVE_TEMPLATE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9._-]*\s+)?\$\{env\.[A-Za-z_][A-Za-z0-9_]*\}$",
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_TEMPLATE_PREFIX = "${env."
SENSITIVE_FLAG_RE = re.compile(r"^--?([A-Za-z][A-Za-z0-9_-]*)(?:=(.*))?$", re.DOTALL)
KNOWN_SECRET_PATTERNS = [
    (
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    ("openai-style-token", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
]
# `scheme://user:password@host`; the password group is checked separately so
# that `${env.NAME}` interpolations are accepted.
URL_USERINFO_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://([^\s/@:]+):([^\s/@]+)@")

ENVIRONMENT_FAILURE_RE = re.compile(
    r"the following environment variables? must be set"
    r"|\benvironment variables?\b.{0,80}\b(?:missing|not set|unset|required|must be set|not defined)\b"
    r"|\b(?:missing|not set|unset|required|no)\b.{0,80}\benvironment variables?\b"
    r"|\bapi[ _-]?key\b.{0,60}\b(?:missing|not set|unset|is required|must be set|not configured|not found|not provided)\b"
    r"|\b(?:missing|no|unset|invalid)\b.{0,40}\bapi[ _-]?key\b"
    r"|(?-i:\b[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)\b).{0,80}\b(?:missing|not set|unset|is required|must be set|not found|not defined|empty|not provided)\b"
    r"|\b(?:missing|not set|unset|required|set)\b.{0,80}(?-i:\b[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)\b)"
    r"|\bcredentials?\b.{0,60}\b(?:missing|not set|not found|not configured|required|unavailable)\b"
    r"|\bauthentication\b.{0,40}\b(?:required|failed|missing)\b",
    re.IGNORECASE | re.DOTALL,
)
CONFIGURATION_FAILURE_RE = re.compile(
    r"configuration invalid|invalid configuration|unknown field|additional propert(?:y|ies)|"
    r"schema (?:error|validation)|yaml (?:error|parse)|parse error|failed to (?:load|parse) config",
    re.IGNORECASE,
)
# Control characters (other than newline and tab) that a literal block scalar
# cannot represent; strings containing them keep their original style.
LITERAL_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x85\u2028\u2029]")


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
    notice: Optional[str] = None
    cache_trusted: bool = True


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


class AnchorsNotAllowed(ValueError):
    """Raised when the document uses YAML anchors or aliases."""

    def __init__(self, lines: list[int]) -> None:
        super().__init__("YAML anchors and aliases are not allowed")
        self.lines = lines


def load_json_bytes(data: bytes, source: str) -> dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationIncomplete(f"Invalid JSON schema from {source}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationIncomplete(f"Schema from {source} is not a JSON object")
    return parsed


def schema_latest_version(schema: Mapping[str, Any]) -> Optional[str]:
    return sources.schema_latest_version(schema)


def is_official_schema(schema: Mapping[str, Any]) -> bool:
    return sources.is_official_schema(schema)


def schema_toolset_types(schema: Mapping[str, Any]) -> tuple[set[str], str]:
    """Return the toolset type enum and where it came from ("schema" or "bundled")."""
    for container in ("definitions", "$defs"):
        definitions = schema.get(container)
        if not isinstance(definitions, Mapping):
            continue
        for name in ("Toolset", "toolset"):
            try:
                values = definitions[name]["properties"]["type"]["enum"]
            except (KeyError, TypeError):
                continue
            if isinstance(values, list) and values and all(isinstance(value, str) for value in values):
                return set(values), "schema"
    return set(TOOLSET_TYPES), "bundled"


def read_official_cache(cache_dir: Path) -> Optional[tuple[dict[str, Any], bytes, dict[str, Any]]]:
    """Load the cached official schema; None when absent, ValidationIncomplete when corrupt."""
    cache_schema = cache_dir / sources.SCHEMA_FILENAME
    cache_meta = cache_dir / sources.SCHEMA_META_FILENAME
    if not cache_schema.exists():
        return None
    try:
        data = cache_schema.read_bytes()
    except OSError as exc:
        raise ValidationIncomplete(f"Cannot read cached schema: {exc}") from exc
    schema = load_json_bytes(data, str(cache_schema))
    if not is_official_schema(schema):
        raise ValidationIncomplete(
            f"Cached file is not an official Docker Agent schema: {cache_schema}"
        )
    if not cache_meta.exists():
        raise ValidationIncomplete(f"Cached official schema metadata is missing: {cache_meta}")
    try:
        metadata = sources.load_metadata(cache_meta)
    except (OSError, ValueError) as exc:
        raise ValidationIncomplete(
            f"Cannot read cached official schema metadata: {exc}"
        ) from exc
    if metadata.get("source") != OFFICIAL_SCHEMA_URL:
        raise ValidationIncomplete(
            "Cached schema metadata does not name the official Docker Agent schema URL"
        )
    if metadata.get("sha256") != sources.sha256_bytes(data):
        raise ValidationIncomplete(
            f"Cached official schema digest does not match {sources.SCHEMA_META_FILENAME}; "
            "the cache is corrupt or was edited. Refresh it with scripts/refresh_official_sources.py."
        )
    return schema, data, metadata


def load_schema(
    *,
    mode: str,
    cache_dir: Path,
    schema_path: Optional[Path],
    offline: bool,
    refresh: bool,
    timeout: float,
    max_age_hours: float = DEFAULT_MAX_CACHE_AGE_HOURS,
    cache_trusted: bool = True,
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
            sha256=sources.sha256_bytes(data),
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
            sha256=sources.sha256_bytes(data),
            latest_version=schema_latest_version(schema),
            fetched_at=None,
            official=False,
            schema=schema,
        )

    cache_schema = cache_dir / sources.SCHEMA_FILENAME
    cache_meta = cache_dir / sources.SCHEMA_META_FILENAME

    cached: Optional[tuple[dict[str, Any], bytes, dict[str, Any]]] = None
    cache_error: Optional[str] = None
    try:
        cached = read_official_cache(cache_dir)
    except ValidationIncomplete as exc:
        cache_error = str(exc)

    def cached_info(warning: Optional[str]) -> SchemaInfo:
        assert cached is not None
        schema, data, metadata = cached
        return SchemaInfo(
            source_kind="official-cache",
            source=str(cache_schema),
            sha256=sources.sha256_bytes(data),
            latest_version=schema_latest_version(schema),
            fetched_at=metadata.get("fetched_at"),
            official=True,
            schema=schema,
            warning=warning,
            cache_trusted=cache_trusted,
        )

    if cached is not None and not offline and not refresh:
        age = sources.cache_age_hours(cached[2])
        if age is not None and 0 <= age <= max_age_hours:
            return cached_info(None)

    fetch_error: Optional[str] = None
    if not offline:
        try:
            data = sources.fetch_bytes(OFFICIAL_SCHEMA_URL, timeout)
            schema = load_json_bytes(data, OFFICIAL_SCHEMA_URL)
            if not is_official_schema(schema):
                raise ValidationIncomplete(
                    "Downloaded schema did not identify itself as Docker Agent Configuration"
                )
        except (OSError, urllib.error.URLError, ValidationIncomplete) as exc:
            fetch_error = str(exc)
            if refresh:
                raise ValidationIncomplete(
                    f"Could not refresh the official Docker Agent schema: {exc}"
                ) from exc
        else:
            metadata = sources.source_metadata(
                data,
                OFFICIAL_SCHEMA_URL,
                extra={"latest_version": schema_latest_version(schema)},
            )
            previous = cached[2] if cached is not None else sources.read_cached_metadata(cache_meta)
            notice = sources.digest_change_notice(previous, metadata, label="official Docker Agent schema")
            warning: Optional[str] = None
            try:
                sources.atomic_write(cache_schema, data)
                sources.atomic_write(cache_meta, sources.encode_metadata(metadata))
            except OSError as exc:
                warning = (
                    f"Downloaded the official schema but could not update the cache in {cache_dir}: {exc}. "
                    "Validation continues with the downloaded copy."
                )
            return SchemaInfo(
                source_kind="official-live",
                source=OFFICIAL_SCHEMA_URL,
                sha256=metadata["sha256"],
                latest_version=metadata["latest_version"],
                fetched_at=metadata["fetched_at"],
                official=True,
                schema=schema,
                warning=warning,
                notice=notice,
            )

    if cached is not None:
        if fetch_error:
            warning = (
                "Live schema refresh failed; using the cached official schema. "
                f"Refresh error: {fetch_error}"
            )
        else:
            warning = "Offline mode: using the cached official schema."
        return cached_info(warning)

    if cache_error:
        detail = f" Live download also failed: {fetch_error}." if fetch_error else ""
        raise ValidationIncomplete(cache_error + detail)

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
    for line in text.split("\n"):
        if in_preamble and SCHEMA_COMMENT_RE.match(line):
            continue
        if in_preamble and line.strip() and not line.lstrip().startswith("#"):
            in_preamble = False
        lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def anchor_lines(parser: Any, text: str) -> list[int]:
    """Return 1-based line numbers of anchors and aliases in ``text``."""
    lines: list[int] = []
    for event in parser.parse(text):
        if (AliasEvent is not None and isinstance(event, AliasEvent)) or getattr(event, "anchor", None):
            lines.append(event.start_mark.line + 1)
    return sorted(set(lines))


def describe_duplicate_key(exc: Any) -> str:
    problem = str(getattr(exc, "problem", "") or "")
    match = re.search(r'found duplicate key "(.+)" with value', problem, re.DOTALL)
    key_text = f' "{match.group(1)}"' if match else ""
    mark = getattr(exc, "problem_mark", None)
    where = ""
    if mark is not None:
        where = f" at line {mark.line + 1}, column {mark.column + 1}"
    context_mark = getattr(exc, "context_mark", None)
    if context_mark is not None:
        where += f" (mapping starts at line {context_mark.line + 1})"
    return f"Duplicate YAML key{key_text}{where}."


def describe_yaml_error(exc: Any, source: str) -> str:
    """Describe a YAML error without echoing the source snippet ruamel attaches."""
    if isinstance(exc, MarkedYAMLError):
        parts = [str(part) for part in (getattr(exc, "context", None), getattr(exc, "problem", None)) if part]
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        detail = "; ".join(parts) or type(exc).__name__
        return f"YAML parse error in {source}: {detail}{where}"
    return f"YAML parse error in {source}: {type(exc).__name__}: {exc}"


def parse_yaml(text: str, source: str) -> Any:
    parser = yaml_parser()
    try:
        anchors = anchor_lines(parser, text)
        if anchors:
            raise AnchorsNotAllowed(anchors)
        documents = list(parser.load_all(text))
    except AnchorsNotAllowed:
        raise
    except DuplicateKeyError as exc:
        raise ValueError(describe_duplicate_key(exc)) from exc
    except Exception as exc:
        raise ValueError(describe_yaml_error(exc, source)) from exc
    if len(documents) != 1:
        raise ValueError(
            f"Docker Agent config must contain exactly one YAML document; found {len(documents)}"
        )
    data = documents[0]
    if not isinstance(data, Mapping):
        raise ValueError("Docker Agent config root must be a YAML mapping")
    return data


def to_plain(value: Any, _memo: Optional[dict[int, Any]] = None) -> Any:
    memo = _memo if _memo is not None else {}
    if isinstance(value, Mapping):
        if id(value) in memo:
            return memo[id(value)]
        result: dict[str, Any] = {}
        memo[id(value)] = result
        for key, item in value.items():
            result[str(key)] = to_plain(item, memo)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if id(value) in memo:
            return memo[id(value)]
        items: list[Any] = []
        memo[id(value)] = items
        items.extend(to_plain(item, memo) for item in value)
        return items
    return value


def set_block_style(value: Any, _seen: Optional[set[int]] = None) -> None:
    seen = _seen if _seen is not None else set()
    if id(value) in seen:
        return
    if CommentedMap is not None and isinstance(value, CommentedMap):
        seen.add(id(value))
        value.fa.set_block_style()
        for item in value.values():
            set_block_style(item, seen)
    elif CommentedSeq is not None and isinstance(value, CommentedSeq):
        seen.add(id(value))
        value.fa.set_block_style()
        for item in value:
            set_block_style(item, seen)


def literal_block_round_trips(value: str) -> bool:
    """Return True when ``value`` survives a literal block scalar round trip."""
    if LITERAL_UNSAFE_RE.search(value):
        return False
    parser = yaml_parser()
    parser.version = None
    stream = StringIO()
    try:
        parser.dump({"value": LiteralScalarString(value)}, stream)
        parsed = yaml_parser().load(stream.getvalue())
    except Exception:
        return False
    return isinstance(parsed, Mapping) and str(parsed.get("value")) == value


def normalize_structure(data: Any) -> None:
    """Apply canonical scalar styles. Key ordering happens on the rendered text."""
    set_block_style(data)

    version = data.get("version")
    if version is not None and not isinstance(version, str):
        data["version"] = DoubleQuotedScalarString(str(version))
    elif isinstance(version, str) and not isinstance(version, DoubleQuotedScalarString):
        data["version"] = DoubleQuotedScalarString(str(version))

    agents = data.get("agents")
    if isinstance(agents, Mapping):
        for agent in agents.values():
            if not isinstance(agent, Mapping):
                continue
            instruction = agent.get("instruction")
            if isinstance(instruction, str) and not isinstance(instruction, LiteralScalarString):
                text = str(instruction)
                if ("\n" in text or len(text) > 80) and literal_block_round_trips(text):
                    # LiteralScalarString preserves the scalar value. Do not append a
                    # newline merely to obtain the visual `|` chomping style.
                    agent["instruction"] = LiteralScalarString(text)


def expected_plain_after_fix(data: Any) -> Any:
    """The plain value the formatter is allowed to produce for ``data``."""
    expected = to_plain(data)
    if isinstance(expected, dict) and expected.get("version") is not None:
        expected["version"] = str(expected["version"])
    return expected


def dump_yaml(data: Any) -> str:
    parser = yaml_parser()
    # Keep YAML 1.2 parsing semantics without emitting a document directive/header.
    parser.version = None
    parser.explicit_start = False
    stream = StringIO()
    parser.dump(data, stream)
    body = strip_schema_comment(stream.getvalue())
    body_lines = body.split("\n")
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    rendered = OFFICIAL_SCHEMA_COMMENT + "\n"
    if body_lines:
        rendered += "\n" + "\n".join(body_lines) + "\n"
    return rendered


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_comment_line(line: str) -> bool:
    return line.lstrip(" ").startswith("#")


def _block_end(lines: list[str], last_key_line: int, key_col: int, content_lines: set[int]) -> int:
    for index in range(last_key_line + 1, len(lines)):
        line = lines[index]
        if index in content_lines or not line.strip() or _is_comment_line(line):
            continue
        if _line_indent(line) < key_col:
            return index
    return len(lines)


def reorder_rendered_mapping(
    lines: list[str],
    mapping: Any,
    order: list[str],
    *,
    content_lines: set[int],
    separate_sections: bool = False,
) -> bool:
    """Reorder the text lines of ``mapping`` (parsed from ``lines``) in place.

    Comments directly above a key travel with that key (a preamble above the
    first key stays where it is); blank lines and dedented comments at the end
    of the block stay at the end. ``content_lines`` are scalar body lines that
    must never be mistaken for comments. The block keeps its line count unless
    ``separate_sections`` normalises blank lines between entries. Returns True
    when the text changed.
    """
    if not isinstance(mapping, Mapping) or len(mapping) < 2:
        return False
    if relative_order_is_canonical(mapping, order):
        return False
    lc = getattr(mapping, "lc", None)
    if lc is None or not getattr(lc, "data", None):
        return False
    keys = list(mapping.keys())
    positions: list[int] = []
    key_col: Optional[int] = None
    for key in keys:
        entry = lc.data.get(key)
        if entry is None:
            return False
        positions.append(int(entry[0]))
        if key_col is None:
            key_col = int(entry[1])
        elif int(entry[1]) != key_col:
            return False
    assert key_col is not None
    if positions != sorted(positions) or positions[-1] >= len(lines):
        return False

    def is_comment(index: int) -> bool:
        return index not in content_lines and _is_comment_line(lines[index]) and bool(lines[index].strip())

    def is_blank(index: int) -> bool:
        return index not in content_lines and not lines[index].strip()

    first_line = positions[0]
    end = _block_end(lines, positions[-1], key_col, content_lines)
    starts: list[int] = []
    for index, line_no in enumerate(positions):
        start = line_no
        if index > 0:
            floor = positions[index - 1] + 1
            while start - 1 >= floor and is_comment(start - 1):
                start -= 1
        starts.append(start)

    segments: list[tuple[int, str, int, list[str]]] = []
    for index, key in enumerate(keys):
        seg_end = starts[index + 1] if index + 1 < len(keys) else end
        segments.append((index, str(key), positions[index] - starts[index], lines[starts[index]:seg_end]))

    tail: list[str] = []
    last_lines = segments[-1][3]
    last_key_offset = segments[-1][2]
    last_start = starts[-1]
    while len(last_lines) > last_key_offset + 1:
        candidate_index = last_start + len(last_lines) - 1
        if is_blank(candidate_index) or (
            is_comment(candidate_index) and _line_indent(lines[candidate_index]) < key_col
        ):
            tail.insert(0, last_lines.pop())
        else:
            break

    prefix = lines[first_line][:key_col]
    has_prefix = bool(prefix.strip())
    if has_prefix:
        _, _, offset, seg_lines = segments[0]
        seg_lines[offset] = " " * key_col + seg_lines[offset][key_col:]

    rank = {key: index for index, key in enumerate(order)}

    def sort_key(segment: tuple[int, str, int, list[str]]) -> tuple[int, int]:
        original_index, key, _, _ = segment
        if key in rank:
            return (rank[key], original_index)
        return (len(order) + original_index, original_index)

    ordered = sorted(segments, key=sort_key)
    if has_prefix:
        _, _, offset, seg_lines = ordered[0]
        seg_lines[offset] = prefix + seg_lines[offset][key_col:]

    new_lines: list[str] = []
    if separate_sections:
        for index, (_, _, _, seg_lines) in enumerate(ordered):
            body = list(seg_lines)
            while body and not body[-1].strip():
                body.pop()
            if index > 0:
                new_lines.append("")
            new_lines.extend(body)
    else:
        for _, _, _, seg_lines in ordered:
            new_lines.extend(seg_lines)
    new_lines.extend(tail)
    lines[first_line:end] = new_lines
    return True


def reorderable_blocks(data: Mapping[str, Any]) -> list[tuple[Any, list[str], bool]]:
    """Mappings subject to canonical ordering, innermost first, top level last."""
    blocks: list[tuple[Any, list[str], bool]] = []

    def mapping_values(section: str) -> list[Any]:
        value = data.get(section)
        if isinstance(value, Mapping):
            return [item for item in value.values() if isinstance(item, Mapping)]
        return []

    for agent in mapping_values("agents"):
        toolsets = agent.get("toolsets")
        if isinstance(toolsets, Sequence) and not isinstance(toolsets, str):
            blocks.extend((toolset, TOOLSET_ORDER, False) for toolset in toolsets if isinstance(toolset, Mapping))
    blocks.extend((agent, AGENT_ORDER, False) for agent in mapping_values("agents"))
    for section, order in (
        ("models", MODEL_ORDER),
        ("providers", MODEL_ORDER),
        ("mcps", TOOLSET_ORDER),
        ("toolsets", TOOLSET_ORDER),
    ):
        blocks.extend((entry, order, False) for entry in mapping_values(section))
    blocks.append((data, TOP_LEVEL_ORDER, True))
    return blocks


def reorder_rendered(rendered: str, source: str) -> str:
    """Reorder sections of formatted YAML text into the canonical key order.

    One block is reordered per pass and the text is re-parsed afterwards, so
    every pass works with fresh line positions.
    """
    for _ in range(500):
        data = parse_yaml(rendered, source)
        lines = rendered.split("\n")
        content_lines = scalar_content_lines(scalar_spans(rendered))
        changed = False
        for mapping, order, separate in reorderable_blocks(data):
            if reorder_rendered_mapping(
                lines,
                mapping,
                order,
                content_lines=content_lines,
                separate_sections=separate,
            ):
                changed = True
                break
        if not changed:
            return rendered
        rendered = "\n".join(lines)
    raise ValueError("canonical reordering did not converge")


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


def iter_nodes(
    value: Any,
    path: tuple[Any, ...] = (),
    _seen: Optional[set[int]] = None,
) -> Iterator[tuple[tuple[Any, ...], Any]]:
    seen = _seen if _seen is not None else set()
    yield path, value
    if isinstance(value, Mapping):
        if id(value) in seen:
            return
        seen.add(id(value))
        for key, item in value.items():
            yield from iter_nodes(item, path + (str(key),), seen)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if id(value) in seen:
            return
        seen.add(id(value))
        for index, item in enumerate(value):
            yield from iter_nodes(item, path + (index,), seen)


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


@dataclass
class ScalarSpan:
    style: Optional[str]
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    @property
    def is_block(self) -> bool:
        return self.style in ("|", ">")

    def continuation_lines(self) -> range:
        """Zero-based lines after the first line that belong to this scalar."""
        last = self.end_line + (1 if self.end_col > 0 else 0)
        return range(self.start_line + 1, last)

    def contains(self, line: int, col: int) -> bool:
        if line < self.start_line or line > self.end_line:
            return False
        if line == self.start_line and col < self.start_col:
            return False
        if line == self.end_line and col >= self.end_col:
            return False
        return True


def scalar_spans(text: str) -> list[ScalarSpan]:
    """Positions of every scalar token; empty when the text cannot be scanned."""
    if ScalarToken is None:
        return []
    spans: list[ScalarSpan] = []
    try:
        for token in yaml_parser().scan(text):
            if isinstance(token, ScalarToken):
                spans.append(
                    ScalarSpan(
                        token.style,
                        token.start_mark.line,
                        token.start_mark.column,
                        token.end_mark.line,
                        token.end_mark.column,
                    )
                )
    except Exception:
        return []
    return spans


def scalar_content_lines(spans: Iterable[ScalarSpan]) -> set[int]:
    """Zero-based lines that are entirely scalar content (block bodies, continuations)."""
    lines: set[int] = set()
    for span in spans:
        lines.update(span.continuation_lines())
    return lines


def indentation_jump_lines(text: str, content_lines: set[int]) -> list[int]:
    issues: list[int] = []
    previous_indent = 0
    for index, line in enumerate(text.split("\n")):
        if index in content_lines or not line.strip():
            continue
        if _is_comment_line(line):
            continue
        indent = _line_indent(line)
        if indent > previous_indent + 2:
            issues.append(index + 1)
        previous_indent = indent
    return issues


def tab_lines(text: str, spans: list[ScalarSpan], content_lines: set[int]) -> list[int]:
    """1-based lines with a tab outside scalar content."""
    result: list[int] = []
    for index, line in enumerate(text.split("\n")):
        if "\t" not in line or index in content_lines:
            continue
        for col, char in enumerate(line):
            if char == "\t" and not any(span.contains(index, col) for span in spans):
                result.append(index + 1)
                break
    return result


def format_issues(text: str, data: Any) -> list[Issue]:
    issues: list[Issue] = []
    lines = text.split("\n")
    spans = scalar_spans(text)
    content_lines = scalar_content_lines(spans)

    def sample(numbers: list[int]) -> str:
        return ", ".join(str(number) for number in numbers[:8])

    if text.startswith("\ufeff"):
        issues.append(Issue("error", "format", "utf8-bom", "$", "Remove the UTF-8 BOM."))
    if "\r" in text:
        issues.append(Issue("error", "format", "line-endings", "$", "Use LF line endings."))
    tabs = tab_lines(text, spans, content_lines)
    if tabs:
        issues.append(
            Issue(
                "error",
                "format",
                "tabs",
                "$",
                f"Tabs are not allowed outside scalar content; found on line(s): {sample(tabs)}.",
            )
        )
    indentation = indentation_jump_lines(text, content_lines)
    if indentation:
        issues.append(
            Issue(
                "error",
                "format",
                "indentation",
                "$",
                f"Use two-space indentation; invalid indentation jump on line(s): {sample(indentation)}.",
            )
        )
    trailing = [
        index + 1
        for index, line in enumerate(lines)
        if index not in content_lines and line.rstrip(" \t") != line
    ]
    if trailing:
        issues.append(
            Issue(
                "error",
                "format",
                "trailing-whitespace",
                "$",
                f"Remove trailing whitespace on line(s): {sample(trailing)}.",
            )
        )
    if text and not text.endswith("\n"):
        issues.append(Issue("error", "format", "final-newline", "$", "Add one final newline."))
    first_nonempty = next((line for line in lines if line.strip()), "")
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


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence):
        return "array"
    return type(value).__name__


def _short_list(values: Any, limit: int = 20) -> str:
    if not isinstance(values, (list, tuple)):
        return repr(values)
    rendered = [repr(value) for value in values[:limit]]
    if len(values) > limit:
        rendered.append(f"... ({len(values) - limit} more)")
    return ", ".join(rendered)


def describe_schema_error(error: Any) -> str:
    """Build a schema error message without echoing the offending value."""
    keyword = str(error.validator or "validation")
    expected = error.validator_value
    instance = error.instance
    found = json_type_name(instance)
    if keyword == "type":
        wanted = expected if isinstance(expected, str) else " or ".join(str(item) for item in expected)
        return f"Expected {wanted}, found {found}."
    if keyword == "enum":
        return f"Value must be one of: {_short_list(expected)}."
    if keyword == "const":
        return f"Value must equal {expected!r}."
    if keyword == "required":
        missing = [
            str(name)
            for name in (expected if isinstance(expected, list) else [])
            if not (isinstance(instance, Mapping) and name in instance)
        ]
        return "Missing required " + ("property: " if len(missing) == 1 else "properties: ") + ", ".join(missing) + "."
    if keyword == "additionalProperties":
        if isinstance(instance, Mapping):
            schema = error.schema if isinstance(error.schema, Mapping) else {}
            known = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
            patterns = schema.get("patternProperties") if isinstance(schema.get("patternProperties"), Mapping) else {}
            extras = [
                str(key)
                for key in instance
                if key not in known and not any(re.search(pattern, str(key)) for pattern in patterns)
            ]
            if extras:
                return "Unknown " + ("property: " if len(extras) == 1 else "properties: ") + ", ".join(sorted(extras)) + "."
        return "Additional properties are not allowed."
    if keyword in ("minLength", "maxLength"):
        bound = "least" if keyword == "minLength" else "most"
        return f"String length must be at {bound} {expected} character(s)."
    if keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        operator = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}[keyword]
        return f"Value must be {operator} {expected}."
    if keyword == "multipleOf":
        return f"Value must be a multiple of {expected}."
    if keyword == "pattern":
        return f"String does not match the pattern {expected!r}."
    if keyword == "format":
        return f"String is not a valid {expected}."
    if keyword in ("minItems", "maxItems"):
        bound = "least" if keyword == "minItems" else "most"
        return f"Array must contain at {bound} {expected} item(s)."
    if keyword in ("minProperties", "maxProperties"):
        bound = "least" if keyword == "minProperties" else "most"
        return f"Object must contain at {bound} {expected} propert(y/ies)."
    if keyword == "uniqueItems":
        return "Array items must be unique."
    if keyword == "oneOf":
        return f"Value ({found}) must match exactly one of the allowed alternatives."
    if keyword == "anyOf":
        return f"Value ({found}) does not match any of the allowed alternatives."
    if keyword == "not":
        return f"Value ({found}) matches a disallowed shape."
    if keyword in ("dependencies", "dependentRequired"):
        return "Property dependencies are not satisfied."
    return f"Value ({found}) violates the {keyword!r} constraint."


def uri_format_available() -> bool:
    if FormatChecker is None:
        return False
    try:
        return "uri" in FormatChecker().checkers
    except Exception:
        return False


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

    issues: list[Issue] = []
    if not uri_format_available():
        issues.append(
            Issue(
                "warning",
                "schema",
                "uri-format-unavailable",
                "$",
                "URI format checks were skipped: install jsonschema[format-nongpl] to enable them.",
            )
        )

    raw_errors = sorted(
        validator.iter_errors(to_plain(data)),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), str(item.validator)),
    )
    seen: set[tuple[str, str]] = set()
    schema_issue_count = 0
    for top_error in raw_errors:
        candidates = list(leaf_schema_errors(top_error)) or [top_error]
        # Limit noisy oneOf/anyOf expansion while keeping actionable leaf errors.
        for error in candidates[:12]:
            path = json_path(error.absolute_path)
            message = describe_schema_error(error)
            key = (path, message)
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                Issue(
                    "error",
                    "schema",
                    f"jsonschema-{error.validator or 'validation'}",
                    path,
                    message,
                )
            )
            schema_issue_count += 1
            if schema_issue_count >= MAX_SCHEMA_ISSUES:
                issues.append(
                    Issue(
                        "error",
                        "schema",
                        "too-many-errors",
                        "$",
                        f"Schema validation produced more than {MAX_SCHEMA_ISSUES} errors; fix earlier errors first.",
                    )
                )
                return issues
    return issues


def is_external_reference(value: str) -> bool:
    """URLs, OCI references, `docker:` catalog items, and paths are external."""
    return "/" in value or ":" in value


def is_inline_model(value: str) -> bool:
    return "/" in value or value in SPECIAL_MODEL_REFS


def list_strings(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def mapping_keys(data: Mapping[str, Any], section: str) -> set[str]:
    value = data.get(section)
    if isinstance(value, Mapping):
        return {str(name) for name in value.keys()}
    return set()


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
    if allow_bare_catalog_model:
        # The primary AgentConfig.model field explicitly permits a bare model name
        # such as `gpt-4` or `claude`; the runtime may resolve it through its model
        # catalogue/default provider. When the file defines named models, a bare
        # name that is not one of them is most likely a typo, so warn.
        if model_names:
            closest = difflib.get_close_matches(value, sorted(model_names), n=1, cutoff=0.6)
            hint = f" Did you mean {closest[0]!r}?" if closest else ""
            issues.append(
                Issue(
                    "warning",
                    "semantic",
                    "unresolved-model-name",
                    json_path(path),
                    f"Bare model name {value!r} is not defined under models.{hint} "
                    "Docker Agent may still resolve it through its model catalogue.",
                )
            )
        return
    # Other model-reference fields are documented as named entries or inline
    # provider/model specs and should resolve statically.
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


@dataclass
class ReferenceSets:
    agents: set[str]
    models: set[str]
    providers: set[str]
    mcps: set[str]
    rag: set[str]
    commands: set[str]
    skills: set[str]
    toolsets: set[str]
    budgets: set[str]

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "ReferenceSets":
        return cls(
            agents=mapping_keys(data, "agents"),
            models=mapping_keys(data, "models"),
            providers=mapping_keys(data, "providers"),
            mcps=mapping_keys(data, "mcps"),
            rag=mapping_keys(data, "rag"),
            commands=mapping_keys(data, "commands"),
            skills=mapping_keys(data, "skills"),
            toolsets=mapping_keys(data, "toolsets"),
            budgets=mapping_keys(data, "budgets"),
        )


def version_issues(
    data: Mapping[str, Any],
    *,
    latest_version: Optional[str],
    allow_legacy_version: bool,
) -> list[Issue]:
    issues: list[Issue] = []
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
    return issues


def model_section_issues(data: Mapping[str, Any], refs: ReferenceSets) -> list[Issue]:
    issues: list[Issue] = []
    models = data.get("models")
    if isinstance(models, Mapping):
        for model_name, model in models.items():
            if not isinstance(model, Mapping):
                continue
            provider = model.get("provider")
            if isinstance(provider, str) and provider not in BUILTIN_PROVIDERS and provider not in refs.providers:
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
                    refs.models,
                )
            for index, ref in enumerate(list_strings(model.get("first_available"))):
                check_model_ref(
                    issues,
                    ref,
                    ("models", str(model_name), "first_available", index),
                    refs.models,
                )
            fallback = model.get("fallback")
            if isinstance(fallback, Mapping):
                for index, ref in enumerate(list_strings(fallback.get("models"))):
                    check_model_ref(
                        issues,
                        ref,
                        ("models", str(model_name), "fallback", "models", index),
                        refs.models,
                    )
            routing = model.get("routing")
            if isinstance(routing, Sequence) and not isinstance(routing, str):
                for index, rule in enumerate(routing):
                    if isinstance(rule, Mapping):
                        check_model_ref(
                            issues,
                            rule.get("model"),
                            ("models", str(model_name), "routing", index, "model"),
                            refs.models,
                        )

    providers = data.get("providers")
    if isinstance(providers, Mapping):
        for provider_name, provider in providers.items():
            if isinstance(provider, Mapping):
                check_model_ref(
                    issues,
                    provider.get("compaction_model"),
                    ("providers", str(provider_name), "compaction_model"),
                    refs.models,
                )
    return issues


def agent_issues(
    name: str,
    agent: Mapping[str, Any],
    refs: ReferenceSets,
    *,
    config_dir: Path,
    toolset_types: set[str],
) -> list[Issue]:
    issues: list[Issue] = []
    base = ("agents", name)
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
        refs.models,
        allow_bare_catalog_model=True,
    )
    check_model_ref(
        issues,
        agent.get("compaction_model"),
        base + ("compaction_model",),
        refs.models,
    )
    fallback = agent.get("fallback")
    if isinstance(fallback, Mapping):
        for index, ref in enumerate(list_strings(fallback.get("models"))):
            check_model_ref(
                issues,
                ref,
                base + ("fallback", "models", index),
                refs.models,
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
        agent_refs = agent.get(field_name)
        if not isinstance(agent_refs, Sequence) or isinstance(agent_refs, str):
            continue
        seen_refs: set[str] = set()
        for index, ref in enumerate(agent_refs):
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
            elif not is_external_reference(ref) and ref not in refs.agents:
                add_missing_reference(
                    issues,
                    path=path,
                    kind="agent",
                    value=ref,
                    available=refs.agents,
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
        elif not is_external_reference(force_handoff) and force_handoff not in refs.agents:
            add_missing_reference(
                issues,
                path=path,
                kind="agent",
                value=force_handoff,
                available=refs.agents,
            )

    reference_fields = [
        ("use_toolsets", refs.toolsets, "toolset"),
        ("use_commands", refs.commands, "command-group"),
        ("use_skills", refs.skills, "skill-group"),
        ("budgets", refs.budgets, "budget"),
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
                        model_names=refs.models,
                        mcp_names=refs.mcps,
                        rag_names=refs.rag,
                        toolset_types=toolset_types,
                    )
                )
    return issues


def semantic_issues(
    data: Mapping[str, Any],
    *,
    latest_version: Optional[str],
    allow_legacy_version: bool,
    config_dir: Path,
    toolset_types: set[str],
) -> list[Issue]:
    issues = version_issues(
        data,
        latest_version=latest_version,
        allow_legacy_version=allow_legacy_version,
    )

    agents = data.get("agents")
    if not isinstance(agents, Mapping) or not agents:
        issues.append(
            Issue("error", "semantic", "missing-agents", "$.agents", "Define at least one agent.")
        )
        return issues

    refs = ReferenceSets.from_data(data)
    if "root" not in refs.agents:
        issues.append(
            Issue(
                "warning",
                "semantic",
                "no-root-agent",
                "$.agents",
                "No agent named 'root' exists; ensure invocation explicitly selects the intended entry agent.",
            )
        )

    issues.extend(model_section_issues(data, refs))

    for agent_name, agent in agents.items():
        name = str(agent_name)
        if not isinstance(agent, Mapping):
            issues.append(
                Issue("error", "semantic", "agent-type", json_path(("agents", name)), "Agent must be a mapping.")
            )
            continue
        issues.extend(
            agent_issues(name, agent, refs, config_dir=config_dir, toolset_types=toolset_types)
        )

    reusable_toolsets = data.get("toolsets")
    if isinstance(reusable_toolsets, Mapping):
        for name, toolset in reusable_toolsets.items():
            if isinstance(toolset, Mapping):
                issues.extend(
                    validate_toolset_semantics(
                        toolset,
                        ("toolsets", str(name)),
                        model_names=refs.models,
                        mcp_names=refs.mcps,
                        rag_names=refs.rag,
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
        if not is_external_reference(ref) and ref not in mcp_names:
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


def nearest_key(path: tuple[Any, ...]) -> Optional[str]:
    for part in reversed(path):
        if isinstance(part, str):
            return part
    return None


def in_sensitive_subtree(path: tuple[Any, ...]) -> bool:
    return any(isinstance(part, str) and part in SENSITIVE_SUBTREE_KEYS for part in path[:-1])


def is_env_template(value: str) -> bool:
    return bool(SENSITIVE_TEMPLATE_RE.fullmatch(value))


def literal_secret_findings(path: tuple[Any, ...], value: str) -> list[Issue]:
    findings: list[Issue] = []
    for pattern_name, pattern in KNOWN_SECRET_PATTERNS:
        if pattern.search(value):
            findings.append(
                Issue(
                    "error",
                    "security",
                    "literal-secret",
                    json_path(path),
                    f"Possible embedded {pattern_name}; replace it with an environment reference.",
                )
            )
    for match in URL_USERINFO_RE.finditer(value):
        password = match.group(2)
        if not password.startswith(ENV_TEMPLATE_PREFIX):
            findings.append(
                Issue(
                    "error",
                    "security",
                    "literal-secret",
                    json_path(path),
                    "Possible embedded url-password (credentials in a URL); replace it with an environment reference.",
                )
            )
            break
    return findings


def argument_secret_findings(path: tuple[Any, ...], args: Sequence[Any]) -> list[Issue]:
    """Flag `--token=value` and `--token value` pairs with sensitive flag names."""
    findings: list[Issue] = []
    items = list(args)
    for index, item in enumerate(items):
        if not isinstance(item, str):
            continue
        match = SENSITIVE_FLAG_RE.match(item)
        if not match or not SENSITIVE_KEY_RE.search(match.group(1)):
            continue
        inline_value = match.group(2)
        if inline_value is not None:
            if inline_value and not is_env_template(inline_value):
                findings.append(_argument_issue(path + (index,), match.group(1)))
            continue
        if index + 1 < len(items) and isinstance(items[index + 1], str):
            following = items[index + 1]
            if following and not following.startswith("-") and not is_env_template(following):
                findings.append(_argument_issue(path + (index + 1,), match.group(1)))
    return findings


def _argument_issue(path: tuple[Any, ...], flag: str) -> Issue:
    return Issue(
        "error",
        "security",
        "literal-sensitive-argument",
        json_path(path),
        f"Command-line flag {flag!r} carries a literal value; pass ${{env.NAME}} instead.",
    )


def security_issues(data: Any) -> tuple[list[Issue], set[str]]:
    issues: list[Issue] = []
    required_env: set[str] = set()

    for path, value in iter_nodes(data):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if nearest_key(path) == "args":
                issues.extend(argument_secret_findings(path, value))
            continue
        if not isinstance(value, str):
            continue
        required_env.update(ENV_REF_RE.findall(value))
        issues.extend(literal_secret_findings(path, value))
        key = nearest_key(path)
        if key is None:
            continue
        if key == "token_key" and path and path[-1] == "token_key":
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
        if SENSITIVE_KEY_RE.search(key) and in_sensitive_subtree(path):
            if not is_env_template(value):
                issues.append(
                    Issue(
                        "error",
                        "security",
                        "literal-sensitive-value",
                        json_path(path),
                        "Sensitive values must be ${env.NAME}, optionally prefixed by an auth scheme such as Bearer.",
                    )
                )

    return issues, required_env


def is_environment_only_failure(output: str) -> bool:
    return bool(ENVIRONMENT_FAILURE_RE.search(output)) and not bool(
        CONFIGURATION_FAILURE_RE.search(output)
    )


def decode_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


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

    # An explicit directory prefix keeps a file name that starts with "-" from
    # being parsed as a CLI option.
    command = prefix + ["run", f".{os.sep}{path.name}", "--dry-run"]
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
        output = (decode_output(exc.stdout) + "\n" + decode_output(exc.stderr)).strip()
        return (
            DockerCheck(status="timeout", command=command, output=output[-4000:] or None),
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

    output = decode_output(result.stdout).strip()
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
    result.sort(key=issue_sort_key)
    if len(result) > MAX_REPORTED_ISSUES:
        suppressed = len(result) - MAX_REPORTED_ISSUES
        result = result[:MAX_REPORTED_ISSUES]
        result.append(
            Issue(
                "info",
                "report",
                "issues-suppressed",
                "$",
                f"{suppressed} further issue(s) suppressed; fix the reported ones first.",
            )
        )
    return result


def schema_public_info(info: SchemaInfo) -> dict[str, Any]:
    return {
        "source_kind": info.source_kind,
        "source": info.source,
        "sha256": info.sha256,
        "latest_version": info.latest_version,
        "fetched_at": info.fetched_at,
        "official": info.official,
        "cache_trusted": info.cache_trusted,
    }


def schema_validation_level(info: SchemaInfo) -> str:
    if info.official:
        if not info.cache_trusted:
            return "official (untrusted cache)"
        return "official"
    if info.source_kind == "explicit":
        return "explicit"
    return "core"


def cache_dir_inside_config_tree(cache_dir: Path, config_path: Path) -> bool:
    """True when the schema cache lives inside the validated file's directory tree."""
    try:
        return cache_dir.expanduser().resolve().is_relative_to(config_path.parent.resolve())
    except OSError:
        return False


def write_fixed_file(path: Path, rendered: str) -> None:
    try:
        sources.atomic_write(path, rendered.encode("utf-8"))
    except OSError as exc:
        raise ValidationIncomplete(f"Cannot write formatted YAML {path}: {exc}") from exc


def early_report(
    path: Path,
    schema_info: SchemaInfo,
    issues: list[Issue],
) -> ValidationReport:
    return ValidationReport(
        file=str(path),
        validation_level=schema_validation_level(schema_info),
        schema=schema_public_info(schema_info),
        docker=asdict(DockerCheck(status="not-run")),
        required_environment=[],
        changed=False,
        issues=deduplicate_issues(issues),
    )


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
    except AnchorsNotAllowed as exc:
        sample = ", ".join(str(line) for line in exc.lines[:8])
        issues.append(
            Issue(
                "error",
                "format",
                "yaml-anchor",
                "$",
                f"YAML anchors and aliases are not allowed; found on line(s): {sample}.",
            )
        )
        return early_report(path, schema_info, issues)
    except ValueError as exc:
        issues.append(Issue("error", "yaml", "parse", "$", str(exc)))
        return early_report(path, schema_info, issues)

    if args.fix:
        expected = expected_plain_after_fix(data)
        normalize_structure(data)
        rendered = dump_yaml(data)
        try:
            rendered = reorder_rendered(rendered, str(path))
            data = parse_yaml(rendered, str(path))
        except ValueError as exc:
            raise ValidationIncomplete(
                f"Formatter produced invalid YAML; {path} was left unchanged: {exc}"
            ) from exc
        if to_plain(data) != expected:
            raise ValidationIncomplete(
                f"Formatter would change scalar values; {path} was left unchanged. "
                "Report this as a formatter bug and format the file manually."
            )
        if rendered != original_text:
            write_fixed_file(path, rendered)
            changed = True
        current_text = rendered
    else:
        current_text = original_text

    issues.extend(format_issues(current_text, data))
    issues.extend(validate_json_schema(data, schema_info))
    toolset_types, toolset_types_source = schema_toolset_types(schema_info.schema)
    if toolset_types_source != "schema":
        issues.append(
            Issue(
                "info",
                "schema",
                "toolset-types-fallback",
                "$",
                "The loaded schema does not declare a toolset type enum; the bundled list was used.",
            )
        )
    issues.extend(
        semantic_issues(
            data,
            latest_version=schema_info.latest_version,
            allow_legacy_version=args.allow_legacy_version,
            config_dir=path.parent,
            toolset_types=toolset_types,
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
    if schema_info.notice:
        issues.append(Issue("info", "schema", "schema-digest-changed", "$", schema_info.notice))
    if schema_info.official and not schema_info.cache_trusted:
        issues.append(
            Issue(
                "warning",
                "schema",
                "untrusted-cache-location",
                "$",
                "The schema cache directory lies inside the validated file's directory tree, so the "
                "cached schema could have been supplied with the file. Use a cache outside that tree.",
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
        help="Rewrite the file using canonical clean formatting before validation "
        "(the file is only written when the rewrite verifiably preserves every value)",
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
        "--max-age",
        type=float,
        default=DEFAULT_MAX_CACHE_AGE_HOURS,
        metavar="HOURS",
        help="Reuse the cached official schema when it is younger than this many hours "
        f"(default: {DEFAULT_MAX_CACHE_AGE_HOURS:g}; 0 always re-downloads)",
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
        help="Report a config version that differs from the loaded schema's latest version "
        "as a warning instead of an error",
    )
    parser.add_argument(
        "--docker-check",
        choices=("auto", "required", "off"),
        default="auto",
        help="Run Docker Agent dry-run automatically, require it, or disable it "
        "(auto and required execute the Docker Agent CLI against the file)",
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


def print_schema_info(schema_info: SchemaInfo, as_json: bool) -> None:
    payload = schema_public_info(schema_info)
    if schema_info.warning:
        payload["warning"] = schema_info.warning
    if schema_info.notice:
        payload["notice"] = schema_info.notice
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Source kind: {payload['source_kind']}")
    print(f"Source: {payload['source']}")
    print(f"Official: {'yes' if payload['official'] else 'no'}")
    print(f"Latest config version: {payload['latest_version'] or 'unknown'}")
    print(f"SHA-256: {payload['sha256']}")
    if payload.get("fetched_at"):
        print(f"Fetched at: {payload['fetched_at']}")
    if payload.get("notice"):
        print(f"Notice: {payload['notice']}")
    if payload.get("warning"):
        print(f"Warning: {payload['warning']}")


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.schema_info and not args.file:
        parser.error("file is required unless --schema-info is used")
    if not args.schema_info:
        check_dependencies()

    cache_trusted = True
    if args.file and args.schema_mode == "official" and args.schema is None:
        config_path = Path(args.file).expanduser()
        if cache_dir_inside_config_tree(args.cache_dir, config_path):
            cache_trusted = False

    schema_info = load_schema(
        mode=args.schema_mode,
        cache_dir=args.cache_dir.expanduser(),
        schema_path=args.schema.expanduser() if args.schema else None,
        offline=args.offline,
        refresh=args.refresh_schema,
        timeout=args.schema_timeout,
        max_age_hours=max(args.max_age, 0.0),
        cache_trusted=cache_trusted,
    )
    if args.schema_info:
        print_schema_info(schema_info, args.json)
        return 0
    report = validate_file(args, schema_info)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print_human(report)
    return 0 if report.passed else 1


def report_incomplete(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"status": "incomplete", "error": message}, indent=2, sort_keys=True))
    else:
        print(f"Docker Agent validation: INCOMPLETE\n{message}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args, parser)
    except ValidationIncomplete as exc:
        report_incomplete(str(exc), args.json)
        return 2
    except Exception as exc:  # unexpected failure: never masquerade as "file invalid"
        report_incomplete(
            f"Unexpected {type(exc).__name__} while validating: {exc}",
            args.json,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
