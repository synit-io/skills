#!/usr/bin/env python3
"""Refresh the official Docker Agent schema and llms.txt cache.

This script intentionally uses only the Python standard library so it can be
run before installing the validator's optional Python dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _sources as sources  # noqa: E402

SCHEMA_URL = sources.OFFICIAL_SCHEMA_URL
LLMS_URL = sources.OFFICIAL_LLMS_URL
DEFAULT_CACHE_DIR = sources.DEFAULT_CACHE_DIR


def download(url: str, timeout: float) -> bytes:
    return sources.fetch_bytes(url, timeout)


def validate_schema(data: bytes) -> tuple[dict[str, Any], Optional[str]]:
    try:
        schema = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Downloaded schema is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError("Downloaded schema is not a JSON object")
    if not sources.is_official_schema(schema):
        raise ValueError(
            "Downloaded schema does not identify itself as the Docker Agent Configuration schema"
        )
    return schema, sources.schema_latest_version(schema)


def refresh_sources(
    *,
    cache_dir: Path,
    vendor_dir: Optional[Path],
    timeout: float,
    schema_only: bool,
    llms_only: bool,
) -> dict[str, Any]:
    """Download the requested sources, then replace the cache files atomically.

    Returns ``{"sources": {name: metadata}, "notices": [...]}``. Notices report
    digest changes relative to the previously cached copy.
    """
    prepared: list[tuple[str, bytes, dict[str, Any], str, str]] = []

    if not llms_only:
        schema_bytes = download(SCHEMA_URL, timeout)
        _, version = validate_schema(schema_bytes)
        metadata = sources.source_metadata(
            schema_bytes,
            SCHEMA_URL,
            extra={"latest_version": version},
        )
        prepared.append(
            ("schema", schema_bytes, metadata, sources.SCHEMA_FILENAME, sources.SCHEMA_META_FILENAME)
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
        metadata = sources.source_metadata(llms_bytes, LLMS_URL)
        prepared.append(("llms", llms_bytes, metadata, sources.LLMS_FILENAME, sources.LLMS_META_FILENAME))

    results: dict[str, Any] = {}
    notices: list[str] = []
    for name, data, metadata, data_name, metadata_name in prepared:
        previous = sources.read_cached_metadata(cache_dir / metadata_name)
        notice = sources.digest_change_notice(previous, metadata, label=f"official {name} source")
        if notice:
            notices.append(notice)
        metadata_bytes = sources.encode_metadata(metadata)
        sources.atomic_write(cache_dir / data_name, data)
        sources.atomic_write(cache_dir / metadata_name, metadata_bytes)
        if vendor_dir:
            sources.atomic_write(vendor_dir / data_name, data)
            sources.atomic_write(vendor_dir / metadata_name, metadata_bytes)
        results[name] = metadata
    return {"sources": results, "notices": notices}


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
    parser.add_argument("--timeout", type=float, default=30.0, help="Download timeout in seconds")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--schema-only", action="store_true", help="Refresh only agent-schema.json")
    selection.add_argument("--llms-only", action="store_true", help="Refresh only llms.txt")
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cache_dir = args.cache_dir.expanduser().resolve()
    vendor_dir = args.vendor_dir.expanduser().resolve() if args.vendor_dir else None
    try:
        outcome = refresh_sources(
            cache_dir=cache_dir,
            vendor_dir=vendor_dir,
            timeout=args.timeout,
            schema_only=args.schema_only,
            llms_only=args.llms_only,
        )
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"Refresh failed: {exc}", file=sys.stderr)
        return 2

    for notice in outcome["notices"]:
        print(f"Notice: {notice}", file=sys.stderr)

    if args.json:
        print(json.dumps(outcome, indent=2, sort_keys=True))
    else:
        for name, metadata in outcome["sources"].items():
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
