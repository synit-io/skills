#!/usr/bin/env python3
"""Regression tests for the Docker Agent validator."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import refresh_official_sources as refresher
import validate_agent_yaml as validator

HERE = Path(__file__).resolve().parent
VALIDATOR = HERE / "validate_agent_yaml.py"
SKILL_ROOT = HERE.parent
SCHEMA_COMMENT = (
    "# yaml-language-server: $schema="
    "https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json"
)


class ValidatorTests(unittest.TestCase):
    maxDiff = None

    def run_validator(
        self,
        yaml_text: str,
        *extra: str,
        filename: str = "agent.yaml",
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None, Path, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / filename
        path.write_text(yaml_text, encoding="utf-8")
        command = [
            sys.executable,
            str(VALIDATOR),
            str(path),
            "--schema-mode",
            "core",
            "--docker-check",
            "off",
            "--json",
            *extra,
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        payload: dict[str, Any] | None = None
        if completed.stdout.strip():
            try:
                decoded = json.loads(completed.stdout)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                payload = None
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
        payload = json.loads(completed.stdout) if completed.stdout.strip() else None
        return completed, payload

    def assert_issue(self, payload: dict[str, Any], code: str) -> None:
        codes = [item.get("code") for item in payload.get("issues", [])]
        self.assertIn(code, codes, msg=f"Expected {code!r}; found {codes!r}")

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
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["status"], "pass")
        rendered = path.read_text(encoding="utf-8")
        self.assertTrue(rendered.startswith(SCHEMA_COMMENT + "\n\nversion: \"16\"\n"))
        self.assertIn("agents:\n  root:\n    model: openai/gpt-5-mini\n    description: General assistant", rendered)
        self.assertTrue(rendered.endswith("\n"))
        self.assertFalse(any(line.rstrip() != line for line in rendered.splitlines()))

    def test_duplicate_key_fails_at_yaml_gate(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    model: anthropic/claude-sonnet-4-5
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assert_issue(payload, "parse")
        messages = [item.get("message", "") for item in payload.get("issues", [])]
        self.assertTrue(any("Duplicate YAML key" in message for message in messages))

    def test_unknown_sub_agent_fails_semantic_gate(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: Delegating assistant
    sub_agents:
      - missing-agent
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "unknown-agent")

    def test_literal_secret_fails_security_gate(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    toolsets:
      - type: mcp
        command: docker
        args:
          - mcp
          - gateway
          - run
        env:
          API_KEY: sk-abcdefghijklmnopqrstuvwxyz123456
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "literal-sensitive-value")

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
        self.assertTrue(
            any(item.get("gate") == "schema" for item in payload.get("issues", [])),
            payload,
        )

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

    def test_bare_primary_model_name_is_allowed(self) -> None:
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
        self.assertFalse(
            any(item.get("code") == "unknown-model" for item in payload.get("issues", [])),
            payload,
        )

    def test_formatter_replaces_relative_schema_comment(self) -> None:
        text = """# yaml-language-server: $schema=../agent-schema.json

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
"""
        completed, _, path, temporary = self.run_validator(text, "--fix")
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        rendered = path.read_text(encoding="utf-8")
        self.assertEqual(rendered.count("yaml-language-server:"), 1)
        self.assertTrue(rendered.startswith(SCHEMA_COMMENT + "\n"))
        self.assertNotIn("../agent-schema.json", rendered)

    def test_formatter_preserves_instruction_scalar_value(self) -> None:
        instruction = (
            "This is a deliberately long one-line instruction whose exact value must remain "
            "unchanged while the formatter chooses a readable block scalar representation."
        )
        text = f"""{SCHEMA_COMMENT}

