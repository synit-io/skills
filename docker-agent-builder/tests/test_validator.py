#!/usr/bin/env python3
"""Regression tests for the Docker Agent validator.

Run from the skill directory with either
``python3 tests/test_validator.py`` or
``python3 -m unittest discover -s tests``. The tests are offline: every
network call is mocked and Docker dry-run is disabled or faked.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _sources as sources  # noqa: E402
import refresh_official_sources as refresher  # noqa: E402
import validate_agent_yaml as validator  # noqa: E402

VALIDATOR = SCRIPTS / "validate_agent_yaml.py"
SCHEMA_COMMENT = (
    "# yaml-language-server: $schema="
    "https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json"
)
OFFICIAL_SCHEMA = {
    "title": "Docker Agent Configuration",
    "description": "Docker Agent schema",
    "properties": {"version": {"enum": ["16"]}},
}
FAKE_AGENT_CLI = '''
import os
import sys
import time

mode = os.environ.get("FAKE_AGENT_MODE", "ok")
print("args:" + " ".join(sys.argv[1:]))
if mode == "ok":
    sys.exit(0)
if mode == "config":
    print("configuration invalid: unknown field foo")
    sys.exit(1)
if mode == "env":
    print("The following environment variables must be set: OPENAI_API_KEY")
    sys.exit(1)
if mode == "timeout":
    sys.stdout.write("partial output before hanging\\n")
    sys.stdout.flush()
    time.sleep(30)
sys.exit(3)
'''


def fake_secret(prefix: str, body: str) -> str:
    """Assemble a test credential at runtime.

    The fixtures must match the validator's secret patterns, but the same
    shapes trigger GitHub push protection when they appear as literals in
    source. Keeping prefix and body apart means no literal in this file
    looks like a real token to a scanner.
    """
    return prefix + body


FAKE_GITHUB_TOKEN = fake_secret("ghp_", "abcdefghijklmnopqrstuvwxyz0123456789")
FAKE_OPENAI_TOKEN = fake_secret("sk-", "abcdefghijklmnopqrstuvwxyz123456")


def minimal_yaml(**overrides: str) -> str:
    agent_lines = ["    model: openai/gpt-5-mini", "    description: General assistant"]
    for key, value in overrides.items():
        agent_lines.append(f"    {key}: {value}")
    return f"{SCHEMA_COMMENT}\n\nversion: \"16\"\nagents:\n  root:\n" + "\n".join(agent_lines) + "\n"


class ValidatorTestCase(unittest.TestCase):
    maxDiff = None

    def run_validator(
        self,
        yaml_text: str,
        *extra: str,
        filename: str = "agent.yaml",
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None, Path, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / filename
        path.write_text(yaml_text, encoding="utf-8", newline="")
        completed, payload = self.run_existing(path, *extra)
        return completed, payload, path, temporary

    def run_existing(
        self,
        path: Path,
        *extra: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
        completed = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                str(path),
                "--schema-mode",
                "core",
                "--docker-check",
                "off",
                "--json",
                *extra,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        payload: dict[str, Any] | None = None
        if completed.stdout.strip():
            try:
                decoded = json.loads(completed.stdout)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                payload = None
        return completed, payload

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = validator.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_issue(self, payload: dict[str, Any], code: str) -> None:
        codes = [item.get("code") for item in payload.get("issues", [])]
        self.assertIn(code, codes, msg=f"Expected {code!r}; found {codes!r}")

    def assert_no_issue(self, payload: dict[str, Any], code: str) -> None:
        codes = [item.get("code") for item in payload.get("issues", [])]
        self.assertNotIn(code, codes, msg=f"Did not expect {code!r}; found {codes!r}")

    def core_args(self, path: Path, *extra: str) -> Any:
        return validator.build_parser().parse_args(
            [str(path), "--schema-mode", "core", "--docker-check", "off", *extra]
        )

    def core_schema_info(self) -> validator.SchemaInfo:
        return validator.load_schema(
            mode="core",
            cache_dir=Path(tempfile.gettempdir()),
            schema_path=None,
            offline=True,
            refresh=False,
            timeout=1,
        )

    def write_official_cache(self, cache: Path, *, fetched_at: str | None = None, schema: dict[str, Any] | None = None) -> bytes:
        schema_bytes = json.dumps(schema or OFFICIAL_SCHEMA).encode("utf-8")
        cache.mkdir(parents=True, exist_ok=True)
        (cache / sources.SCHEMA_FILENAME).write_bytes(schema_bytes)
        metadata = sources.source_metadata(
            schema_bytes,
            validator.OFFICIAL_SCHEMA_URL,
            extra={"latest_version": "16"},
        )
        if fetched_at is not None:
            metadata["fetched_at"] = fetched_at
        (cache / sources.SCHEMA_META_FILENAME).write_bytes(sources.encode_metadata(metadata))
        return schema_bytes


class GateTests(ValidatorTestCase):
    def test_minimal_file_is_formatted_and_passes(self) -> None:
        text = """agents:
  root:
    instruction: Answer clearly.
    description: General assistant
    model: openai/gpt-5-mini
