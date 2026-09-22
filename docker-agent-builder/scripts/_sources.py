"""Shared helpers for retrieving and caching official Docker Agent sources.

Both ``validate_agent_yaml.py`` and ``refresh_official_sources.py`` import this
module from the script directory. It deliberately uses only the Python standard
library so the refresh script can run before the validator's third-party
dependencies are installed.

Cache integrity note: the metadata written next to a cached file records the
SHA-256 digest of the bytes that were downloaded. Comparing the file against
that digest detects local corruption or accidental edits of the cache. It does
not prove that the download itself was authentic; that trust rests on the TLS
connection to the official URL at download time.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

OFFICIAL_SCHEMA_URL = (
    "https://raw.githubusercontent.com/docker/docker-agent/main/agent-schema.json"
)
OFFICIAL_LLMS_URL = "https://docker.github.io/docker-agent/llms.txt"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "docker-agent-builder"

SCHEMA_FILENAME = "agent-schema.json"
SCHEMA_META_FILENAME = "agent-schema.meta.json"
LLMS_FILENAME = "llms.txt"
LLMS_META_FILENAME = "llms.meta.json"

OFFICIAL_SCHEMA_TITLE = "Docker Agent Configuration"
USER_AGENT = "docker-agent-builder/1.0"
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024


class InsecureRedirect(urllib.error.URLError):
    """Raised when a download would be redirected away from HTTPS."""


class ResponseTooLarge(urllib.error.URLError):
    """Raised when a download exceeds the configured size limit."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temporary file in the same directory.

    An existing file keeps its permission bits.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode: Optional[int] = None
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = None
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False)
    temp_name = handle.name
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def is_official_schema(schema: Mapping[str, Any]) -> bool:
    """Return True when ``schema`` presents itself as Docker's agent schema.

    This is a plausibility check on self-declared fields, not an authenticity
    proof: any document can copy these values.
    """
    title = str(schema.get("title", ""))
    schema_id = str(schema.get("$id", ""))
    description = str(schema.get("description", ""))
    return title == OFFICIAL_SCHEMA_TITLE and (
        "docker/docker-agent" in schema_id or "Docker Agent" in description
    )


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


def source_metadata(
    data: bytes,
    url: str,
    *,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the metadata record stored next to a cached source file."""
    metadata: dict[str, Any] = {
        "source": url,
        "fetched_at": utc_now(),
        "sha256": sha256_bytes(data),
        "bytes": len(data),
    }
    if extra:
        metadata.update(extra)
    return metadata


def encode_metadata(metadata: Mapping[str, Any]) -> bytes:
    return (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_metadata(path: Path) -> dict[str, Any]:
    """Read a metadata file; raises OSError or ValueError on any problem."""
    parsed = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return parsed


def read_cached_metadata(path: Path) -> Optional[dict[str, Any]]:
    """Best-effort read of existing cache metadata; None when absent or invalid."""
    try:
        return load_metadata(path)
    except (OSError, ValueError):
        return None


def cache_age_hours(metadata: Mapping[str, Any], *, now: Optional[datetime] = None) -> Optional[float]:
    """Age of a cache entry in hours, or None when ``fetched_at`` is unusable."""
    fetched_at = metadata.get("fetched_at")
    if not isinstance(fetched_at, str):
        return None
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - fetched).total_seconds() / 3600.0


def digest_change_notice(
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
    *,
    label: str,
) -> Optional[str]:
    """Describe a digest change between two metadata records, if any."""
    if not previous:
        return None
    old_digest = previous.get("sha256")
    new_digest = current.get("sha256")
    if not isinstance(old_digest, str) or old_digest == new_digest:
        return None
    old_version = previous.get("latest_version") or "unknown"
    new_version = current.get("latest_version") or "unknown"
    old_time = previous.get("fetched_at") or "unknown time"
    return (
        f"The {label} changed since the previous download: "
        f"sha256 {old_digest[:12]} (latest config version {old_version}, fetched {old_time}) "
        f"-> {str(new_digest)[:12]} (latest config version {new_version})."
    )


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise InsecureRedirect(
                f"refusing to follow a redirect from {req.full_url} to a non-HTTPS URL"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(request: urllib.request.Request, timeout: float) -> Any:
    """Open ``request`` with an opener that refuses HTTPS-to-HTTP redirects."""
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
    return opener.open(request, timeout=timeout)


def python_has_ca_certificates() -> bool:
    """Return True when Python's default TLS context loaded any CA certificate."""
    try:
        return bool(ssl.create_default_context().get_ca_certs())
    except Exception:
        return False


def _is_certificate_error(error: BaseException) -> bool:
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason)


def curl_fallback_allowed(error: BaseException) -> bool:
    """Decide whether a failed urllib download may be retried with curl.

    HTTP status errors, refused redirects, and oversized responses are final.
    A certificate verification failure is retried only when Python itself has
    no CA certificates loaded (a missing local trust store rather than a bad
    server certificate); curl still verifies the certificate with its own
    trust store. Every other failure is treated as connection-level.
    """
    if isinstance(error, (urllib.error.HTTPError, InsecureRedirect, ResponseTooLarge)):
        return False
    if _is_certificate_error(error):
        return not python_has_ca_certificates()
    return True


def _as_url_error(error: BaseException) -> urllib.error.URLError:
    if isinstance(error, urllib.error.URLError):
        return error
    wrapped = urllib.error.URLError(f"{type(error).__name__}: {error}")
    wrapped.__cause__ = error
    return wrapped


def _fetch_with_curl(
    url: str,
    timeout: float,
    max_bytes: int,
    urllib_error: BaseException,
) -> bytes:
    curl = shutil.which("curl")
    if not curl:
        raise _as_url_error(urllib_error)
    try:
        result = subprocess.run(
            [
                curl,
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-filesize",
                str(max_bytes),
                "--max-time",
                str(timeout),
                "--user-agent",
                USER_AGENT,
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
    if len(result.stdout) > max_bytes:
        raise ResponseTooLarge(f"response from {url} exceeds {max_bytes} bytes")
    return result.stdout


def fetch_bytes(url: str, timeout: float, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """Download ``url`` over HTTPS with a size limit and verified TLS.

    Raises ``urllib.error.URLError`` (or a subclass) on every failure.
    """
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise urllib.error.URLError(f"only HTTPS downloads are allowed: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with open_url(request, timeout) as response:
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        if not curl_fallback_allowed(error):
            raise _as_url_error(error)
        return _fetch_with_curl(url, timeout, max_bytes, error)
    if len(data) > max_bytes:
        raise ResponseTooLarge(f"response from {url} exceeds {max_bytes} bytes")
    return data