version: \"16\"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    instruction: {instruction}
"""
        completed, _, path, temporary = self.run_validator(text, "--fix")
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        sys.path.insert(0, str(HERE))
        try:
            import validate_agent_yaml as validator
            parsed = validator.parse_yaml(path.read_text(encoding="utf-8"), str(path))
        finally:
            sys.path.pop(0)
        self.assertEqual(str(parsed["agents"]["root"]["instruction"]), instruction)

    def test_formatter_preserves_schema_directive_text_inside_instruction(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    instruction: |
      Keep this literal line:
      # yaml-language-server: $schema=../agent-schema.json
"""
        completed, _, path, temporary = self.run_validator(text, "--fix")
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        rendered = path.read_text(encoding="utf-8")
        self.assertIn("      # yaml-language-server: $schema=../agent-schema.json", rendered)

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
        self.assertFalse(
            any(item.get("code") == "unknown-tool-type" for item in payload["issues"]),
            payload,
        )

    def test_tool_types_are_derived_from_official_schema_shape(self) -> None:
        schema = {
            "definitions": {
                "Toolset": {
                    "properties": {
                        "type": {"enum": ["think", "file", "future-tool"]}
                    }
                }
            }
        }
        self.assertEqual(
            validator.schema_toolset_types(schema),
            {"think", "file", "future-tool"},
        )

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

    def test_sensitive_value_cannot_mix_literal_and_interpolation(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    toolsets:
      - type: mcp
        command: example
        env:
          API_KEY: actual-secret-${{env.NOOP}}
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "literal-sensitive-value")

    def test_sensitive_value_requires_env_namespace(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    toolsets:
      - type: mcp
        command: example
        env:
          GITHUB_TOKEN: "${{GITHUB_TOKEN}}"
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "literal-sensitive-value")

    def test_bearer_env_reference_is_allowed(self) -> None:
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
            Authorization: "Bearer ${{env.SERVICE_TOKEN}}"