version: 16
"""
        completed, payload, path, temporary = self.run_validator(text, "--fix")
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assertEqual(payload["status"], "pass")
        self.assertTrue(payload["changed"])
        rendered = path.read_text(encoding="utf-8")
        self.assertTrue(rendered.startswith(SCHEMA_COMMENT + "\n\nversion: \"16\"\n"))
        self.assertIn("agents:\n  root:\n    model: openai/gpt-5-mini\n    description: General assistant", rendered)
        self.assertTrue(rendered.endswith("\n"))
        self.assertFalse(any(line.rstrip() != line for line in rendered.split("\n")))

    def test_duplicate_key_fails_at_yaml_gate_without_echoing_values(self) -> None:
        secret = FAKE_GITHUB_TOKEN
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    model: {secret}
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "parse")
        messages = [item.get("message", "") for item in payload.get("issues", [])]
        self.assertTrue(any('Duplicate YAML key "model" at line 7' in message for message in messages), messages)
        self.assertNotIn(secret, completed.stdout)
        self.assertNotIn("suppress this check", completed.stdout)
        completed_text = subprocess.run(
            [sys.executable, str(VALIDATOR), str(temporary.name) + "/agent.yaml", "--schema-mode", "core", "--docker-check", "off"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotIn(secret, completed_text.stdout + completed_text.stderr)

    def test_yaml_parse_error_omits_source_snippet(self) -> None:
        secret = FAKE_GITHUB_TOKEN
        text = f"{SCHEMA_COMMENT}\n\nversion: \"16\"\nagents: [\n  token: {secret}\n"
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "parse")
        self.assertNotIn(secret, completed.stdout)

    def test_empty_file_and_non_mapping_root_fail_parse_gate(self) -> None:
        for text in ("", "- just\n- a list\n", "scalar\n"):
            with self.subTest(text=text):
                completed, payload, _, temporary = self.run_validator(text)
                self.addCleanup(temporary.cleanup)
                self.assertEqual(completed.returncode, 1)
                assert payload is not None
                self.assert_issue(payload, "parse")

    def test_multiple_documents_fail_parse_gate(self) -> None:
        text = f"{SCHEMA_COMMENT}\n\nversion: \"16\"\nagents:\n  root:\n    model: openai/gpt-5-mini\n---\nversion: \"16\"\n"
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "parse")
        self.assertTrue(any("exactly one YAML document" in item["message"] for item in payload["issues"]))

    def test_anchors_are_rejected_early_and_cheaply(self) -> None:
        lines = ["version: \"16\"", "x: &a [1, 2, 3, 4, 5, 6, 7, 8]"]
        previous = "a"
        for name in "bcdefg":
            lines.append(f"{name}: &{name} [" + ", ".join([f"*{previous}"] * 8) + "]")
            previous = name
        lines.append("agents: {root: {model: x}}")
        completed, payload, path, temporary = self.run_validator("\n".join(lines) + "\n", "--fix")
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "yaml-anchor")
        self.assertLess(len(completed.stdout), 20_000)
        self.assertFalse(payload["changed"])
        self.assertIn("&a", path.read_text(encoding="utf-8"))

    def test_flow_style_is_a_format_error_and_fix_repairs_it(self) -> None:
        text = f"{SCHEMA_COMMENT}\n\nversion: \"16\"\nagents:\n  root: {{model: openai/gpt-5-mini, description: Flow}}\n"
        completed, payload, path, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "flow-style")
        completed, payload = self.run_existing(path, "--fix")
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertNotIn("{", path.read_text(encoding="utf-8"))

    def test_unknown_sub_agent_fails_semantic_gate(self) -> None:
        completed, payload, _, temporary = self.run_validator(
            minimal_yaml(sub_agents="\n      - missing-agent")
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "unknown-agent")

    def test_unknown_top_level_key_fails_schema_gate(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
unknown_section: true
agents:
  root:
    model: openai/gpt-5-mini
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        schema_issues = [item for item in payload["issues"] if item["gate"] == "schema" and item["severity"] == "error"]
        self.assertTrue(schema_issues, payload)
        self.assertTrue(any("unknown_section" in item["message"] for item in schema_issues), schema_issues)

    def test_schema_errors_do_not_echo_instance_values(self) -> None:
        secret = FAKE_GITHUB_TOKEN
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
models:
  primary:
    provider: openai
    model: gpt-5-mini
    max_tokens: {secret}
agents:
  root:
    model: primary
    description: General assistant
"""
        completed, payload, path, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "jsonschema-type")
        self.assertNotIn(secret, completed.stdout + completed.stderr)
        human = subprocess.run(
            [sys.executable, str(VALIDATOR), str(path), "--schema-mode", "core", "--docker-check", "off"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotIn(secret, human.stdout + human.stderr)
        self.assertIn("Expected integer, found string", human.stdout)

    def test_force_handoff_cycle_fails(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  alpha:
    model: openai/gpt-5-mini
    force_handoff: beta
  beta:
    model: openai/gpt-5-mini
    force_handoff: alpha
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "force-handoff-cycle")

    def test_bare_primary_model_name_is_allowed_without_models_section(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: gpt-4
    description: Assistant using a runtime-resolved bare model
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assert_no_issue(payload, "unknown-model")
        self.assert_no_issue(payload, "unresolved-model-name")

    def test_bare_primary_model_typo_warns_with_suggestion(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
models:
  smart:
    provider: openai
    model: gpt-5
agents:
  root:
    model: smrt
    description: Typo in model name
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assert_issue(payload, "unresolved-model-name")
        message = next(item["message"] for item in payload["issues"] if item["code"] == "unresolved-model-name")
        self.assertIn("'smart'", message)
        self.assertEqual(
            [item["severity"] for item in payload["issues"] if item["code"] == "unresolved-model-name"],
            ["warning"],
        )

    def test_unknown_model_in_non_primary_fields_is_an_error(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
models:
  primary:
    provider: openai
    model: gpt-5-mini
    compaction_model: nope
    first_available:
      - primary
      - missing
    fallback:
      models:
        - alsomissing
    routing:
      - model: routed-missing
agents:
  root:
    model: primary
    description: Assistant
    compaction_model: unknown-compaction
    fallback:
      models:
        - agent-fallback-missing
    toolsets:
      - type: model_picker
        models:
          - picker-missing
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        paths = sorted(item["path"] for item in payload["issues"] if item["code"] == "unknown-model")
        self.assertEqual(
            paths,
            [
                "$.agents.root.compaction_model",
                "$.agents.root.fallback.models[0]",
                "$.agents.root.toolsets[0].models[0]",
                "$.models.primary.compaction_model",
                "$.models.primary.fallback.models[0]",
                "$.models.primary.first_available[1]",
                "$.models.primary.routing[0].model",
            ],
        )

    def test_mcp_rag_and_use_references_must_resolve(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
mcps:
  docs:
    ref: docker:context7
rag:
  project_docs:
    docs:
      - ./docs
commands:
  ops: {{}}
skills:
  writing: {{}}
toolsets:
  shared:
    type: think
budgets:
  small: {{}}
agents:
  root:
    model: openai/gpt-5-mini
    description: Assistant
    toolsets:
      - type: mcp
        ref: docs
      - type: mcp
        ref: docker:catalog-item
      - type: mcp
        ref: missing-mcp
      - type: rag
        ref: project_docs
      - type: rag
        ref: missing-rag
    use_toolsets:
      - shared
      - missing-toolset
    use_commands:
      - ops
      - missing-commands
    use_skills:
      - writing
      - missing-skills
    budgets:
      - small
      - missing-budget
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        codes = {(item["code"], item["path"]) for item in payload["issues"] if item["code"].startswith("unknown-")}
        self.assertEqual(
            codes,
            {
                ("unknown-MCP", "$.agents.root.toolsets[2].ref"),
                ("unknown-RAG", "$.agents.root.toolsets[4].ref"),
                ("unknown-toolset", "$.agents.root.use_toolsets[1]"),
                ("unknown-command-group", "$.agents.root.use_commands[1]"),
                ("unknown-skill-group", "$.agents.root.use_skills[1]"),
                ("unknown-budget", "$.agents.root.budgets[1]"),
            },
        )

    def test_file_tool_type_passes_semantic_validation(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: File-aware assistant
    toolsets:
      - type: file
        readonly: true
        allow_list:
          - .
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assert_no_issue(payload, "unknown-tool-type")

    def test_tool_types_are_derived_from_official_schema_shape(self) -> None:
        for container in ("definitions", "$defs"):
            schema = {container: {"Toolset": {"properties": {"type": {"enum": ["think", "file", "future-tool"]}}}}}
            with self.subTest(container=container):
                self.assertEqual(
                    validator.schema_toolset_types(schema),
                    ({"think", "file", "future-tool"}, "schema"),
                )
        types, source = validator.schema_toolset_types({"definitions": {}})
        self.assertEqual(source, "bundled")
        self.assertEqual(types, validator.TOOLSET_TYPES)

    def test_toolset_type_fallback_emits_info(self) -> None:
        text = minimal_yaml()
        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "schema.json"
            schema_path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
            completed, payload, _, temporary = self.run_validator(text, "--schema", str(schema_path))
            self.addCleanup(temporary.cleanup)
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            assert payload is not None
            self.assert_issue(payload, "toolset-types-fallback")

    def test_unconstrained_filesystem_warns(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: File-aware assistant
    toolsets:
      - type: filesystem
        readonly: true
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assert_issue(payload, "unconstrained-filesystem")

    def test_bom_and_indentation_fail_then_fix(self) -> None:
        text = f"﻿{SCHEMA_COMMENT}\n\nversion: \"16\"\nagents:\n    root:\n        model: openai/gpt-5-mini\n        description: General assistant\n"
        completed, payload, path, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "utf8-bom")
        self.assert_issue(payload, "indentation")
        completed, payload = self.run_existing(path, "--fix")
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_tabs_inside_block_scalars_are_content_not_format_errors(self) -> None:
        text = minimal_yaml(instruction="|\n      Column A\tColumn B\n      end")
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assert_no_issue(payload, "tabs")
        text = minimal_yaml() + "\tinstruction: bad\n"
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assertTrue(
            any(item["code"] in {"tabs", "parse"} for item in payload["issues"]),
            payload,
        )

    def test_indentation_check_is_linear_on_long_block_scalar_headers(self) -> None:
        text = minimal_yaml(instruction="|" + " " * 40000 + "\n      hello")
        started = datetime.now()
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertLess((datetime.now() - started).total_seconds(), 5)
        assert payload is not None
        self.assert_issue(payload, "trailing-whitespace")

    def test_missing_instruction_file_fails(self) -> None:
        completed, payload, _, temporary = self.run_validator(minimal_yaml(instruction_file="missing.md"))
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "missing-instruction-file")

    def test_instruction_file_symlink_cannot_escape_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as config_directory, tempfile.TemporaryDirectory() as outside:
            config_path = Path(config_directory)
            outside_file = Path(outside) / "instructions.md"
            outside_file.write_text("External instructions", encoding="utf-8")
            (config_path / "instructions.md").symlink_to(outside_file)
            agent_path = config_path / "agent.yaml"
            agent_path.write_text(minimal_yaml(instruction_file="instructions.md"), encoding="utf-8")
            completed, payload = self.run_existing(agent_path)
            self.assertEqual(completed.returncode, 1)
            assert payload is not None
            self.assert_issue(payload, "unsafe-instruction-file")

    def test_strict_structured_output_requires_every_property(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: Structured extractor
    structured_output:
      name: extraction
      strict: true
      schema:
        type: object
        properties:
          summary:
            type: string
          confidence:
            type: number
        required:
          - summary
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "strict-output-required-properties")

    def test_allow_legacy_version_downgrades_version_mismatch(self) -> None:
        text = minimal_yaml().replace('version: "16"', 'version: "15"')
        completed, payload, path, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assertEqual(
            [item["severity"] for item in payload["issues"] if item["code"] == "non-current-version"],
            ["error"],
        )
        completed, payload = self.run_existing(path, "--allow-legacy-version")
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assertEqual(
            [item["severity"] for item in payload["issues"] if item["code"] == "non-current-version"],
            ["warning"],
        )
        self.assertEqual(payload["status"], "pass")

    def test_issue_cap_adds_suppression_note(self) -> None:
        issues = [
            validator.Issue("error", "schema", "x", f"$.p{index}", "m") for index in range(validator.MAX_REPORTED_ISSUES + 25)
        ]
        result = validator.deduplicate_issues(issues)
        self.assertEqual(len(result), validator.MAX_REPORTED_ISSUES + 1)
        self.assertEqual(result[-1].code, "issues-suppressed")
        self.assertIn("25 further", result[-1].message)


class FormatterTests(ValidatorTestCase):
    def roundtrip_fix(self, text: str) -> tuple[Path, dict[str, Any], tempfile.TemporaryDirectory[str]]:
        completed, payload, path, temporary = self.run_validator(text, "--fix")
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        return path, payload, temporary

    def plain(self, path: Path) -> Any:
        return validator.to_plain(validator.parse_yaml(path.read_text(encoding="utf-8"), str(path)))

    def test_formatter_replaces_relative_schema_comment(self) -> None:
        text = """# yaml-language-server: $schema=../agent-schema.json

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
"""
        path, _, temporary = self.roundtrip_fix(text)
        self.addCleanup(temporary.cleanup)
        rendered = path.read_text(encoding="utf-8")
        self.assertEqual(rendered.count("yaml-language-server:"), 1)
        self.assertTrue(rendered.startswith(SCHEMA_COMMENT + "\n"))
        self.assertNotIn("../agent-schema.json", rendered)

    def test_formatter_preserves_instruction_scalar_value(self) -> None:
        instruction = (
            "This is a deliberately long one-line instruction whose exact value must remain "
            "unchanged while the formatter chooses a readable block scalar representation."
        )
        path, _, temporary = self.roundtrip_fix(minimal_yaml(instruction=instruction))
        self.addCleanup(temporary.cleanup)
        self.assertEqual(self.plain(path)["agents"]["root"]["instruction"], instruction)
        self.assertIn("instruction: |", path.read_text(encoding="utf-8"))

    def test_formatter_preserves_schema_directive_text_inside_instruction(self) -> None:
        path, _, temporary = self.roundtrip_fix(
            minimal_yaml(instruction="|\n      Keep this literal line:\n      # yaml-language-server: $schema=../agent-schema.json")
        )
        self.addCleanup(temporary.cleanup)
        self.assertIn("      # yaml-language-server: $schema=../agent-schema.json", path.read_text(encoding="utf-8"))

    def test_formatter_keeps_trailing_spaces_inside_block_scalars(self) -> None:
        text = minimal_yaml(instruction="|\n      Hard break  \n      second line")
        path, payload, temporary = self.roundtrip_fix(text)
        self.addCleanup(temporary.cleanup)
        self.assertFalse(payload["changed"])
        self.assertEqual(self.plain(path)["agents"]["root"]["instruction"], "Hard break  \nsecond line\n")
        self.assert_no_issue(payload, "trailing-whitespace")

    def test_formatter_keeps_carriage_returns_in_quoted_instruction(self) -> None:
        text = minimal_yaml(instruction='"Progress bar uses \\r to redraw\\nSecond line of a long instruction to force literal style"')
        expected = "Progress bar uses \r to redraw\nSecond line of a long instruction to force literal style"
        path, payload, temporary = self.roundtrip_fix(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(self.plain(path)["agents"]["root"]["instruction"], expected)
        text = minimal_yaml(instruction='"first\\r\\nsecond"', welcome_message='"a\\r\\nb"')
        path, payload, temporary = self.roundtrip_fix(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(self.plain(path)["agents"]["root"]["instruction"], "first\r\nsecond")
        self.assertEqual(self.plain(path)["agents"]["root"]["welcome_message"], "a\r\nb")

    def test_formatter_leaves_null_version_alone(self) -> None:
        text = minimal_yaml().replace('version: "16"', "version:")
        completed, payload, path, temporary = self.run_validator(text, "--fix")
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "missing-version")
        self.assertNotIn('"None"', path.read_text(encoding="utf-8"))
        self.assertIsNone(self.plain(path)["version"])

    def test_formatter_moves_comments_with_their_keys(self) -> None:
        text = f"""{SCHEMA_COMMENT}

agents:
  root:
    instruction: |
      Do the thing.
      # not a comment
    # model comment
    model: primary   # eol comment
    description: Lead
    toolsets:
      # first toolset
      - ref: docs
        type: mcp
      - type: think

  helper:
    description: Helper
    model: primary

# Models section header
models:
  primary:
    model: gpt-5-mini
    provider: openai

mcps:
  docs:
    ref: docker:context7

version: 16
"""
        path, payload, temporary = self.roundtrip_fix(text)
        self.addCleanup(temporary.cleanup)
        rendered = path.read_text(encoding="utf-8")
        self.assertTrue(payload["changed"])
        self.assertIn("# Models section header\nmodels:\n  primary:\n    provider: openai\n    model: gpt-5-mini\n", rendered)
        self.assertIn("  root:\n    # model comment\n    model: primary   # eol comment\n    description: Lead\n    instruction: |\n      Do the thing.\n      # not a comment\n    toolsets:\n      # first toolset\n      - type: mcp\n        ref: docs\n      - type: think\n\n  helper:\n    model: primary\n    description: Helper\n", rendered)
        self.assertTrue(rendered.startswith(SCHEMA_COMMENT + "\n\nversion: \"16\"\n\n# Models section header\nmodels:\n"))
        self.assertIn("\nmcps:\n  docs:\n    ref: docker:context7\n\nagents:\n", rendered)
        completed, payload = self.run_existing(path, "--fix")
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assertFalse(payload["changed"])

    def test_formatter_writes_atomically_and_preserves_mode(self) -> None:
        text = minimal_yaml().replace('version: "16"', "version: 16")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.yaml"
            path.write_text(text, encoding="utf-8")
            path.chmod(0o640)
            completed, payload = self.run_existing(path, "--fix")
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            assert payload is not None
            self.assertTrue(payload["changed"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(sorted(entry.name for entry in Path(directory).iterdir()), ["agent.yaml"])

    def test_formatter_refuses_to_write_when_values_would_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.yaml"
            original = minimal_yaml().replace('version: "16"', "version: 16")
            path.write_text(original, encoding="utf-8")
            args = self.core_args(path, "--fix")
            with mock.patch.object(validator, "dump_yaml", return_value=SCHEMA_COMMENT + "\n\nversion: \"16\"\nagents:\n  root:\n    model: changed\n"):
                with self.assertRaises(validator.ValidationIncomplete):
                    validator.validate_file(args, self.core_schema_info())
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            with mock.patch.object(validator, "dump_yaml", return_value="agents: [\n"):
                with self.assertRaises(validator.ValidationIncomplete):
                    validator.validate_file(args, self.core_schema_info())
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_bundled_templates_pass_core_validation_and_are_idempotent(self) -> None:
        templates = sorted((SKILL_ROOT / "assets" / "templates").glob("*.yaml"))
        self.assertGreaterEqual(len(templates), 5)
        for source in templates:
            with self.subTest(template=source.name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / source.name
                target.write_bytes(source.read_bytes())
                completed, payload = self.run_existing(target)
                self.assertEqual(completed.returncode, 0, f"{source.name}: {completed.stderr}\n{completed.stdout}")
                completed, payload = self.run_existing(target, "--fix")
                self.assertEqual(completed.returncode, 0, f"{source.name}: {completed.stderr}\n{completed.stdout}")
                assert payload is not None
                self.assertEqual(payload["status"], "pass", payload)
                self.assertFalse(payload["changed"], f"{source.name} is not canonically formatted")
                self.assertEqual(target.read_bytes(), source.read_bytes())


class SecurityTests(ValidatorTestCase):
    def env_yaml(self, key: str, value: str) -> str:
        return f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    toolsets:
      - type: mcp
        command: example
        env:
          {key}: {value}
"""

    def test_literal_secret_fails_security_gate(self) -> None:
        completed, payload, _, temporary = self.run_validator(
            self.env_yaml("API_KEY", FAKE_OPENAI_TOKEN)
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "literal-sensitive-value")
        self.assert_issue(payload, "literal-secret")

    def test_every_known_secret_pattern_is_detected_without_echo(self) -> None:
        samples = {
            "private-key": fake_secret("-----BEGIN ", "ENCRYPTED PRIVATE KEY-----"),
            "openai-style-token": FAKE_OPENAI_TOKEN,
            "github-token": FAKE_GITHUB_TOKEN,
            "github-fine-grained-token": fake_secret("github_pat_", "11ABCDEFG0abcdefghijklmnopqrstuvwxyz"),
            "aws-access-key": fake_secret("AKIA", "ABCDEFGHIJKLMNOP"),
            "google-api-key": fake_secret("AIza", "SyA1234567890abcdefghijklmnopqrstuv"),
            "slack-token": fake_secret("xoxb-", "1234567890-abcdefghijklmnop"),
            "jwt": fake_secret("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.", "eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"),
        }
        self.assertEqual(set(samples), {name for name, _ in validator.KNOWN_SECRET_PATTERNS})
        for name, sample in samples.items():
            with self.subTest(pattern=name):
                text = minimal_yaml(instruction=f'"Notes: {sample}"')
                completed, payload, _, temporary = self.run_validator(text)
                self.addCleanup(temporary.cleanup)
                self.assertEqual(completed.returncode, 1)
                assert payload is not None
                messages = [item["message"] for item in payload["issues"] if item["code"] == "literal-secret"]
                self.assertTrue(any(name in message for message in messages), messages)
                self.assertNotIn(sample, completed.stdout + completed.stderr)
        for header in ("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN DSA PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----"):
            with self.subTest(header=header):
                self.assertTrue(validator.KNOWN_SECRET_PATTERNS[0][1].search(header))

    def test_url_userinfo_password_is_detected_but_interpolation_is_not(self) -> None:
        text = self.env_yaml("DATABASE_URL", '"postgres://admin:SuperSecret123@db.internal:5432/app"')
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assertTrue(any("url-password" in item["message"] for item in payload["issues"]), payload)
        self.assertNotIn("SuperSecret123", completed.stdout)
        text = self.env_yaml("DATABASE_URL", '"postgres://admin:${env.DB_PASSWORD}@db.internal:5432/app"')
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assertIn("DB_PASSWORD", payload["required_environment"])

    def test_sensitive_flags_in_args_are_detected(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    toolsets:
      - type: mcp
        command: npx
        args:
          - server
          - --token=hunter2hunter2
          - --api-key
          - literalkey
          - --password
          - ${{env.SERVICE_PASSWORD}}
          - --verbose
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        paths = sorted(item["path"] for item in payload["issues"] if item["code"] == "literal-sensitive-argument")
        self.assertEqual(paths, ["$.agents.root.toolsets[0].args[1]", "$.agents.root.toolsets[0].args[3]"])
        self.assertNotIn("hunter2", completed.stdout)
        self.assertNotIn("literalkey", completed.stdout)

    def test_list_items_under_sensitive_keys_are_checked(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    toolsets:
      - type: mcp
        remote:
          url: https://example.invalid/mcp
          headers:
            X-Api-Key:
              - literal-header-value
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assertEqual(
            [item["path"] for item in payload["issues"] if item["code"] == "literal-sensitive-value"],
            ["$.agents.root.toolsets[0].remote.headers.X-Api-Key[0]"],
        )

    def test_key_name_heuristic_ignores_commands_and_prompt_fields(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
commands:
  ops:
    rotate-token: Explain how to rotate a token safely.
agents:
  root:
    model: openai/gpt-5-mini
    description: The password reset assistant
    instruction: |
      When asked about an api_key, explain the process without revealing anything.
    use_commands:
      - ops
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assert_no_issue(payload, "literal-sensitive-value")

    def test_sensitive_value_cannot_mix_literal_and_interpolation(self) -> None:
        completed, payload, _, temporary = self.run_validator(self.env_yaml("API_KEY", "actual-secret-${env.NOOP}"))
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "literal-sensitive-value")

    def test_sensitive_value_requires_env_namespace(self) -> None:
        completed, payload, _, temporary = self.run_validator(self.env_yaml("GITHUB_TOKEN", '"${GITHUB_TOKEN}"'))
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "literal-sensitive-value")

    def test_auth_scheme_prefixed_env_reference_is_allowed(self) -> None:
        for scheme in ("Bearer", "token", "Basic"):
            with self.subTest(scheme=scheme):
                text = f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    toolsets:
      - type: mcp
        remote:
          url: https://example.invalid/mcp
          headers:
            Authorization: "{scheme} ${{env.SERVICE_TOKEN}}"
"""
                completed, payload, _, temporary = self.run_validator(text)
                self.addCleanup(temporary.cleanup)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                assert payload is not None
                self.assertIn("SERVICE_TOKEN", payload["required_environment"])

    def test_token_key_must_be_an_environment_variable_name(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: "16"
providers:
  internal:
    provider: openai
    base_url: "${{env.INTERNAL_LLM_BASE_URL}}"
    token_key: "${{env.INTERNAL_LLM_API_KEY}}"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "invalid-token-key")


class RuntimeTests(ValidatorTestCase):
    def test_runtime_environment_classifier_is_conservative(self) -> None:
        positives = [
            "The following environment variables must be set: OPENAI_API_KEY",
            "OPENAI_API_KEY is not set",
            "missing API key for provider openai",
            "environment variable ANTHROPIC_API_KEY is required",
        ]
        negatives = [
            "configuration invalid: unknown field; API key is required",
            "max_tokens is required to be positive",
            "unexpected token ':' in agent.yaml; required key 'agents' missing",
            "command 'npx' not found; token budget missing",
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(validator.is_environment_only_failure(text))
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(validator.is_environment_only_failure(text))

    def fake_cli(self, directory: Path) -> list[str]:
        script = directory / "fake_agent.py"
        script.write_text(FAKE_AGENT_CLI, encoding="utf-8")
        return [sys.executable, str(script)]

    def test_docker_dry_run_with_fake_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.fake_cli(root)
            config = root / "-leading-dash.yaml"
            config.write_text(minimal_yaml(), encoding="utf-8")
            cases = {
                "ok": ("passed", []),
                "config": ("failed", ["docker-agent-dry-run"]),
                "env": ("environment-blocked", ["runtime-environment"]),
            }
            for mode, (status, codes) in cases.items():
                with self.subTest(mode=mode), mock.patch.dict(os.environ, {"FAKE_AGENT_MODE": mode}), mock.patch.object(
                    validator, "detect_docker_agent", return_value=prefix
                ):
                    check, issues = validator.docker_dry_run(config, mode="auto", timeout=10, environment_policy="warn")
                    self.assertEqual(check.status, status)
                    self.assertEqual([issue.code for issue in issues], codes)
                    assert check.command is not None
                    self.assertEqual(check.command[-2:], [f".{os.sep}-leading-dash.yaml", "--dry-run"])
                    if check.output:
                        self.assertIn("args:run ./-leading-dash.yaml --dry-run", check.output)
            with mock.patch.dict(os.environ, {"FAKE_AGENT_MODE": "env"}), mock.patch.object(
                validator, "detect_docker_agent", return_value=prefix
            ):
                check, issues = validator.docker_dry_run(config, mode="auto", timeout=10, environment_policy="error")
                self.assertEqual(check.status, "failed")
                self.assertEqual([issue.code for issue in issues], ["docker-agent-dry-run"])
            with mock.patch.dict(os.environ, {"FAKE_AGENT_MODE": "timeout"}), mock.patch.object(
                validator, "detect_docker_agent", return_value=prefix
            ):
                check, issues = validator.docker_dry_run(config, mode="auto", timeout=1, environment_policy="warn")
                self.assertEqual(check.status, "timeout")
                self.assertEqual([issue.code for issue in issues], ["docker-agent-timeout"])
                self.assertIn("partial output", check.output or "")
            with mock.patch.object(validator, "detect_docker_agent", return_value=None):
                check, issues = validator.docker_dry_run(config, mode="required", timeout=1, environment_policy="warn")
                self.assertEqual(check.status, "unavailable")
                self.assertEqual([issue.severity for issue in issues], ["error"])

    def test_timeout_with_bytes_output_does_not_crash(self) -> None:
        exc = subprocess.TimeoutExpired(cmd=["x"], timeout=1, output=b"partial \xff bytes", stderr=None)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "agent.yaml"
            config.write_text(minimal_yaml(), encoding="utf-8")
            with mock.patch.object(validator, "detect_docker_agent", return_value=["fake"]), mock.patch.object(
                validator.subprocess, "run", side_effect=exc
            ):
                check, issues = validator.docker_dry_run(config, mode="auto", timeout=1, environment_policy="warn")
        self.assertEqual(check.status, "timeout")
        self.assertIn("partial", check.output or "")
        self.assertEqual([issue.code for issue in issues], ["docker-agent-timeout"])


class SchemaSourceTests(ValidatorTestCase):
    def test_corrupted_official_cache_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / sources.SCHEMA_FILENAME).write_bytes(json.dumps(OFFICIAL_SCHEMA).encode("utf-8"))
            (cache / sources.SCHEMA_META_FILENAME).write_text(
                json.dumps({"source": validator.OFFICIAL_SCHEMA_URL, "sha256": "0" * 64}),
                encoding="utf-8",
            )
            with self.assertRaises(validator.ValidationIncomplete):
                validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=True, refresh=False, timeout=1)

    def test_non_utf8_metadata_is_reported_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            self.write_official_cache(cache)
            (cache / sources.SCHEMA_META_FILENAME).write_bytes(b"\xff\xfe\x00 not json")
            with self.assertRaises(validator.ValidationIncomplete):
                validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=True, refresh=False, timeout=1)

    def test_explicit_schema_is_not_labeled_official(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema.json"
            path.write_text(json.dumps(OFFICIAL_SCHEMA), encoding="utf-8")
            info = validator.load_schema(
                mode="official",
                cache_dir=Path(directory) / "cache",
                schema_path=path,
                offline=True,
                refresh=False,
                timeout=1,
            )
            self.assertFalse(info.official)
            self.assertEqual(validator.schema_validation_level(info), "explicit")

    def test_live_fetch_writes_cache_and_reports_digest_change(self) -> None:
        new_schema = dict(OFFICIAL_SCHEMA, properties={"version": {"enum": ["16", "17"]}})
        new_bytes = json.dumps(new_schema).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            self.write_official_cache(cache, fetched_at="2020-01-01T00:00:00+00:00")
            with mock.patch.object(sources, "fetch_bytes", return_value=new_bytes) as fetch:
                info = validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=False, refresh=False, timeout=1)
            fetch.assert_called_once()
            self.assertEqual(info.source_kind, "official-live")
            self.assertTrue(info.official)
            self.assertEqual(info.latest_version, "17")
            self.assertIsNone(info.warning)
            assert info.notice is not None
            self.assertIn("changed since the previous download", info.notice)
            self.assertIn("16", info.notice)
            self.assertIn("17", info.notice)
            self.assertEqual((cache / sources.SCHEMA_FILENAME).read_bytes(), new_bytes)
            metadata = json.loads((cache / sources.SCHEMA_META_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(metadata["sha256"], hashlib.sha256(new_bytes).hexdigest())
            self.assertEqual(metadata["bytes"], len(new_bytes))
            self.assertEqual(metadata["latest_version"], "17")
            self.assertEqual(metadata["source"], validator.OFFICIAL_SCHEMA_URL)

    def test_fresh_cache_is_reused_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            self.write_official_cache(cache)
            with mock.patch.object(sources, "fetch_bytes", side_effect=AssertionError("must not download")):
                info = validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=False, refresh=False, timeout=1)
            self.assertEqual(info.source_kind, "official-cache")
            self.assertIsNone(info.warning)
            stale = (datetime.now(timezone.utc) - timedelta(hours=48)).replace(microsecond=0).isoformat()
            self.write_official_cache(cache, fetched_at=stale)
            with mock.patch.object(sources, "fetch_bytes", return_value=json.dumps(OFFICIAL_SCHEMA).encode("utf-8")) as fetch:
                info = validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=False, refresh=False, timeout=1)
            fetch.assert_called_once()
            self.assertEqual(info.source_kind, "official-live")
            self.assertIsNone(info.notice)
            with mock.patch.object(sources, "fetch_bytes", return_value=json.dumps(OFFICIAL_SCHEMA).encode("utf-8")) as fetch:
                info = validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=False, refresh=False, timeout=1, max_age_hours=0)
            fetch.assert_called_once()

    def test_live_fetch_survives_unwritable_cache(self) -> None:
        schema_bytes = json.dumps(OFFICIAL_SCHEMA).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            with mock.patch.object(sources, "fetch_bytes", return_value=schema_bytes), mock.patch.object(
                sources, "atomic_write", side_effect=OSError("read-only cache")
            ):
                info = validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=False, refresh=False, timeout=1)
            self.assertEqual(info.source_kind, "official-live")
            self.assertTrue(info.official)
            assert info.warning is not None
            self.assertIn("could not update the cache", info.warning)

    def test_failed_fetch_falls_back_to_cache_and_refresh_requires_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            self.write_official_cache(cache, fetched_at="2020-01-01T00:00:00+00:00")
            with mock.patch.object(sources, "fetch_bytes", side_effect=urllib.error.URLError("no network")):
                info = validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=False, refresh=False, timeout=1)
                self.assertEqual(info.source_kind, "official-cache")
                assert info.warning is not None
                self.assertIn("no network", info.warning)
                with self.assertRaises(validator.ValidationIncomplete):
                    validator.load_schema(mode="official", cache_dir=cache, schema_path=None, offline=False, refresh=True, timeout=1)
            with mock.patch.object(sources, "fetch_bytes", side_effect=urllib.error.URLError("no network")):
                with self.assertRaises(validator.ValidationIncomplete):
                    validator.load_schema(mode="official", cache_dir=cache / "missing", schema_path=None, offline=False, refresh=False, timeout=1)

    def test_cache_inside_config_tree_downgrades_validation_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / ".cache"
            self.write_official_cache(cache)
            config = root / "agent.yaml"
            config.write_text(minimal_yaml(), encoding="utf-8")
            code, out, _ = self.run_main(str(config), "--offline", "--cache-dir", str(cache), "--docker-check", "off", "--json")
            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertEqual(payload["validation_level"], "official (untrusted cache)")
            self.assertFalse(payload["schema"]["cache_trusted"])
            self.assertIn("untrusted-cache-location", [item["code"] for item in payload["issues"]])
            outside = root.parent / f"docker-agent-builder-test-cache-{os.getpid()}"
            self.write_official_cache(outside)
            try:
                code, out, _ = self.run_main(str(config), "--offline", "--cache-dir", str(outside), "--docker-check", "off", "--json")
            finally:
                for entry in outside.iterdir():
                    entry.unlink()
                outside.rmdir()
            payload = json.loads(out)
            self.assertEqual(payload["validation_level"], "official")
            self.assertTrue(payload["schema"]["cache_trusted"])

    def test_uri_format_warning_depends_on_format_checker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.yaml"
            path.write_text(minimal_yaml(), encoding="utf-8")
            data = validator.parse_yaml(path.read_text(encoding="utf-8"), str(path))
            info = self.core_schema_info()
            with mock.patch.object(validator, "uri_format_available", return_value=False):
                codes = [issue.code for issue in validator.validate_json_schema(data, info)]
            self.assertIn("uri-format-unavailable", codes)
            with mock.patch.object(validator, "uri_format_available", return_value=True):
                codes = [issue.code for issue in validator.validate_json_schema(data, info)]
            self.assertNotIn("uri-format-unavailable", codes)

    def test_source_refresh_failure_preserves_existing_cache(self) -> None:
        schema_bytes = json.dumps(OFFICIAL_SCHEMA).encode("utf-8")
        old_files = {
            "agent-schema.json": b"old-schema",
            "agent-schema.meta.json": b"old-schema-meta",
            "llms.txt": b"old-llms",
            "llms.meta.json": b"old-llms-meta",
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            for name, data in old_files.items():
                (cache / name).write_bytes(data)
            with mock.patch.object(
                refresher,
                "download",
                side_effect=[schema_bytes, OSError("llms download failed")],
            ), self.assertRaises(OSError):
                refresher.refresh_sources(cache_dir=cache, vendor_dir=None, timeout=1, schema_only=False, llms_only=False)
            for name, data in old_files.items():
                self.assertEqual((cache / name).read_bytes(), data)

    def test_refresh_accepts_current_llms_index_shape_and_reports_changes(self) -> None:
        llms = (
            b"# Docker Agent\n\n"
            b"- [Configuration](https://docker.github.io/docker-agent/configuration/overview/)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            self.write_official_cache(cache, schema={**OFFICIAL_SCHEMA, "properties": {"version": {"enum": ["15"]}}})
            with mock.patch.object(refresher, "download", side_effect=[json.dumps(OFFICIAL_SCHEMA).encode("utf-8"), llms]):
                outcome = refresher.refresh_sources(cache_dir=cache, vendor_dir=None, timeout=1, schema_only=False, llms_only=False)
            self.assertEqual(set(outcome["sources"]), {"schema", "llms"})
            self.assertEqual((cache / "llms.txt").read_bytes(), llms)
            self.assertEqual(len(outcome["notices"]), 1)
            self.assertIn("official schema source changed", outcome["notices"][0])
            metadata = json.loads((cache / sources.SCHEMA_META_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(set(metadata), {"source", "fetched_at", "sha256", "bytes", "latest_version"})
            self.assertEqual(metadata["latest_version"], "16")

    def test_refresh_rejects_schema_without_official_identity(self) -> None:
        with self.assertRaises(ValueError):
            refresher.validate_schema(json.dumps({"title": "Docker Agent Configuration"}).encode("utf-8"))
        with self.assertRaises(ValueError):
            refresher.validate_schema(b"not json")
        self.assertTrue(sources.is_official_schema(OFFICIAL_SCHEMA))
        self.assertIs(validator.is_official_schema, validator.is_official_schema)
        self.assertTrue(validator.is_official_schema({"title": "Docker Agent Configuration", "$id": "https://github.com/docker/docker-agent/x"}))

    def test_refresh_main_reports_failure_with_exit_2(self) -> None:
        with mock.patch.object(refresher, "download", side_effect=urllib.error.URLError("offline")):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = refresher.main(["--schema-only", "--cache-dir", tempfile.gettempdir()])
        self.assertEqual(code, 2)
        self.assertIn("Refresh failed", err.getvalue())


class DownloadTests(unittest.TestCase):
    def curl_result(self, payload: bytes = b"verified-content") -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["curl"], returncode=0, stdout=payload, stderr=b"")

    def test_connection_error_uses_verified_curl_fallback(self) -> None:
        with mock.patch.object(sources, "open_url", side_effect=urllib.error.URLError("connection reset")), mock.patch.object(
            sources.shutil, "which", return_value="/usr/bin/curl"
        ), mock.patch.object(sources.subprocess, "run", return_value=self.curl_result()) as run:
            self.assertEqual(sources.fetch_bytes("https://example.invalid/source", 1), b"verified-content")
            self.assertEqual(refresher.download("https://example.invalid/source", 1), b"verified-content")
        command = run.call_args[0][0]
        self.assertIn("--proto", command)
        self.assertEqual(command[command.index("--proto") + 1], "=https")
        self.assertEqual(command[command.index("--proto-redir") + 1], "=https")
        self.assertEqual(command[command.index("--max-filesize") + 1], str(sources.MAX_DOWNLOAD_BYTES))

    def test_http_status_and_certificate_errors_do_not_fall_back(self) -> None:
        http_error = urllib.error.HTTPError("https://example.invalid", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(sources, "open_url", side_effect=http_error), mock.patch.object(
            sources.subprocess, "run", side_effect=AssertionError("curl must not run")
        ):
            with self.assertRaises(urllib.error.HTTPError):
                sources.fetch_bytes("https://example.invalid/source", 1)
        cert_error = urllib.error.URLError(sources.ssl.SSLCertVerificationError("certificate verify failed"))
        with mock.patch.object(sources, "open_url", side_effect=cert_error), mock.patch.object(
            sources, "python_has_ca_certificates", return_value=True
        ), mock.patch.object(sources.subprocess, "run", side_effect=AssertionError("curl must not run")):
            with self.assertRaises(urllib.error.URLError):
                sources.fetch_bytes("https://example.invalid/source", 1)
        with mock.patch.object(sources, "open_url", side_effect=cert_error), mock.patch.object(
            sources, "python_has_ca_certificates", return_value=False
        ), mock.patch.object(sources.shutil, "which", return_value="/usr/bin/curl"), mock.patch.object(
            sources.subprocess, "run", return_value=self.curl_result()
        ):
            self.assertEqual(sources.fetch_bytes("https://example.invalid/source", 1), b"verified-content")

    def test_oversized_responses_and_insecure_redirects_are_refused(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def read(self, limit: int = -1) -> bytes:
                return self.payload[:limit] if limit >= 0 else self.payload

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        with mock.patch.object(sources, "open_url", return_value=FakeResponse(b"x" * 10)), mock.patch.object(
            sources.subprocess, "run", side_effect=AssertionError("curl must not run")
        ):
            with self.assertRaises(sources.ResponseTooLarge):
                sources.fetch_bytes("https://example.invalid/source", 1, max_bytes=5)
            self.assertEqual(sources.fetch_bytes("https://example.invalid/source", 1, max_bytes=10), b"x" * 10)
        handler = sources._HttpsOnlyRedirectHandler()
        request = urllib.request.Request("https://example.invalid/source")
        with self.assertRaises(sources.InsecureRedirect):
            handler.redirect_request(request, None, 302, "Found", {}, "http://example.invalid/insecure")
        with self.assertRaises(urllib.error.URLError):
            sources.fetch_bytes("http://example.invalid/source", 1)
        http_exception = sources.http.client.RemoteDisconnected("closed")
        with mock.patch.object(sources, "open_url", side_effect=http_exception), mock.patch.object(
            sources.shutil, "which", return_value=None
        ):
            with self.assertRaises(urllib.error.URLError):
                sources.fetch_bytes("https://example.invalid/source", 1)


class CommandLineTests(ValidatorTestCase):
    def test_schema_info_with_core_schema(self) -> None:
        code, out, _ = self.run_main("--schema-info", "--schema-mode", "core")
        self.assertEqual(code, 0)
        self.assertIn("Source kind: bundled-core", out)
        self.assertIn(f"Latest config version: {validator.BUNDLED_SNAPSHOT_VERSION}", out)
        code, out, _ = self.run_main("--schema-info", "--schema-mode", "core", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["source_kind"], "bundled-core")
        self.assertFalse(payload["official"])

    def test_schema_info_does_not_require_validator_dependencies(self) -> None:
        with mock.patch.object(validator, "YAML", None), mock.patch.object(validator, "validator_for", None):
            code, out, _ = self.run_main("--schema-info", "--schema-mode", "core")
        self.assertEqual(code, 0, out)

    def test_exit_2_paths(self) -> None:
        code, _, err = self.run_main("/nonexistent/agent.yaml", "--schema-mode", "core", "--docker-check", "off")
        self.assertEqual(code, 2)
        self.assertIn("INCOMPLETE", err)
        with tempfile.TemporaryDirectory() as directory:
            code, _, err = self.run_main(str(directory), "--schema-mode", "core", "--docker-check", "off")
            self.assertEqual(code, 2)
            code, out, _ = self.run_main("agent.yaml", "--offline", "--cache-dir", str(Path(directory) / "empty"), "--json")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["status"], "incomplete")
            missing_schema = Path(directory) / "missing.json"
            code, _, err = self.run_main("agent.yaml", "--schema", str(missing_schema))
            self.assertEqual(code, 2)
        with mock.patch.object(validator, "validator_for", None):
            code, _, err = self.run_main("agent.yaml", "--schema-mode", "core")
            self.assertEqual(code, 2)
            self.assertIn("jsonschema", err)

    def test_unexpected_exception_exits_2_with_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.yaml"
            path.write_text(minimal_yaml(), encoding="utf-8")
            with mock.patch.object(validator, "validate_file", side_effect=RuntimeError("boom")):
                code, out, _ = self.run_main(str(path), "--schema-mode", "core", "--docker-check", "off", "--json")
            self.assertEqual(code, 2)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "incomplete")
            self.assertIn("RuntimeError", payload["error"])
            with mock.patch.object(validator, "validate_file", side_effect=RuntimeError("boom")):
                code, out, err = self.run_main(str(path), "--schema-mode", "core", "--docker-check", "off")
            self.assertEqual(code, 2)
            self.assertIn("Unexpected RuntimeError", err)

    def test_file_argument_is_required_without_schema_info(self) -> None:
        with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
            validator.main(["--schema-mode", "core"])
        self.assertEqual(raised.exception.code, 2)

    def test_scripts_run_from_another_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for script in (VALIDATOR, SCRIPTS / "refresh_official_sources.py"):
                completed = subprocess.run(
                    [sys.executable, str(script), "--help"],
                    cwd=directory,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


class DocumentationConsistencyTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (SKILL_ROOT / relative).read_text(encoding="utf-8")

    def test_source_manifest_hashes_match_files(self) -> None:
        manifest = self.read("references/source-manifest.md")
        matches = re.findall(r"`(references/[^`]+)`[^\n]*SHA-256 `([0-9a-f]{64})`", manifest)
        self.assertGreaterEqual(len(matches), 3)
        for relative_path, expected in matches:
            with self.subTest(file=relative_path):
                actual = hashlib.sha256((SKILL_ROOT / relative_path).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)

    def test_skill_resource_map_paths_exist(self) -> None:
        skill = self.read("SKILL.md")
        resource_map = skill.split("## Resource map", 1)[1]
        paths = sorted(set(re.findall(r"`((?:references|assets|scripts|tests)/[^`\s]+)`", resource_map)))
        self.assertGreaterEqual(len(paths), 8)
        for relative_path in paths:
            with self.subTest(path=relative_path):
                self.assertTrue((SKILL_ROOT / relative_path).exists(), relative_path)

    def test_skill_frontmatter_declares_mit_license(self) -> None:
        skill = self.read("SKILL.md")
        front_matter = skill.split("---\n", 2)[1]
        self.assertIn("license: MIT", front_matter)
        for relative in ("SKILL.md", "README.md", "references/THIRD_PARTY_NOTICES.md", "references/source-manifest.md"):
            with self.subTest(file=relative):
                text = self.read(relative)
                for forbidden in ("Synit Repository License", "SRL", "source-available", "internal-use"):
                    self.assertNotIn(forbidden, text)

    def test_documented_top_level_order_matches_code(self) -> None:
        guide = self.read("references/authoring-guide.md")
        block = re.search(r"```yaml\n# yaml-language-server[^\n]*\n(.*?)```", guide, re.DOTALL)
        assert block is not None
        keys = [line.split(":", 1)[0] for line in block.group(1).split("\n") if line and not line.startswith(" ")]
        self.assertEqual(keys, validator.TOP_LEVEL_ORDER)
        for relative in ("SKILL.md", "README.md"):
            with self.subTest(file=relative):
                self.assertNotIn("budget, budgets, flavors", self.read(relative))

    def test_documented_toolset_types_match_code_and_core_schema(self) -> None:
        guide = self.read("references/authoring-guide.md")
        block = re.search(r"toolset types include:\n\n```text\n(.*?)```", guide, re.DOTALL)
        assert block is not None
        documented = {item.strip() for item in block.group(1).replace("\n", " ").split(",") if item.strip()}
        self.assertEqual(documented, validator.TOOLSET_TYPES)
        core_schema = json.loads(self.read("references/core-schema.json"))
        self.assertEqual(set(core_schema["definitions"]["toolset"]["properties"]["type"]["enum"]), validator.TOOLSET_TYPES)

    def test_snapshot_version_is_consistent(self) -> None:
        version = validator.BUNDLED_SNAPSHOT_VERSION
        core_schema = json.loads(self.read("references/core-schema.json"))
        self.assertEqual(sources.schema_latest_version(core_schema), version)
        for template in sorted((SKILL_ROOT / "assets" / "templates").glob("*.yaml")):
            with self.subTest(template=template.name):
                self.assertIn(f'version: "{version}"', template.read_text(encoding="utf-8"))
        guide = self.read("references/authoring-guide.md")
        versions = set(re.findall(r'version: "(\d+)"', guide))
        self.assertEqual(versions, {version})
        self.assertNotIn(f"`{version}`", self.read("SKILL.md"))

    def test_documented_flags_exist(self) -> None:
        parser = validator.build_parser()
        flags = {action.option_strings[0] for action in parser._actions if action.option_strings}
        flags.discard("-h")
        readme = self.read("README.md")
        for flag in flags:
            with self.subTest(flag=flag):
                self.assertIn(f"`{flag}", readme)
        skill = self.read("SKILL.md")
        for flag in ("--allow-legacy-version", "--docker-check off", "--offline"):
            self.assertIn(flag, skill)

    def test_review_commands_do_not_use_fix(self) -> None:
        skill = self.read("SKILL.md")
        review = skill.split("Review or diagnose", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
        self.assertNotIn("--fix", review)
        self.assertIn("--docker-check off", review)
        self.assertIn("--allow-legacy-version", review)

    def test_gitignore_excludes_source_caches(self) -> None:
        ignore = self.read(".gitignore").split("\n")
        for pattern in ("references/llms.txt", "references/agent-schema.json", "references/*.meta.json", "__pycache__/", "*.pyc"):
            self.assertIn(pattern, ignore)
        self.assertFalse(any(pattern.strip() == "references/core-schema.json" for pattern in ignore))

    def test_requirements_enable_uri_format_checks(self) -> None:
        requirements = self.read("scripts/requirements.txt")
        self.assertIn("jsonschema[format-nongpl]>=4.22,<5", requirements)


if __name__ == "__main__":
    unittest.main(verbosity=2)
