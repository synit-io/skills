#!/usr/bin/env python3
"""Refresh the official Docker Agent schema and llms.txt cache.

This script intentionally uses only the Python standard library so it can be
run before installing the validator's optional Python dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_URL = "https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json"
LLMS_URL = "https://docker.github.io/docker-agent/llms.txt"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "docker-agent-builder"


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def download(url: str, timeout: float) -> bytes:
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


def latest_version(schema: dict[str, Any]) -> str | None:
    try:
        values = schema["properties"]["version"]["enum"]
    except (KeyError, TypeError):
        return None
    if not isinstance(values, list):
        return None
    as_strings = [str(value) for value in values]
    numeric = [int(value) for value in as_strings if value.isdigit()]
    if numeric:
        return str(max(numeric))
    return as_strings[-1] if as_strings else None


def validate_schema(data: bytes) -> tuple[dict[str, Any], str | None]:
    try:
        schema = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Downloaded schema is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError("Downloaded schema is not a JSON object")
    if schema.get("title") != "Docker Agent Configuration":
        raise ValueError("Downloaded schema is not titled 'Docker Agent Configuration'")
    return schema, latest_version(schema)


def source_metadata(
    data: bytes,
    url: str,
    *,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": url,
        "fetched_at": now_utc(),
        "sha256": digest(data),
        "bytes": len(data),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return metadata


def refresh_sources(
    *,
    cache_dir: Path,
    vendor_dir: Path | None,
    timeout: float,
    schema_only: bool,
    llms_only: bool,
) -> dict[str, Any]:
    prepared: list[tuple[str, bytes, dict[str, Any], str, str]] = []

    if not llms_only:
        schema_bytes = download(SCHEMA_URL, timeout)
        _, version = validate_schema(schema_bytes)
        metadata = source_metadata(
            schema_bytes,
            SCHEMA_URL,
            extra_metadata={"latest_version": version},
        )
        prepared.append(
            ("schema", schema_bytes, metadata, "agent-schema.json", "agent-schema.meta.json")
        )

    if not schema_only:
        llms_bytes = download(LLMS_URL, timeout)
        try:
            llms_text = llms_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Downloaded llms.txt is not valid UTF-8: {exc}") from exc
        if not llms_text.lstrip().startswith("# Docker Agent") or (
            "https://docker.github.io/docker-agent/" not in llms_text
        ):
            raise ValueError("Downloaded llms.txt does not look like Docker Agent documentation")
        metadata = source_metadata(llms_bytes, LLMS_URL)
        prepared.append(("llms", llms_bytes, metadata, "llms.txt", "llms.meta.json"))

    results: dict[str, Any] = {}
    for name, data, metadata, data_name, metadata_name in prepared:
        metadata_bytes = (
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        atomic_write(cache_dir / data_name, data)
        atomic_write(cache_dir / metadata_name, metadata_bytes)
        if vendor_dir:
            atomic_write(vendor_dir / data_name, data)
            atomic_write(vendor_dir / metadata_name, metadata_bytes)
        results[name] = metadata
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh cached official Docker Agent schema and llms.txt sources."
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Cache destination (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--vendor-dir",
        type=Path,
        help="Optionally copy refreshed sources into a directory for an audited snapshot",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--schema-only", action="store_true")
    selection.add_argument("--llms-only", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cache_dir = args.cache_dir.expanduser().resolve()
    vendor_dir = args.vendor_dir.expanduser().resolve() if args.vendor_dir else None
    try:
        results = refresh_sources(
            cache_dir=cache_dir,
            vendor_dir=vendor_dir,
            timeout=args.timeout,
            schema_only=args.schema_only,
            llms_only=args.llms_only,
        )
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"Refresh failed: {exc}", file=__import__("sys").stderr)
        return 2

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for name, metadata in results.items():
            details = [
                f"{name}: {metadata['source']}",
                f"  SHA-256: {metadata['sha256']}",
                f"  Bytes: {metadata['bytes']}",
                f"  Fetched: {metadata['fetched_at']}",
            ]
            if metadata.get("latest_version"):
                details.append(f"  Latest config version: {metadata['latest_version']}")
            print("\n".join(details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