"""
        completed, payload, _, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        assert payload is not None
        self.assertIn("SERVICE_TOKEN", payload["required_environment"])

    def test_bom_and_indentation_fail_then_fix(self) -> None:
        text = f"\ufeff{SCHEMA_COMMENT}\n\nversion: \"16\"\nagents:\n    root:\n        model: openai/gpt-5-mini\n        description: General assistant\n"
        completed, payload, path, temporary = self.run_validator(text)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1)
        assert payload is not None
        self.assert_issue(payload, "utf8-bom")
        self.assert_issue(payload, "indentation")
        completed, payload = self.run_existing(path, "--fix")
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_missing_instruction_file_fails(self) -> None:
        text = f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    instruction_file: missing.md
"""
        completed, payload, _, temporary = self.run_validator(text)
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
            agent_path.write_text(
                f"""{SCHEMA_COMMENT}

version: "16"
agents:
  root:
    model: openai/gpt-5-mini
    description: General assistant
    instruction_file: instructions.md
""",
                encoding="utf-8",
            )
            completed, payload = self.run_existing(agent_path)
            self.assertEqual(completed.returncode, 1)
            assert payload is not None
            self.assert_issue(payload, "unsafe-instruction-file")

    def test_runtime_environment_classifier_is_conservative(self) -> None:
        self.assertTrue(
            validator.is_environment_only_failure(
                "The following environment variables must be set: OPENAI_API_KEY"
            )
        )
        self.assertFalse(
            validator.is_environment_only_failure(
                "configuration invalid: unknown field; API key is required"
            )
        )

    def test_corrupted_official_cache_is_rejected(self) -> None:
        schema = {
            "title": "Docker Agent Configuration",
            "description": "Docker Agent schema",
            "properties": {"version": {"enum": ["16"]}},
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            schema_bytes = json.dumps(schema).encode("utf-8")
            (cache / "agent-schema.json").write_bytes(schema_bytes)
            (cache / "agent-schema.meta.json").write_text(
                json.dumps(
                    {
                        "source": validator.OFFICIAL_SCHEMA_URL,
                        "sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(validator.ValidationIncomplete):
                validator.load_schema(
                    mode="official",
                    cache_dir=cache,
                    schema_path=None,
                    offline=True,
                    refresh=False,
                    timeout=1,
                )

    def test_explicit_schema_is_not_labeled_official(self) -> None:
        schema = {
            "title": "Docker Agent Configuration",
            "description": "Docker Agent schema",
            "properties": {"version": {"enum": ["16"]}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema.json"
            path.write_text(json.dumps(schema), encoding="utf-8")
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

    def test_source_refresh_failure_preserves_existing_cache(self) -> None:
        schema = {
            "title": "Docker Agent Configuration",
            "properties": {"version": {"enum": ["16"]}},
        }
        schema_bytes = json.dumps(schema).encode("utf-8")
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
                refresher.refresh_sources(
                    cache_dir=cache,
                    vendor_dir=None,
                    timeout=1,
                    schema_only=False,
                    llms_only=False,
                )
            for name, data in old_files.items():
                self.assertEqual((cache / name).read_bytes(), data)

    def test_refresh_accepts_current_llms_index_shape(self) -> None:
        schema = {
            "title": "Docker Agent Configuration",
            "properties": {"version": {"enum": ["16"]}},
        }
        llms = (
            b"# Docker Agent\n\n"
            b"- [Configuration](https://docker.github.io/docker-agent/configuration/overview/)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            with mock.patch.object(
                refresher,
                "download",
                side_effect=[json.dumps(schema).encode("utf-8"), llms],
            ):
                results = refresher.refresh_sources(
                    cache_dir=cache,
                    vendor_dir=None,
                    timeout=1,
                    schema_only=False,
                    llms_only=False,
                )
            self.assertEqual(set(results), {"schema", "llms"})
            self.assertEqual((cache / "llms.txt").read_bytes(), llms)

    def test_official_fetch_uses_verified_curl_fallback(self) -> None:
        for module, function_name in (
            (refresher, "download"),
            (validator, "fetch_bytes"),
        ):
            with self.subTest(module=module.__name__):
                with mock.patch.object(
                    module.urllib.request,
                    "urlopen",
                    side_effect=module.urllib.error.URLError("certificate verify failed"),
                ), mock.patch.object(module.shutil, "which", return_value="/usr/bin/curl"), mock.patch.object(
                    module.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        args=["curl"],
                        returncode=0,
                        stdout=b"verified-content",
                        stderr=b"",
                    ),
                ):
                    result = getattr(module, function_name)("https://example.invalid/source", 1)
                self.assertEqual(result, b"verified-content")

    def test_source_manifest_hashes_match_files(self) -> None:
        manifest = (SKILL_ROOT / "references" / "source-manifest.md").read_text(
            encoding="utf-8"
        )
        matches = re.findall(
            r"`(references/[^`]+)`[^\n]*SHA-256 `([0-9a-f]{64})`",
            manifest,
        )
        self.assertGreaterEqual(len(matches), 3)
        for relative_path, expected in matches:
            with self.subTest(file=relative_path):
                actual = hashlib.sha256((SKILL_ROOT / relative_path).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)

    def test_skill_resource_map_paths_exist(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        resource_map = skill.split("## Resource map", 1)[1]
        paths = re.findall(r"^- `([^`]+)`", resource_map, flags=re.MULTILINE)
        self.assertGreaterEqual(len(paths), 8)
        for relative_path in paths:
            with self.subTest(path=relative_path):
                self.assertTrue((SKILL_ROOT / relative_path).exists(), relative_path)

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

    def test_bundled_templates_pass_core_validation(self) -> None:
        templates = sorted((SKILL_ROOT / "assets" / "templates").glob("*.yaml"))
        self.assertGreaterEqual(len(templates), 5)
        for source in templates:
            with self.subTest(template=source.name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / source.name
                target.write_bytes(source.read_bytes())
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(VALIDATOR),
                        str(target),
                        "--fix",
                        "--schema-mode",
                        "core",
                        "--docker-check",
                        "off",
                        "--json",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"{source.name}: {completed.stderr}\n{completed.stdout}",
                )
                payload = json.loads(completed.stdout)
                self.assertEqual(payload["status"], "pass", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
