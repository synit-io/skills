#!/usr/bin/env python3
"""Matrix42 ESM Public API CLI for helpdesk agent workflows.

Stateless subcommands, JSON output. Config: env M42_BASE_URL/M42_API_TOKEN
or the m42_config.json written by `setup` (see resolve_config_path).
"""
import argparse
import copy
import html
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import stat
import string
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE_NAME = "m42_config.json"
# Pre-XDG location next to this script. Still honored when the file exists so
# existing installations keep working; new configs are written elsewhere.
LEGACY_CONFIG_PATH = os.path.join(SCRIPT_DIR, CONFIG_FILE_NAME)

# Data definitions (stable, schema-level)
DD_ACTIVITY = "SPSActivityClassBase"
DD_COMMON = "SPSCommonClassBase"
DD_JOURNAL = "SPSActivityClassUnitOfWork"
DD_TIME_TRACKING = "SPSActivityClassTimeTracking"
DD_TIME_TRACKING_CONFIG = "SPSGlobalConfigurationClassTimeTracking"
DD_TIME_ACTIVITY_TYPE = "SVMActivityPickupActivityType"
DD_USER = "SPSUserClassBase"
DD_ACCOUNT = "SPSAccountClassBase"
DD_CATEGORY = "SPSScCategoryClassBase"
DD_KB = "SVMKBArticleClassBase"
DD_ANNOUNCEMENT = "SVMAnnouncementClassBase"
DD_CHANGE = "SVMActivityClassChange"
DD_STATE = "SPSCommonPickupObjectStatus"
DD_URGENCY = "SVMActivityPickupUrgency"
DD_IMPACT = "SVMActivityPickupImpact"
DD_CLOSE_REASON = "SPSCommonPickupObjectStateReason"
DD_JOURNAL_TYPE = "SPSJournalEntryPickupType"
DD_SECURITY_ROLE = "SPSSecurityClassRole"
DD_ATTACHMENT = "SPSActivityClassAttachment"
DD_SERVICE = "SPSArticleClassBase"
DD_ASSET = "SPSAssetClassBase"

CI_INCIDENT = "SPSActivityTypeIncident"
CI_TICKET = "SPSActivityTypeTicket"
CI_PROBLEM = "SPSActivityTypeProblem"

JOURNAL_COMMENT_ACTION = 0  # ActivityAction 0 = plain comment entry
STATE_SEMANTICS = (
    "new", "assigned", "in_progress", "paused", "planned", "solved", "closed"
)
TICKET_FAMILIES = {"incident", "service_request", "ticket", "task", "problem"}
EMPTY_TENANT_PROFILE = {
    "schema_version": 1,
    "state_group": None,
    "states": {},
    "urgency": {},
    "urgency_default": None,
    "impact_default": None,
    "close_reasons": {},
    "journal_actions": {},
    "ticket_prefixes": {},
    "roles": {},
    "role_assignment_attribute": None,
    "portal_url_template": None,
    "behavior": {
        "auto_recipient_states": [],
        "auto_recipient_on_close": [],
        "auto_recipient_on_reopen": False,
        "forward_state": None,
        "forward_preserve_states": [],
        "reopen_state": None,
        "default_comment_visibility": None,
        "preclose_state_by_family": {},
        "processed_journal_families": [],
        "state_close_fallback_families": [],
        "comment_language_mode": None,
        "operator_language": None,
        "close_questions": [],
    },
}

STATE_INPUT_ALIASES = {
    alias: semantic
    for semantic in STATE_SEMANTICS
    for alias in (semantic, semantic.replace("_", " "))
}
PORTABLE_JOURNAL_TEXT = {
    "takeover": "State changed: in progress.",
    "pause": "State changed: paused.",
    "resume": "Ticket resumed.",
    "solved": "State changed: solved.",
    "state_change": "Ticket state changed.",
    "processed": "Ticket processed.",
    "forward_user": "Ticket forwarded to user.",
    "forward_role": "Ticket forwarded to role.",
    "reopen": "Ticket reopened.",
    "close": "Ticket closed.",
    "close_task": "Task closed.",
}
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TICKET_NUMBER_RE = re.compile(r"^([^\d\s]+)(\d+)$")
TICKET_PREFIX_RE = re.compile(r"[^\d\s]+")  # same prefix class as TICKET_NUMBER_RE
DD_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
PLACEHOLDER_RE = re.compile(r"<[^<>]+>")
RESERVED_EXAMPLE_DOMAINS = ("example.com", "example.net", "example.org")

JOURNAL_COLUMNS = ("ID,CreatedDate,ActivityAction,Creator.ID as CreatorId,"
                   "OriginalSolutionHtml,VisibleInPortal")
MAX_RECORDS_CEILING = 10000
MAX_WORK_MINUTES = 1440
PRIORITY_RANGE = (0, 99)
# Idempotent GETs only: bounded backoff on throttling, gateway errors, timeouts.
RETRYABLE_GET_STATUS = frozenset({429, 502, 503, 504})
GET_RETRY_DELAYS = (0.5, 2.0)
WORK_TIME_RETRY_HINT = (
    "work time is already recorded; do NOT book it again - retry close-ticket "
    "with --work-minutes 0 after inspecting the ticket"
)


class M42Error(Exception):
    """Operational failure reported as JSON. `extra` fields join the output."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


class M42HttpError(M42Error):
    """HTTP status failure; keeps the status code for retry decisions."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class M42TimeoutError(M42Error):
    """The request timed out; a mutation may or may not have been applied."""


class FragmentRows(list):
    """Fragment rows plus a flag telling whether more rows may exist."""

    truncated = False


def asql_quote(value):
    """Escape a value for single-quoted ASQL literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _plain_text_value(value):
    """Normalize literal text while preserving its line-oriented formatting."""
    text = "" if value is None else str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _plain_text_field(value):
    """Keep rich-text API fields literal: no formatting tags, raw newlines kept."""
    return html.escape(_plain_text_value(value), quote=False)


def _work_minutes_error(minutes):
    """Return why a work-duration answer is unusable, or None when it is valid."""
    if not math.isfinite(minutes) or minutes < 0:
        return "must be a finite number at least 0"
    if minutes > MAX_WORK_MINUTES:
        return (f"must not exceed {MAX_WORK_MINUTES} minutes (24 hours) per close; "
                "book longer work in Matrix42 time tracking")
    return None


def _nonnegative_minutes(value):
    """Argparse type for an explicit close-time work-duration answer."""
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number of minutes")
    problem = _work_minutes_error(minutes)
    if problem:
        raise argparse.ArgumentTypeError(problem)
    return minutes


def _max_records_arg(value):
    """Argparse type for --max: a row limit within the documented ceiling."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer")
    if limit < 1 or limit > MAX_RECORDS_CEILING:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_RECORDS_CEILING}")
    return limit


def _priority_arg(value):
    """Argparse type for --priority. No live priority inventory is available, so
    the value is only range-checked."""
    try:
        priority = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer")
    low, high = PRIORITY_RANGE
    if priority < low or priority > high:
        raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
    return priority


def parse_ticket_number(ticket_number):
    """Validate and normalize a tenant ticket number."""
    tn = str(ticket_number).strip().upper()
    match = TICKET_NUMBER_RE.match(tn)
    if not match or len(tn) > 64:
        raise M42Error(f"invalid ticket number format: {ticket_number!r} "
                       f"(expected a prefix followed by digits)")
    return tn


def is_guid(value):
    return bool(GUID_RE.match(str(value).strip()))


def validate_dd_name(name):
    """Data-definition and CI names are interpolated into URL paths."""
    text = str(name or "").strip()
    if not DD_NAME_RE.fullmatch(text):
        raise M42Error(
            f"invalid data definition name: {name!r} (expected letters, digits, "
            "and underscores, starting with a letter)"
        )
    return text


def _is_nonnegative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _int_or_raw(value):
    """Integer form of an API number such as 204 or "204"; other values pass through."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value.strip())
    return value


def _flag(value):
    """Boolean form of an API flag such as 1, "1", true, or "True"."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return bool(value)


def normalize_base_url(base_url):
    """Validate transport and append exactly one /m42Services path segment."""
    raw = str(base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise M42Error("--base-url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
        raise M42Error("--base-url must not contain credentials, parameters, query, or fragment")
    if parsed.scheme != "https":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise M42Error("--base-url must use HTTPS (HTTP is allowed only for localhost)")
    path = parsed.path.rstrip("/")
    if not path.lower().endswith("/m42services"):
        path += "/m42Services"
    return urllib.parse.urlunparse(parsed._replace(path=path))


def _validate_integer_map(profile, section, valid_keys=None, *, allow_none=False):
    override = profile.get(section, {})
    if not isinstance(override, dict):
        raise M42Error(f"tenant profile {section!r} must be a JSON object")
    for key, value in override.items():
        if valid_keys is not None and key not in valid_keys:
            raise M42Error(f"unknown tenant profile {section} key: {key!r}")
        if allow_none and value is None:
            continue
        if not _is_nonnegative_int(value):
            raise M42Error(
                f"tenant profile {section}.{key} must be a non-negative integer"
            )


def _reject_placeholders(value, path="tenant profile"):
    """An unedited example profile must never validate: refuse <...> markers."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and PLACEHOLDER_RE.search(key):
                raise M42Error(f"{path} still contains a placeholder key: {key!r}")
            _reject_placeholders(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_placeholders(item, f"{path}[{index}]")
    elif isinstance(value, str) and PLACEHOLDER_RE.search(value):
        raise M42Error(f"{path} still contains a placeholder value: {value!r}")


def _validate_portal_template(template):
    if not isinstance(template, str):
        raise M42Error(
            "tenant profile portal_url_template must contain {ticket_number} or be null"
        )
    try:
        fields = [
            (name, spec, conversion)
            for _text, name, spec, conversion in string.Formatter().parse(template)
            if name is not None
        ]
    except ValueError as e:
        raise M42Error(f"tenant profile portal_url_template has invalid braces: {e}")
    unknown = sorted({name for name, _spec, _conv in fields if name != "ticket_number"})
    if unknown or any(spec or conversion for _name, spec, conversion in fields):
        raise M42Error(
            "tenant profile portal_url_template may only use the {ticket_number} "
            f"placeholder; found: {unknown or 'format modifiers'}"
        )
    if not fields:
        raise M42Error(
            "tenant profile portal_url_template must contain {ticket_number} or be null"
        )
    parsed = urllib.parse.urlparse(template.replace("{ticket_number}", "ticket"))
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password):
        raise M42Error("tenant profile portal_url_template must be an HTTPS URL")
    host = parsed.hostname.lower().rstrip(".")
    if any(host == domain or host.endswith("." + domain)
           for domain in RESERVED_EXAMPLE_DOMAINS):
        raise M42Error(
            "tenant profile portal_url_template still points at a documentation "
            f"example host ({host}); use the tenant portal URL or null"
        )


def validate_tenant_profile(profile):
    """Validate operator-reviewed tenant values without tenant defaults."""
    if not isinstance(profile, dict):
        raise M42Error("tenant profile must be a JSON object")
    _reject_placeholders(profile)
    merged = copy.deepcopy(EMPTY_TENANT_PROFILE)
    allowed = set(merged)
    unknown = sorted(set(profile) - allowed)
    if unknown:
        raise M42Error(f"unknown tenant profile keys: {unknown}")
    if profile.get("schema_version", 1) != 1:
        raise M42Error("tenant profile schema_version must be 1")
    merged["schema_version"] = 1
    _validate_integer_map(
        profile, "states", set(STATE_SEMANTICS), allow_none=True
    )
    _validate_integer_map(
        profile, "urgency", {"low", "medium", "high"}, allow_none=True
    )
    _validate_integer_map(profile, "close_reasons")
    _validate_integer_map(
        profile, "journal_actions", set(PORTABLE_JOURNAL_TEXT), allow_none=True
    )
    for section in ("states", "urgency", "close_reasons", "journal_actions"):
        merged[section].update(profile.get(section, {}))
    semantics_by_value = {}
    for semantic, value in merged["states"].items():
        if value is not None:
            semantics_by_value.setdefault(value, []).append(semantic)
    shared = {value: sorted(names) for value, names in semantics_by_value.items()
              if len(names) > 1}
    if shared:
        raise M42Error(
            "tenant profile states must map each semantic to a distinct value; "
            f"shared values: {shared}"
        )
    urgency_default = profile.get("urgency_default")
    if urgency_default is not None and urgency_default not in merged["urgency"]:
        raise M42Error(
            "tenant profile urgency_default must name a configured urgency alias"
        )
    merged["urgency_default"] = urgency_default
    if "state_group" in profile:
        value = profile["state_group"]
        if value is not None and not _is_nonnegative_int(value):
            raise M42Error("tenant profile state_group must be null or a non-negative integer")
        merged["state_group"] = value
    if "impact_default" in profile:
        value = profile["impact_default"]
        if value is not None and not _is_nonnegative_int(value):
            raise M42Error(
                "tenant profile impact_default must be null or a non-negative integer"
            )
        merged["impact_default"] = value
    prefixes = profile.get("ticket_prefixes", {})
    if not isinstance(prefixes, dict):
        raise M42Error("tenant profile 'ticket_prefixes' must be a JSON object")
    for prefix, family in prefixes.items():
        if not isinstance(prefix, str) or not prefix.strip():
            raise M42Error("tenant profile ticket prefixes must be non-empty strings")
        normalized_prefix = prefix.strip().upper()
        if not TICKET_PREFIX_RE.fullmatch(normalized_prefix):
            raise M42Error(
                f"tenant profile ticket prefix {prefix!r} can never match a ticket "
                "number: a prefix is everything before the trailing digits and "
                "must not contain digits or whitespace"
            )
        if family is not None and family not in TICKET_FAMILIES:
            raise M42Error(
                f"tenant profile ticket_prefixes.{prefix} has unknown family {family!r}"
            )
        if merged["ticket_prefixes"].get(normalized_prefix, family) != family:
            raise M42Error(
                f"tenant profile ticket prefix {normalized_prefix!r} is configured "
                "more than once with different families"
            )
        merged["ticket_prefixes"][normalized_prefix] = family
    roles = profile.get("roles", {})
    if not isinstance(roles, dict):
        raise M42Error("tenant profile 'roles' must be a JSON object")
    for alias, role in roles.items():
        if not isinstance(alias, str) or not alias.strip() or not isinstance(role, dict):
            raise M42Error("tenant profile roles must map aliases to role objects")
        if not is_guid(role.get("id", "")):
            raise M42Error(f"tenant profile roles.{alias}.id must be a GUID")
        name = role.get("name")
        if not isinstance(name, str) or not name.strip():
            raise M42Error(f"tenant profile roles.{alias}.name must be non-empty")
        merged["roles"][alias] = {"id": role["id"].strip(), "name": name.strip()}
    role_attribute = profile.get("role_assignment_attribute")
    if role_attribute not in (None, "RecipientRole", "Recipient"):
        raise M42Error(
            "tenant profile role_assignment_attribute must be RecipientRole, Recipient, or null"
        )
    merged["role_assignment_attribute"] = role_attribute
    if merged["roles"] and role_attribute is None:
        raise M42Error(
            "tenant profile role_assignment_attribute is required when roles are configured"
        )
    portal_template = profile.get("portal_url_template")
    if portal_template is not None:
        _validate_portal_template(portal_template)
    merged["portal_url_template"] = portal_template
    behavior = profile.get("behavior", {})
    if not isinstance(behavior, dict):
        raise M42Error("tenant profile 'behavior' must be a JSON object")
    unknown_behavior = sorted(set(behavior) - set(merged["behavior"]))
    if unknown_behavior:
        raise M42Error(f"unknown tenant profile behavior keys: {unknown_behavior}")
    for key in ("auto_recipient_states",):
        values = behavior.get(key, [])
        if not isinstance(values, list) or any(v not in STATE_SEMANTICS for v in values):
            raise M42Error(f"tenant profile behavior.{key} must list state semantics")
        merged["behavior"][key] = list(dict.fromkeys(values))
    preserve_states = behavior.get("forward_preserve_states", [])
    if not isinstance(preserve_states, list) or any(
        value not in STATE_SEMANTICS for value in preserve_states
    ):
        raise M42Error(
            "tenant profile behavior.forward_preserve_states must list state semantics"
        )
    merged["behavior"]["forward_preserve_states"] = list(
        dict.fromkeys(preserve_states)
    )
    for key in (
        "auto_recipient_on_close",
        "processed_journal_families",
        "state_close_fallback_families",
    ):
        values = behavior.get(key, [])
        if not isinstance(values, list) or any(v not in TICKET_FAMILIES for v in values):
            raise M42Error(f"tenant profile behavior.{key} must list ticket families")
        merged["behavior"][key] = list(dict.fromkeys(values))
    reopen = behavior.get("auto_recipient_on_reopen", False)
    if not isinstance(reopen, bool):
        raise M42Error("tenant profile behavior.auto_recipient_on_reopen must be boolean")
    merged["behavior"]["auto_recipient_on_reopen"] = reopen
    for key in ("forward_state", "reopen_state"):
        semantic = behavior.get(key)
        if semantic is not None and semantic not in STATE_SEMANTICS:
            raise M42Error(
                f"tenant profile behavior.{key} must be a state semantic or null"
            )
        if semantic is not None and merged["states"].get(semantic) is None:
            raise M42Error(
                f"tenant profile behavior.{key} references an unmapped state {semantic!r}"
            )
        merged["behavior"][key] = semantic
    visibility = behavior.get("default_comment_visibility")
    if visibility not in (None, "portal", "internal"):
        raise M42Error(
            "tenant profile behavior.default_comment_visibility must be portal, internal, or null"
        )
    merged["behavior"]["default_comment_visibility"] = visibility
    preclose = behavior.get("preclose_state_by_family", {})
    if not isinstance(preclose, dict):
        raise M42Error(
            "tenant profile behavior.preclose_state_by_family must be a JSON object"
        )
    for family, semantic in preclose.items():
        if family not in TICKET_FAMILIES or semantic not in (*STATE_SEMANTICS, None):
            raise M42Error(
                "tenant profile preclose mappings must use known families and state semantics"
            )
    merged["behavior"]["preclose_state_by_family"] = dict(preclose)
    missing_preclose_states = sorted({
        semantic for semantic in preclose.values()
        if semantic is not None and merged["states"].get(semantic) is None
    })
    if missing_preclose_states:
        raise M42Error(
            "tenant profile preclose states lack state mappings: "
            f"{missing_preclose_states}"
        )
    invalid_processed = sorted(
        family for family in merged["behavior"]["processed_journal_families"]
        if preclose.get(family) is None
    )
    if invalid_processed:
        raise M42Error(
            "processed journal families require a preclose state: "
            f"{invalid_processed}"
        )
    language_mode = behavior.get("comment_language_mode")
    if language_mode not in (None, "initiator", "operator", "bilingual"):
        raise M42Error(
            "tenant profile behavior.comment_language_mode must be initiator, operator, bilingual, or null"
        )
    merged["behavior"]["comment_language_mode"] = language_mode
    operator_language = behavior.get("operator_language")
    if operator_language is not None and (
        not isinstance(operator_language, str) or not operator_language.strip()
    ):
        raise M42Error("tenant profile behavior.operator_language must be a string or null")
    merged["behavior"]["operator_language"] = operator_language
    if language_mode in ("operator", "bilingual") and not operator_language:
        raise M42Error(
            "tenant profile behavior.operator_language is required for selected comment mode"
        )
    questions = behavior.get("close_questions", [])
    if not isinstance(questions, list) or any(
        not isinstance(question, str) or not question.strip() for question in questions
    ):
        raise M42Error("tenant profile behavior.close_questions must list non-empty strings")
    merged["behavior"]["close_questions"] = questions
    return merged


def _profile_value(c, section, key=None):
    profile = getattr(c, "tenant_profile", EMPTY_TENANT_PROFILE)
    value = profile[section]
    if key is None:
        if value is None:
            raise M42Error(f"tenant profile has no configured {section}")
        return value
    if key not in value:
        available = sorted(value)
        raise M42Error(
            f"tenant profile has no {section} mapping for {key!r}; configured: {available}"
        )
    selected = value[key]
    if selected is None:
        raise M42Error(f"tenant profile has no configured {section} value for {key!r}")
    return selected


def _origin(url):
    parsed = urllib.parse.urlparse(url)
    default_port = {"https": 443, "http": 80}.get(parsed.scheme)
    try:
        port = parsed.port or default_port
    except ValueError:  # malformed port in a server-supplied Location header
        port = None
    return parsed.scheme, (parsed.hostname or "").lower(), port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib re-sends request headers on redirects, including Authorization.
    Follow a redirect only when it stays on the same HTTPS origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = _origin(req.full_url)
        target = _origin(newurl)
        if target[0] != "https" or target != source:
            raise M42Error(
                f"refusing HTTP {code} redirect from {source[0]}://{source[1]} to "
                f"{target[0]}://{target[1]}: credentials are only sent to the "
                "configured HTTPS origin; check --base-url"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# One opener for every request, so no call can bypass the redirect policy.
_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler)


def _urlopen(req, timeout):
    """The only network call site; unit tests patch this function."""
    return _OPENER.open(req, timeout=timeout)


class Client:
    def __init__(self, base_url, api_token, tenant_profile=None):
        self.base_url = normalize_base_url(base_url)
        self.api_token = api_token
        self.tenant_profile = validate_tenant_profile(
            {} if tenant_profile is None else tenant_profile)
        self.profile_source = None
        self.tenant_review = None
        self._access_token = None
        self._access_exp = 0
        self._state_rows_cache = {}

    def _exchange(self, method, path, url, data, headers, *, timeout=60, retry=False):
        """Send one request and parse its JSON body. Every transport, status, and
        decoding failure becomes an M42Error."""
        what = f"{'retry of ' if retry else ''}{method} {path}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _urlopen(req, timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise M42HttpError(f"HTTP {e.code} on {what}: {detail}", e.code)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise M42TimeoutError(f"timeout on {what}")
            raise M42Error(f"connection error on {what}: {e.reason}")
        except (TimeoutError, socket.timeout):
            raise M42TimeoutError(f"timeout on {what}")
        except (OSError, http.client.HTTPException) as e:
            raise M42Error(f"connection error on {what}: {e}")
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise M42Error(f"non-JSON response on {what}: {raw[:200]!r}")

    def _access(self):
        if self._access_token and time.time() < self._access_exp - 30:
            return self._access_token
        path = "/api/ApiToken/GenerateAccessTokenFromApiToken/"
        data = None
        errors = []
        content_types = ("application/json;charset=UTF-8", "text/json")
        for index, content_type in enumerate(content_types):
            headers = {
                "Authorization": "Bearer " + self.api_token,
                "Content-Type": content_type,
            }
            try:
                data = self._exchange("POST", path, self.base_url + path, b"{}",
                                      headers, timeout=30)
                break
            except M42HttpError as e:
                errors.append(f"{content_type}: {e}")
                if e.code in (401, 403) or index == len(content_types) - 1:
                    raise M42Error(f"token exchange failed: {'; '.join(errors)}")
            except M42Error as e:
                raise M42Error(f"token exchange failed: {e}")
        if not isinstance(data, dict):
            raise M42Error("token exchange returned no usable JSON object")
        self._access_token = data.get("RawToken")
        if not self._access_token:
            raise M42Error(f"token exchange returned no RawToken: keys={list(data)}")
        # Access tokens are short-lived; assume ~5 min if not told otherwise.
        self._access_exp = time.time() + 280
        return self._access_token

    def request(self, method, path, params=None, body=None, language=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Authorization": "Bearer " + self._access(), "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        if language:
            headers["Explicit-Language"] = language
        return self._do(method, path, url, data, headers)

    def _do(self, method, path, url, data, headers):
        """Run one request. A 401 refreshes the access token once for any method,
        because the server rejected the call before processing it. Only GETs are
        retried after throttling, gateway errors, or timeouts: a POST, PUT, or
        DELETE may already have been applied."""
        delays = GET_RETRY_DELAYS if method == "GET" else ()
        refreshed = False
        attempt = 0
        while True:
            try:
                return self._exchange(method, path, url, data, headers,
                                      retry=refreshed or attempt > 0)
            except M42HttpError as e:
                if e.code == 401 and not refreshed:
                    # Access-token lifetime is a guess (~280 s); a mid-session 401
                    # means it expired earlier.
                    refreshed = True
                    self._access_token = None
                    self._access_exp = 0
                    headers = dict(headers)
                    headers["Authorization"] = "Bearer " + self._access()
                    continue
                if e.code not in RETRYABLE_GET_STATUS or attempt >= len(delays):
                    raise
            except M42TimeoutError:
                if attempt >= len(delays):
                    raise
            time.sleep(delays[attempt])
            attempt += 1

    def fragments(self, dd, where="", columns="ID", page_size=1000, max_records=10000):
        """Page and deduplicate fragments.

        The result is a list whose `truncated` attribute is true when more rows
        may exist: the max_records limit was reached on a full page, or the
        server repeated a full page (some tenants ignore pageNumber), in which
        case the rows collected so far are returned instead of being discarded.
        """
        dd = validate_dd_name(dd)
        if page_size < 1 or max_records < 1:
            raise M42Error("page_size and max_records must be positive")
        page_size = min(page_size, max_records)
        out = FragmentRows()
        seen = set()
        page = 0
        while True:
            params = {"where": where, "columns": columns,
                      "pageSize": page_size, "pageNumber": page}
            batch = self.request("GET", f"/api/data/fragments/{dd}", params=params)
            if not isinstance(batch, list):
                raise M42Error(f"unexpected response for {dd}: {str(batch)[:200]}")
            count_before = len(out)
            for index, row in enumerate(batch):
                rid = row.get("ID") if isinstance(row, dict) else None
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                out.append(row)
                if len(out) >= max_records:
                    out.truncated = (index + 1 < len(batch)
                                     or len(batch) >= page_size)
                    return out
            if len(batch) < page_size:
                break
            if len(out) == count_before:
                out.truncated = True
                break
            page += 1
        return out

    def single(self, dd, where, columns="ID"):
        rows = self.fragments(dd, where=where, columns=columns, page_size=1, max_records=1)
        return rows[0] if rows else None


def _ticket_prefix(ticket_number):
    match = TICKET_NUMBER_RE.match(str(ticket_number).strip().upper())
    if not match:
        raise M42Error(f"invalid ticket number format: {ticket_number!r}")
    return match.group(1)


def _ticket_family(c, ticket_number):
    prefix = _ticket_prefix(ticket_number)
    family = _profile_value(c, "ticket_prefixes").get(prefix)
    if family is None:
        raise M42Error(
            f"tenant profile has no ticket family for prefix {prefix!r}; "
            "run setup discovery and review ticket_prefixes"
        )
    return family


def resolve_config_path():
    """Single source of truth for the config file location, for reads and writes.

    1. M42_CONFIG_PATH, when set.
    2. The legacy file next to this script, when it already exists.
    3. $XDG_CONFIG_HOME/m42sd/m42_config.json (default ~/.config/m42sd/...), so
       a new config never lands inside the skill directory.
    """
    explicit = os.environ.get("M42_CONFIG_PATH")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if os.path.exists(LEGACY_CONFIG_PATH):
        return LEGACY_CONFIG_PATH
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(config_home, "m42sd", CONFIG_FILE_NAME)


def _read_config(path):
    """Parse the stored config once, refusing a file other local users can access."""
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise M42Error(
                f"config file {path} holds the API token but is accessible by "
                f"group or others (mode {mode:04o}); run: chmod 600 {path}"
            )
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise M42Error(f"cannot read config file {path}: {e}")
    if not isinstance(cfg, dict):
        raise M42Error(f"config file {path} must contain a JSON object")
    return cfg


def _write_config(path, cfg):
    """Write atomically: a 0600 temp file in the target directory, then rename.
    A failed write never truncates an existing config or exposes the token."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temp_path = os.path.join(
        directory, f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), 0o600)
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def load_client():
    env_base = os.environ.get("M42_BASE_URL")
    env_token = os.environ.get("M42_API_TOKEN")
    base = env_base
    token = env_token
    cfg = {}
    config_path = resolve_config_path()
    if os.path.exists(config_path):
        cfg = _read_config(config_path)
        if env_base and not env_token and cfg.get("base_url"):
            if normalize_base_url(env_base) != normalize_base_url(cfg["base_url"]):
                raise M42Error(
                    "M42_BASE_URL selects a different tenant; also set "
                    "M42_API_TOKEN and M42_TENANT_PROFILE_FILE"
                )
        base = base or cfg.get("base_url")
        token = token or cfg.get("api_token")
    if not (base and token):
        raise M42Error("no configuration: set M42_BASE_URL and M42_API_TOKEN "
                       "or run `setup` first")
    profile = cfg.get("tenant_profile") or {}
    profile_source = "config" if profile else "empty"
    if env_base and cfg.get("base_url"):
        try:
            same_tenant = normalize_base_url(env_base) == normalize_base_url(
                cfg["base_url"]
            )
        except M42Error:
            same_tenant = False
        if not same_tenant:
            profile = {}
            profile_source = "empty"
    profile_path = os.environ.get("M42_TENANT_PROFILE_FILE")
    if profile_path:
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)
            profile_source = "environment-file"
        except (OSError, json.JSONDecodeError) as e:
            raise M42Error(f"cannot read M42_TENANT_PROFILE_FILE: {e}")
    client = Client(base, token, profile)
    client.profile_source = profile_source
    if profile_source == "config":
        client.tenant_review = cfg.get("tenant_review")
    return client


def out(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def fail(msg, **extra):
    result = {"ok": False, "error": msg}
    result.update(extra)
    out(result)
    sys.exit(1)


def _mark_truncated(result, *row_sets):
    """Tell the caller when a listing may be incomplete instead of failing silently."""
    if any(getattr(rows, "truncated", False) for rows in row_sets):
        result["truncated"] = True
        result["truncation_note"] = (
            "more rows may exist: the row limit was reached or the server "
            "stopped paginating; narrow the filter"
        )
    return result


# ---------------------------------------------------------------- commands

def _discover_fragment_rows(c, dd, *, where="", columns, max_records=1000):
    attempts = [columns] if isinstance(columns, str) else list(columns)
    errors = []
    for selected_columns in attempts:
        try:
            rows = c.fragments(
                dd,
                where=where,
                columns=selected_columns,
                page_size=min(max_records, 1000),
                max_records=max_records,
            )
            return {"available": True, "data_definition": dd, "rows": rows,
                    "truncated": bool(getattr(rows, "truncated", False))}
        except M42Error as e:
            errors.append(str(e))
    return {
        "available": False,
        "data_definition": dd,
        "rows": [],
        "error": errors[-1],
    }


def _discover_tenant(c):
    """Read tenant-owned choices without applying product or reference defaults."""
    discovery = {
        "states": _discover_fragment_rows(
            c,
            DD_STATE,
            columns=("ID,Value,DisplayString,StateGroup", "ID,Value,DisplayString"),
            max_records=2000,
        ),
        "urgency": _discover_fragment_rows(
            c, DD_URGENCY, columns="ID,Value,DisplayString", max_records=500
        ),
        "impact": _discover_fragment_rows(
            c, DD_IMPACT, columns="ID,Value,DisplayString", max_records=500
        ),
        "close_reasons": _discover_fragment_rows(
            c,
            DD_CLOSE_REASON,
            columns="ID,Value,DisplayString",
            max_records=1000,
        ),
        "journal_actions": _discover_fragment_rows(
            c,
            DD_JOURNAL_TYPE,
            columns=("ID,Value,DisplayString,VisibleInPortal", "ID,Value,DisplayString"),
            max_records=1000,
        ),
        "roles": _discover_fragment_rows(
            c,
            DD_SECURITY_ROLE,
            where="ShowInForwardAction=1",
            columns=(
                "ID,Name,ShowInForwardAction,T(SPSScRoleClassBase).ID as RoleId",
                "ID,Name,ShowInForwardAction",
            ),
            max_records=10000,
        ),
    }
    ticket_limit = 10000
    tickets = _discover_fragment_rows(
        c, DD_ACTIVITY, columns="ID,TicketNumber", max_records=ticket_limit
    )
    prefix_counts = {}
    if tickets["available"]:
        for row in tickets["rows"]:
            number = str(row.get("TicketNumber") or "").strip().upper()
            match = TICKET_NUMBER_RE.match(number)
            if match:
                prefix = match.group(1)
                prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
    discovery["ticket_prefixes"] = {
        "available": tickets["available"],
        "data_definition": DD_ACTIVITY,
        "prefix_counts": dict(sorted(prefix_counts.items())),
        "sample_size": len(tickets["rows"]),
        "sample_limit": ticket_limit,
        "possibly_truncated": (len(tickets["rows"]) >= ticket_limit
                               or bool(tickets.get("truncated"))),
        "unrecognized_number_count": len(tickets["rows"]) - sum(prefix_counts.values()),
    }
    if not tickets["available"]:
        discovery["ticket_prefixes"]["error"] = tickets.get("error")
    return discovery


def _profile_template(discovery):
    prefixes = discovery["ticket_prefixes"].get("prefix_counts", {})
    profile = copy.deepcopy(EMPTY_TENANT_PROFILE)
    profile["states"] = {semantic: None for semantic in STATE_SEMANTICS}
    profile["urgency"] = {alias: None for alias in ("low", "medium", "high")}
    profile["journal_actions"] = {
        name: None for name in PORTABLE_JOURNAL_TEXT if name != "state_change"
    }
    profile["ticket_prefixes"] = {prefix: None for prefix in prefixes}
    return profile


def _setup_questions():
    return [
        {
            "field": "state_group and states",
            "question": (
                "Which live state group and values mean new, assigned, in progress, "
                "paused, planned, solved, and closed? Use null only for unused states."
            ),
        },
        {
            "field": "urgency and impact_default",
            "question": (
                "Which live urgency values should low/medium/high use, which urgency "
                "is the creation default, and which live impact is the default?"
            ),
        },
        {
            "field": "close_reasons",
            "question": (
                "Which discovered close reasons may the agent use, and what stable "
                "aliases should identify them?"
            ),
        },
        {
            "field": "journal_actions",
            "question": (
                "Which discovered journal templates match each action? Use null to "
                "write an explicit plain comment instead of a native template."
            ),
        },
        {
            "field": "ticket_prefixes",
            "question": (
                "Map every discovered ticket prefix to incident, service_request, "
                "ticket, task, or problem; use null to explicitly disable unsupported "
                "families such as projects or changes. Review sample limits and add "
                "known prefixes missing from the sample."
            ),
        },
        {
            "field": "roles and role_assignment_attribute",
            "question": (
                "Which discovered forward roles may the agent use? Normally assign "
                "their RoleId through RecipientRole; use Recipient only when this "
                "tenant intentionally represents roles as Person records."
            ),
        },
        {
            "field": "behavior",
            "question": (
                "Choose forwarding and reopen states, states preserved on forward, "
                "default comment visibility, each family close path, acting-user "
                "assignment, allowed state-close fallbacks, comment language mode, "
                "operator language, and close questions."
            ),
        },
        {
            "field": "portal_url_template",
            "question": (
                "Optional: provide tenant portal URL containing {ticket_number}, or null."
            ),
        },
    ]


def _discovered_integer_values(section):
    return {
        int(row["Value"])
        for row in section.get("rows", [])
        if row.get("Value") is not None
        and str(row.get("Value")).lstrip("-").isdigit()
    }


def _validate_profile_against_discovery(profile, discovery):
    warnings = []
    comparisons = {
        "states": "states",
        "urgency": "urgency",
        "close_reasons": "close_reasons",
        "journal_actions": "journal_actions",
    }
    for profile_section, discovery_section in comparisons.items():
        section = discovery[discovery_section]
        selected = {
            value for value in profile[profile_section].values() if value is not None
        }
        if section["available"]:
            available = _discovered_integer_values(section)
            unknown = sorted(selected - available)
            if unknown:
                raise M42Error(
                    f"tenant profile {profile_section} values are not live: {unknown}"
                )
        elif selected:
            warnings.append(
                f"could not live-verify {profile_section}: {section.get('error')}"
            )
    impact = discovery["impact"]
    if profile["impact_default"] is not None:
        if impact["available"]:
            available_impacts = _discovered_integer_values(impact)
            if profile["impact_default"] not in available_impacts:
                raise M42Error(
                    "tenant profile impact_default is not a live impact value: "
                    f"{profile['impact_default']}"
                )
        else:
            warnings.append(
                f"could not live-verify impact_default: {impact.get('error')}"
            )
    states = discovery["states"]
    if profile["state_group"] is not None and states["available"]:
        groups = {
            int(row["StateGroup"])
            for row in states["rows"]
            if row.get("StateGroup") is not None
            and str(row.get("StateGroup")).lstrip("-").isdigit()
        }
        if groups and profile["state_group"] not in groups:
            raise M42Error(
                f"tenant profile state_group {profile['state_group']} is not live: "
                f"{sorted(groups)}"
            )
        if not groups:
            warnings.append("state rows expose no StateGroup; state_group is unverified")
        selected_states = {
            value for value in profile["states"].values() if value is not None
        }
        values_in_group = {
            int(row["Value"])
            for row in states["rows"]
            if row.get("Value") is not None
            and row.get("StateGroup") is not None
            and str(row.get("StateGroup")).lstrip("-").isdigit()
            and int(row["StateGroup"]) == profile["state_group"]
        }
        outside_group = sorted(selected_states - values_in_group)
        if values_in_group and outside_group:
            raise M42Error(
                "tenant profile states are outside selected state_group: "
                f"{outside_group}"
            )
    discovered_prefixes = set(
        discovery["ticket_prefixes"].get("prefix_counts", {})
    )
    missing_prefixes = sorted(discovered_prefixes - set(profile["ticket_prefixes"]))
    if missing_prefixes:
        warnings.append(f"unconfigured discovered ticket prefixes: {missing_prefixes}")
    roles = discovery["roles"]
    if roles["available"] and profile["role_assignment_attribute"] == "RecipientRole":
        available_role_ids = {
            str(row.get("RoleId")) for row in roles["rows"] if row.get("RoleId")
        }
        unknown_roles = sorted(
            role["id"] for role in profile["roles"].values()
            if role["id"] not in available_role_ids
        )
        if unknown_roles and (available_role_ids or not roles["rows"]):
            raise M42Error(
                f"tenant profile role IDs are not live forward roles: {unknown_roles}"
            )
        if profile["roles"] and not available_role_ids:
            warnings.append("discovered role rows expose no RoleId; role IDs are unverified")
    elif profile["roles"] and not roles["available"]:
        warnings.append(f"could not live-verify roles: {roles.get('error')}")
    elif profile["roles"]:
        # role_assignment_attribute="Recipient": targets are Person records, so
        # the forward-role inventory cannot confirm them. This is not an error.
        warnings.append(
            "roles are assigned through Recipient (Person records); their IDs "
            "were not checked against the forward-role inventory"
        )
    return warnings


def _validate_setup_answers(raw_profile, profile, discovery):
    """Require explicit operator decisions; null remains an intentional answer."""
    required_top = set(EMPTY_TENANT_PROFILE)
    missing_top = sorted(required_top - set(raw_profile))
    if missing_top:
        raise M42Error(f"tenant profile is missing setup answers: {missing_top}")
    required_states = {"assigned", "in_progress", "closed"}
    unanswered_states = sorted(
        set(STATE_SEMANTICS) - set(raw_profile.get("states", {}))
    )
    if unanswered_states:
        raise M42Error(
            "tenant profile must answer every state semantic (integer or null): "
            f"{unanswered_states}"
        )
    missing_states = sorted(
        semantic for semantic in required_states
        if profile["states"].get(semantic) is None
    )
    if missing_states:
        raise M42Error(f"tenant profile requires core state mappings: {missing_states}")
    missing_urgency = sorted(
        alias for alias in ("low", "medium", "high")
        if profile["urgency"].get(alias) is None
    )
    if (missing_urgency or profile["urgency_default"] is None
            or profile["impact_default"] is None):
        raise M42Error(
            "tenant profile requires low/medium/high urgency, urgency_default, "
            "and impact_default"
        )
    if not profile["close_reasons"]:
        raise M42Error("tenant profile requires at least one reviewed close reason")
    required_actions = set(_profile_template(discovery)["journal_actions"])
    missing_actions = sorted(
        required_actions - set(raw_profile.get("journal_actions", {}))
    )
    if missing_actions:
        raise M42Error(
            "tenant profile must answer every journal action (integer or null): "
            f"{missing_actions}"
        )
    discovered_prefixes = set(
        discovery["ticket_prefixes"].get("prefix_counts", {})
    )
    missing_prefixes = sorted(
        prefix for prefix in discovered_prefixes
        if prefix not in profile["ticket_prefixes"]
    )
    if missing_prefixes:
        raise M42Error(
            "tenant profile must answer discovered ticket prefixes "
            f"(family or null to disable): {missing_prefixes}"
        )
    behavior_answers = raw_profile.get("behavior", {})
    missing_behavior = sorted(
        set(EMPTY_TENANT_PROFILE["behavior"]) - set(behavior_answers)
    )
    if missing_behavior:
        raise M42Error(f"tenant profile is missing behavior answers: {missing_behavior}")
    if profile["behavior"]["comment_language_mode"] is None:
        raise M42Error("tenant profile requires behavior.comment_language_mode")
    if profile["behavior"]["default_comment_visibility"] is None:
        raise M42Error(
            "tenant profile requires behavior.default_comment_visibility"
        )
    if profile["behavior"]["reopen_state"] is None:
        raise M42Error("tenant profile requires behavior.reopen_state")
    used_families = {
        family for family in profile["ticket_prefixes"].values()
        if family is not None
    }
    missing_close_paths = sorted(
        used_families - set(profile["behavior"]["preclose_state_by_family"])
    )
    if missing_close_paths:
        raise M42Error(
            "tenant profile must choose a preclose state or null for families: "
            f"{missing_close_paths}"
        )


def _setup_token(args):
    """Token sources: deprecated --token, then M42_API_TOKEN, then a TTY prompt."""
    if args.token:
        print("warning: --token is deprecated because it exposes the secret in the "
              "process list and shell history; export M42_API_TOKEN or use the "
              "interactive prompt instead", file=sys.stderr)
        return args.token
    token = os.environ.get("M42_API_TOKEN")
    if token:
        return token
    if sys.stdin is not None and sys.stdin.isatty():
        import getpass
        return getpass.getpass("API token: ")
    fail("no API token: export M42_API_TOKEN or run setup in an interactive "
         "terminal to be prompted; config NOT written")


def cmd_setup(args):
    token = _setup_token(args)
    if not token.strip():
        fail("API token must not be empty")
    token = token.strip()
    profile = None
    if args.profile_file:
        try:
            with open(args.profile_file, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            fail(f"could not read tenant profile, config NOT written: {e}")
    try:
        c = Client(args.base_url, token, profile or {})
    except M42Error as e:
        fail(f"invalid configuration, config NOT written: {e}")
    try:
        c._access()
        discovery = _discover_tenant(c)
    except (urllib.error.URLError, OSError, M42Error) as e:
        fail(f"credential verification failed, config NOT written: {e}")
    if profile is None:
        out({
            "ok": True,
            "configured": False,
            "discovery": discovery,
            "questions": _setup_questions(),
            "profile_template": _profile_template(discovery),
            "next": (
                "Review choices with the operator, write a tenant profile, then rerun "
                "setup with --profile-file. No credentials were stored."
            ),
        })
        return
    try:
        reviewed_profile = validate_tenant_profile(profile)
        _validate_setup_answers(profile, reviewed_profile, discovery)
        warnings = _validate_profile_against_discovery(reviewed_profile, discovery)
    except M42Error as e:
        fail(f"tenant profile review failed, config NOT written: {e}")
    cfg = {
        "base_url": c.base_url,
        "api_token": token,
        "tenant_profile": reviewed_profile,
        "tenant_review": {
            "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "warnings": warnings,
        },
    }
    config_path = resolve_config_path()
    try:
        _write_config(config_path, cfg)
    except OSError as e:
        fail(f"could not write config file {config_path}: {e}")
    result = {"ok": True, "configured": True, "written": config_path,
              "warning": "token stored plaintext; protect this file",
              "tenant_review_warnings": warnings,
              "token_expiry": _token_jwt_expiry(token)}
    out(result)


def cmd_tenant_config(args):
    """Return reviewed non-secret tenant choices used by operational commands."""
    c = load_client()
    out({
        "ok": True,
        "base_url": c.base_url,
        "profile_source": getattr(c, "profile_source", None),
        "tenant_profile": c.tenant_profile,
        "tenant_review": getattr(c, "tenant_review", None),
    })


def _token_jwt_expiry(token):
    try:
        payload = _decode_jwt_payload(token)
        exp = payload.get("exp")
        if not exp:
            return {"exp": None, "note": "no exp claim"}
        exp_dt = datetime.fromtimestamp(int(exp), tz=timezone.utc)
        days = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return {"exp": exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "days_remaining": round(days, 1),
                "warning": ("lifetime <= 14 days remaining — plan rotation")
                           if days < 14 else None}
    except M42Error:
        return {"exp": None, "note": "token not a decodable JWT"}
    except (ValueError, OverflowError, OSError):
        return {"exp": None, "note": "unparseable exp claim"}


def cmd_whoami(args):
    c = load_client()
    # cheap, stable probe: count reachable account fragments; do NOT return other
    # users' account names (would leak directory data into the agent context)
    accounts = c.fragments(DD_ACCOUNT, where="", columns="ID",
                           page_size=5, max_records=5)
    result = {"ok": True, "note": "token valid; API reachable",
              "probe_rows": len(accounts),
              "token_expiry": _token_jwt_expiry(c.api_token)}
    out(result)


def cmd_resolve_user(args):
    c = load_client()
    try:
        result = _resolve_user_or_fail(c, args.user)
    except M42Error as e:
        fail(str(e))
    result["ok"] = True
    out(result)


def cmd_search_tickets(args):
    c = load_client()
    cols = args.columns or ("ID,TicketNumber,Subject,CreatedDate,"
                            "T(SPSCommonClassBase).State.DisplayString as Status,"
                            "Initiator.DisplayName as InitiatorName")
    rows = c.fragments(DD_ACTIVITY, where=args.where, columns=cols,
                       max_records=min(args.max, MAX_RECORDS_CEILING))
    out(_mark_truncated({"ok": True, "count": len(rows), "tickets": rows}, rows))


def _portal_url(c, ticket_number):
    """Configured tenant portal URL; no tenant URL shape is inferred."""
    try:
        template = _profile_value(c, "portal_url_template")
        if template:
            return template.format(
                ticket_number=urllib.parse.quote(str(ticket_number), safe="")
            )
    except Exception:  # noqa: BLE001 - best-effort URL building
        pass
    return None


def _ticket_journal_rows(c, ticket_number, activity_id):
    """Journal rows through documented T() navigation, with the object-expression
    query as a guarded cross-version fallback when the first query is empty or
    rejected."""
    try:
        rows = c.fragments(
            DD_JOURNAL,
            where=f"T(SPSActivityClassBase).TicketNumber={asql_quote(ticket_number)}",
            columns=JOURNAL_COLUMNS, max_records=5000)
        primary_error = None
    except M42Error as e:
        rows = []
        primary_error = e
    if rows:
        return rows
    try:
        # Unverified against a live tenant: this filter uses the activity
        # fragment ID. Some versions may expect the owning object ID instead.
        return c.fragments(
            DD_JOURNAL,
            where=f"[Expression-ObjectID]={asql_quote(activity_id)}",
            columns=JOURNAL_COLUMNS, max_records=5000)
    except M42Error:
        if primary_error is not None:
            raise primary_error
        return rows


def _ticket_attachments(c, ticket_number, max_records):
    return c.fragments(
        DD_ATTACHMENT,
        where=f"T(SPSActivityClassBase).TicketNumber={asql_quote(ticket_number)}",
        columns="ID,Name,CreatedDate,FileSize",
        max_records=max_records)


def cmd_get_ticket(args):
    c = load_client()
    tn = parse_ticket_number(args.ticket_number)
    q = asql_quote(tn)
    cols = ("ID,TicketNumber,Subject,DescriptionHTML as Description,"
            "CreatedDate,ReminderDate,WorkingTimeDisplayString,"
            "Initiator.ID as InitiatorId,Initiator.DisplayName as InitiatorName,"
            "Recipient.ID as RecipientId,Recipient.DisplayName as RecipientName,"
            "Urgency,Urgency.DisplayString as UrgencyName,Priority,"
            "T(SPSCommonClassBase).State.DisplayString as Status")
    if args.columns:
        cols += "," + args.columns
    act = c.single(DD_ACTIVITY, f"TicketNumber={q}", columns=cols)
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    # Same decoding as journal text: entities become literal characters.
    if isinstance(act.get("Description"), str):
        act["Description"] = html.unescape(act["Description"])
    # Concurrency token for --expected-timestamp on the mutating commands.
    common = _ticket_common_fragment(c, tn)
    journal = _ticket_journal_rows(c, tn, act["ID"])
    creator_ids = sorted({str(j.get("CreatorId")) for j in journal
                          if j.get("CreatorId")})
    names = _bulk_user_names(c, creator_ids)
    entries = []
    for j in journal:
        if args.portal_only and not j.get("VisibleInPortal"):
            continue
        text = html.unescape(j.get("OriginalSolutionHtml") or "")
        entries.append({
            "id": j.get("ID"),
            "created": j.get("CreatedDate"),
            "creator_id": j.get("CreatorId"),
            "creator": names.get(str(j.get("CreatorId")), str(j.get("CreatorId"))),
            "activity_action": j.get("ActivityAction"),
            "visible_in_portal": j.get("VisibleInPortal"),
            "text": text,
        })
    entries.sort(key=lambda e: (e.get("created") or "", e.get("id") or ""))
    result = {"ok": True, "ticket": act, "portal_url": _portal_url(c, tn),
              "timestamp": common.get("TimeStamp") if common else None,
              "journal": entries}
    truncated_sets = [journal]
    if args.attachments:
        try:
            atts = _ticket_attachments(c, tn, 200)
            result["attachments"] = atts
            truncated_sets.append(atts)
        except M42Error as e:
            result["attachments"] = []
            result["attachments_note"] = f"not readable on this tenant ({str(e)[:120]})"
    out(_mark_truncated(result, *truncated_sets))


def _bulk_user_names(c, user_ids):
    """Map user GUIDs to display names; one batched lookup, best-effort."""
    names = {}
    ids = sorted({str(u) for u in user_ids if u})
    if not ids:
        return names
    in_clause = ",".join(asql_quote(u) for u in ids)
    try:
        rows = c.fragments(DD_USER, where=f"ID IN ({in_clause})",
                           columns="ID,DisplayName",
                           page_size=max(len(ids), 1), max_records=len(ids))
        for r in rows:
            if r.get("ID") and r.get("DisplayName"):
                names[str(r["ID"])] = r["DisplayName"]
    except M42Error:
        pass
    return names


def _resolve_user_arg(c, user):
    """Accept a GUID (verified to exist) or a name/email/account -> user GUID."""
    if is_guid(user):
        guid = user.strip()
        if not c.single(DD_USER, f"ID={asql_quote(guid)}", columns="ID"):
            raise M42Error(f"user not found: no Person record has ID {guid}")
        return guid
    return _resolve_user_or_fail(c, user)["user_id"]


def _output_created_activity(c, create_result, subject, type_name, *,
                             with_portal_url):
    """Shared create readback for tickets and problems."""
    rows = _created_activity_candidates(c, create_result, subject)
    result = {"ok": True, "object_id": None, "ticket_number": None,
              "type": type_name}
    if not rows:
        result["note"] = "created, but readback failed; check via search-tickets"
    elif len(rows) > 1:
        result["candidates"] = rows
        result["note"] = ("multiple matches for subject; verify via search-tickets "
                          "before acting on the new ticket")
    else:
        result["object_id"] = rows[0]["ID"]
        result["ticket_number"] = rows[0].get("TicketNumber")
        if with_portal_url:
            result["portal_url"] = _portal_url(c, rows[0].get("TicketNumber") or "")
    out(result)


def cmd_create_ticket(args):
    c = load_client()
    user = _resolve_user_arg(c, args.user)
    ci = CI_INCIDENT if args.type == "incident" else CI_TICKET
    urgency = args.urgency or _profile_value(c, "urgency_default")
    frag = {
        "Subject": args.subject,
        "Initiator": user,
        "Urgency": _profile_value(c, "urgency", urgency),
        "Impact": _profile_value(c, "impact_default"),
        "DescriptionHTML": _plain_text_field(args.description),
    }
    if args.category:
        cat = _resolve_category_name(c, args.category)
        # Category is a scalar relation field and therefore takes the GUID string.
        frag["Category"] = cat
    body = {DD_ACTIVITY: frag}
    if args.type != "incident":
        body["InitialData"] = {"Configuration": {"TicketType": "6"}}  # 6 = Service Request
    result = c.request("POST", f"/api/data/objects/{ci}", body=body)
    _output_created_activity(c, result, args.subject, args.type,
                             with_portal_url=True)


def _resolve_category_name(c, category):
    """Category GUID by exact name, or a GUID verified to exist."""
    if is_guid(category):
        guid = category.strip()
        if not c.single(DD_CATEGORY, f"ID={asql_quote(guid)}", columns="ID"):
            raise M42Error(f"category not found: no category has ID {guid} "
                           f"(run list-categories for available categories)")
        return guid
    row = c.single(DD_CATEGORY, f"Name={asql_quote(category)}", columns="ID,Name")
    if not row:
        raise M42Error(f"category not found: {category!r} "
                       f"(run list-categories for available names)")
    return row["ID"]


def cmd_create_problem(args):
    c = load_client()
    user = _resolve_user_arg(c, args.user) if args.user else None
    urgency = args.urgency or _profile_value(c, "urgency_default")
    frag = {
        "Subject": args.subject,
        "Urgency": _profile_value(c, "urgency", urgency),
        "Impact": _profile_value(c, "impact_default"),
        "DescriptionHTML": _plain_text_field(args.description),
    }
    if user:
        frag["Initiator"] = user
    body = {DD_ACTIVITY: frag}
    result = c.request("POST", f"/api/data/objects/{CI_PROBLEM}", body=body)
    _output_created_activity(c, result, args.subject, "problem",
                             with_portal_url=False)


def _created_activity_candidates(c, create_result, subject):
    """Prefer official create response ID; use a guarded readback fallback."""
    candidate_id = None
    if isinstance(create_result, str) and is_guid(create_result):
        candidate_id = create_result
    elif isinstance(create_result, dict):
        for key in ("ID", "Id", "id"):
            if is_guid(create_result.get(key, "")):
                candidate_id = create_result[key]
                break
    columns = (
        "ID,TicketNumber,Subject,CreatedDate,"
        "T(SPSCommonClassBase).State.DisplayString as Status"
    )
    if candidate_id:
        row = c.single(DD_ACTIVITY, f"ID={asql_quote(candidate_id)}", columns=columns)
        if row:
            return [row]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return c.fragments(
        DD_ACTIVITY,
        where=(f"Subject={asql_quote(subject)} AND CreatedDate > #{today}#"),
        columns=columns,
        page_size=10,
        max_records=10,
    )


def _ticket_common_fragment(c, ticket_number):
    """Read the ticket's SPSCommonClassBase fragment through T() navigation.

    State is normalized to int here ("204" -> 204) so every closed-state
    comparison works against the integer profile values.
    """
    row = c.single(
        DD_ACTIVITY, f"TicketNumber={asql_quote(ticket_number)}",
        columns="ID,T(SPSCommonClassBase).ID as CID,"
                "T(SPSCommonClassBase).State as State,"
                "T(SPSCommonClassBase).TimeStamp as TimeStamp")
    if isinstance(row, dict):
        row["State"] = _int_or_raw(row.get("State"))
    return row


def _check_expected_timestamp(args, common, ticket_number):
    """Optimistic concurrency for the agent flow: get-ticket reports `timestamp`;
    a mutation given --expected-timestamp stops when the ticket changed since."""
    expected = getattr(args, "expected_timestamp", None)
    if expected is None:
        return
    current = common.get("TimeStamp") if common else None
    if current is None or str(expected).strip() != str(current):
        fail(
            f"ticket changed since it was read: {ticket_number} no longer has the "
            "expected timestamp; run get-ticket again, re-check the ticket with "
            "the human, then retry with the new timestamp. Nothing was changed.",
            expected_timestamp=expected, current_timestamp=current,
        )


def _put_state_verified(c, ticket_number, common, state, **fields):
    """PUT a state onto the common fragment and read it back.

    Some tenants answer permission failures with HTTP 200 + null, so a PUT that
    returned normally proves nothing. Returns the fresh common fragment.
    """
    _fragment_put(c, DD_COMMON, {"ID": common["CID"], "State": state, **fields,
                                 "TimeStamp": common["TimeStamp"]})
    readback = _ticket_common_fragment(c, ticket_number)
    state_after = readback.get("State") if readback else None
    if state_after != state:
        raise M42Error(
            f"verification failed: state write to {state} was accepted but "
            f"{ticket_number} reads back as state {state_after}; the change was "
            "not applied (check token permissions)"
        )
    return readback


def _auto_assign_recipient(c, activity_id):
    """Make the token identity responsible. Best-effort: returns a warning string
    instead of raising, because the primary transition already succeeded."""
    try:
        _fragment_put(c, DD_ACTIVITY,
                      {"ID": activity_id,
                       "TimeStamp": _activity_time_stamp(c, activity_id),
                       "Recipient": _current_identity(c)})
    except M42Error as e:
        return f"automatic recipient assignment failed: {e}"
    return None


def _fragment_put(c, dd, body):
    """PUT a single fragment via the fragments endpoint (works without objects.Update)."""
    return c.request("PUT", f"/api/data/fragments/{dd}", body=body)


def _activity_time_stamp(c, activity_id):
    """Fresh SPSActivityClassBase TimeStamp (concurrency token) for a ticket."""
    row = c.single(DD_ACTIVITY, f"ID={asql_quote(activity_id)}",
                   columns="ID,TimeStamp")
    if not row or not row.get("TimeStamp"):
        raise M42Error(f"no TimeStamp on activity fragment {activity_id}")
    return row["TimeStamp"]


def _activity_owner(c, activity_id):
    """Return concrete owner CI name and object ID from live fragment metadata."""
    activity = c.request(
        "GET", f"/api/data/fragments/{DD_ACTIVITY}/{activity_id}"
    )
    if not isinstance(activity, dict):
        raise M42Error("cannot read the ticket's activity owner")
    owners = [
        (key.removeprefix("UsedInType"), str(value))
        for key, value in activity.items()
        if key.startswith("UsedInType") and value
    ]
    if len(owners) != 1:
        raise M42Error(
            "cannot identify exactly one ticket owner: "
            f"found {[name for name, _ in owners]}"
        )
    ci_type, owner_id = owners[0]
    if not DD_NAME_RE.fullmatch(ci_type) or not is_guid(owner_id):
        raise M42Error("ticket owner metadata is invalid")
    return ci_type, owner_id


def cmd_forward_ticket(args):
    """Forward to configured role/user field and reviewed state behavior."""
    c = load_client()
    tn = parse_ticket_number(args.ticket_number)
    act = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(tn)}", columns="ID")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    common = _ticket_common_fragment(c, tn)
    if not common:
        fail(f"ticket {args.ticket_number} has no common fragment (unexpected)")
    state_before = common.get("State")
    closed_values = _closed_state_values(c)
    if state_before in closed_values:
        fail(f"ticket {args.ticket_number} is already closed")
    _check_expected_timestamp(args, common, tn)
    forward_semantic = c.tenant_profile["behavior"]["forward_state"]
    preserve_semantics = set(
        c.tenant_profile["behavior"]["forward_preserve_states"]
    )
    state_before_semantic = _semantic_for_state_value(c, state_before)
    if args.to_role:
        target_id, target_name = _resolve_role_arg(c, args.target)
        assignment_attribute = _profile_value(c, "role_assignment_attribute")
        if assignment_attribute not in ("RecipientRole", "Recipient"):
            raise M42Error(
                "tenant profile has no role_assignment_attribute; rerun setup"
            )
    else:
        target_id = _resolve_user_arg(c, args.target)
        target_name = _bulk_user_names(c, [target_id]).get(target_id, args.target)
        assignment_attribute = "Recipient"
    applied = {}
    if forward_semantic is not None and state_before_semantic not in preserve_semantics:
        forward_state = _resolve_semantic_state(c, forward_semantic)
        try:
            _put_state_verified(c, tn, common, forward_state)
            applied["State"] = forward_state
        except M42Error as e:
            fail(f"forward failed at state change: {e}", applied=list(applied))
    try:
        _fragment_put(c, DD_ACTIVITY,
                      {"ID": act["ID"],
                       "TimeStamp": _activity_time_stamp(c, act["ID"]),
                       assignment_attribute: target_id})
        applied[assignment_attribute] = target_name
    except M42Error as e:
        fail(f"forward failed at recipient change: {e}", applied=list(applied))
    action = "forward_role" if args.to_role else "forward_user"
    en_word = "role" if args.to_role else "user"
    hint = f"Forwarded to {en_word}: {target_name}"
    if args.comment:
        hint += f"\n\n{args.comment}"
    entry = _gui_journal_entry(c, tn, action, hint, portal=0,
                               activity_id=act["ID"])
    out({"ok": True, "forwarded": args.ticket_number, "to": target_name,
         "role": bool(args.to_role), "state": applied.get("State"),
         "journal_entry": _journal_entry_id(entry),
         "journal_warning": _journal_warning(entry)})


def cmd_list_roles(args):
    """List operator-approved role targets from reviewed tenant configuration."""
    c = load_client()
    roles = [
        {"alias": alias, **role}
        for alias, role in _profile_value(c, "roles").items()
    ]
    roles.sort(key=lambda role: role["alias"].casefold())
    out({
        "ok": True,
        "count": len(roles),
        "assignment_attribute": c.tenant_profile["role_assignment_attribute"],
        "roles": roles,
    })


def _resolve_role_arg(c, value):
    wanted = _normalize_label(value)
    matches = []
    for alias, role in _profile_value(c, "roles").items():
        if wanted in {
            _normalize_label(alias),
            _normalize_label(role["name"]),
            _normalize_label(role["id"]),
        }:
            matches.append(role)
    if len(matches) == 1:
        return matches[0]["id"], matches[0]["name"]
    if not matches:
        raise M42Error(
            f"role is not in operator-approved tenant profile: {value!r}; "
            f"configured aliases: {sorted(_profile_value(c, 'roles'))}"
        )
    raise M42Error(f"ambiguous configured role: {value!r}")


def cmd_update_ticket(args):
    """Partial update of ticket attributes (state, urgency, priority, subject,
    category, recipient, resume date). May perform independent fragment PUTs
    (state vs. attributes); the output reports exactly which parts were applied
    so a partial failure is visible. Closed tickets are rejected — reopening is
    a separate command (reopen-ticket)."""
    if args.recipient is not None and not args.recipient.strip():
        fail("--recipient must not be empty; omit it, or use --auto-recipient "
             "for the token identity")
    if args.recipient and args.auto_recipient:
        fail("--recipient and --auto-recipient are mutually exclusive")
    if args.auto_recipient and args.no_auto_recipient:
        fail("--auto-recipient and --no-auto-recipient are mutually exclusive")
    requested = (args.state, args.subject, args.urgency, args.priority,
                 args.category, args.recipient, args.resume_at)
    if all(value is None for value in requested) and not args.auto_recipient:
        fail("nothing to update: pass at least one of --state, --subject, "
             "--urgency, --priority, --category, --recipient, --auto-recipient, "
             "or --resume-at")
    if args.state is not None and not args.state.strip():
        fail("--state must not be empty")
    c = load_client()
    tn = parse_ticket_number(args.ticket_number)
    act = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(tn)}",
                   columns="ID,TicketNumber,Subject,TimeStamp,"
                           "Urgency,Urgency.DisplayString as UrgencyName,Priority")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    closed_values = _closed_state_values(c)
    common = _ticket_common_fragment(c, tn)
    state_before = common.get("State") if common else None
    if state_before in closed_values:
        fail(f"ticket {args.ticket_number} is already closed — use reopen-ticket "
             f"(or the GUI) instead of update-ticket")
    _check_expected_timestamp(args, common, tn)
    state_value = None
    state_semantic = None
    state_before_semantic = None
    if args.state:
        # Closing via --state is blocked: it would write a closed state without Reason,
        # without the mandatory solution comment and without the GUI-parity
        # close journal entry. Closing = close-ticket.
        state_value = _resolve_state_value(
            c, args.state,
            allow_unreviewed=getattr(args, "allow_unreviewed_state", False))
        state_semantic = _semantic_for_state_value(c, state_value)
        if state_value in closed_values or state_semantic == "closed":
            fail("--state closed is not supported: closing requires reason + "
                 "solution comment + close journal entry — use close-ticket "
                 "instead")
        state_before_semantic = _semantic_for_state_value(c, state_before)
    # Resolve every requested value before the first write. All activity fields
    # share one fragment update, avoiding transient recipients and stale tokens.
    activity_values = {}
    activity_labels = {}
    if args.subject is not None:
        activity_values["Subject"] = args.subject
        activity_labels["Subject"] = args.subject
    if args.urgency is not None:
        activity_values["Urgency"] = _profile_value(c, "urgency", args.urgency)
        activity_labels["Urgency"] = args.urgency
    if args.priority is not None:
        activity_values["Priority"] = args.priority
        activity_labels["Priority"] = args.priority
    if args.category is not None:
        activity_values["Category"] = _resolve_category_name(c, args.category)
        activity_labels["Category"] = args.category
    if args.recipient:
        activity_values["Recipient"] = _resolve_user_arg(c, args.recipient)
        activity_labels["Recipient"] = args.recipient
    if args.resume_at is not None:
        clear = args.resume_at.strip().lower() in ("never", "clear", "none")
        activity_values["ReminderDate"] = None if clear else _iso_utc(args.resume_at)
        activity_labels["ReminderDate"] = (
            "cleared" if clear else activity_values["ReminderDate"]
        )
    implicit_auto = (
        bool(args.state) and not args.recipient and not args.no_auto_recipient
        and state_semantic in c.tenant_profile["behavior"]["auto_recipient_states"]
    )
    acting_user = (_current_identity(c)
                   if args.auto_recipient or implicit_auto else None)
    applied = {}
    errors = []
    # state lives in SPSCommonClassBase -> update that fragment directly
    if args.state:
        if not common:
            fail("ticket has no common fragment (unexpected)")
        try:
            _put_state_verified(c, tn, common, state_value)
            applied["State"] = state_value
        except M42Error as e:
            errors.append(f"state: {e}")
    # State-change audit entry. Known states use recognizable Matrix42 actions;
    # every other state gets an explicit internal transition comment.
    # Only when the state PUT succeeded and was read back (a failed or silently
    # ignored PUT must not produce a journal entry claiming the transition).
    state_entry_actions = {
        "in_progress": "takeover",
        "paused": "pause",
        "solved": "solved",
    }
    state_changed = "State" in applied and state_value != state_before
    journal_entry = None
    if state_changed:
        if state_semantic == "in_progress" and state_before_semantic == "paused":
            action_name = "resume"
        elif state_semantic in state_entry_actions:
            action_name = state_entry_actions[state_semantic]
        else:
            action_name = "state_change"
        body_text = (f"State changed to {args.state}."
                     if action_name == "state_change" else None)
        journal_entry = _gui_journal_entry(
            c, tn, action_name, body_text, portal=0, activity_id=act["ID"])
    if args.auto_recipient or (implicit_auto and "State" in applied):
        activity_values["Recipient"] = acting_user
        activity_labels["Recipient"] = "token identity"
    if activity_values:
        try:
            body = {"ID": act["ID"], "TimeStamp": _activity_time_stamp(c, act["ID"]),
                    **activity_values}
            _fragment_put(c, DD_ACTIVITY, body)
            applied.update(activity_labels)
        except M42Error as e:
            errors.append(f"attributes: {e}")
    if errors:
        fail(f"partial update of {args.ticket_number}: applied={list(applied)}; "
             f"errors: {'; '.join(errors)}", applied=list(applied),
             journal_entry=_journal_entry_id(journal_entry),
             journal_warning=_journal_warning(journal_entry)
             if state_changed else None)
    out({"ok": True, "updated": args.ticket_number, "applied": applied,
         "journal_entry": _journal_entry_id(journal_entry),
         "journal_warning": _journal_warning(journal_entry)
         if state_changed else None})


def _decode_jwt_payload(token):
    import base64
    parts = str(token).split(".")
    if len(parts) < 2:
        raise M42Error("API token is not a JWT; cannot determine identity")
    try:
        payload = json.loads(base64.urlsafe_b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise M42Error(f"API token has an invalid JWT payload: {e}")
    if not isinstance(payload, dict):
        raise M42Error("API token JWT payload is not an object")
    return payload


def _current_identity(c):
    """Person GUID of the API token's identity. The API token is a JWT whose
    payload carries UserFragmentId for the token owner's SPSUserClassBase row."""
    payload = _decode_jwt_payload(c.api_token)
    uid = payload.get("UserFragmentId")
    if not is_guid(uid or ""):
        raise M42Error("API token carries no usable UserFragmentId")
    return uid


def _iso_utc(value):
    """Accept ISO 8601 (date or datetime, Z or offset) and normalize to the
    Z-suffixed UTC form used for fragment writes."""
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise M42Error(f"invalid date/time: {value!r} (use ISO 8601, "
                       f"e.g. 2026-09-10T08:00:00Z or 2026-09-10)")
    # Naive input, including a date-only value (midnight), is taken as UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_api_datetime(value, *, date_end=False):
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if date_end:
            dt += timedelta(days=1)
        return dt
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_label(value):
    return unicodedata.normalize("NFC", str(value or "").strip()).casefold()


def _activity_state_rows(c):
    """Return live activity-state rows from operator-selected group or all groups."""
    state_group = getattr(c, "tenant_profile", EMPTY_TENANT_PROFILE).get(
        "state_group"
    )
    cache = getattr(c, "_state_rows_cache", None)
    if cache is not None and state_group in cache:
        return cache[state_group]
    where = f"StateGroup={state_group}" if state_group is not None else ""
    rows = c.fragments(DD_STATE, where=where,
                       columns="ID,Value,DisplayString", page_size=100,
                       max_records=2000)
    usable = [r for r in rows if r.get("Value") is not None and r.get("DisplayString")]
    if not usable:
        raise M42Error("activity state lookup returned no usable values; "
                       "refusing to guess a mutation value")
    if cache is not None:
        cache[state_group] = usable
    return usable


def _resolve_semantic_state(c, semantic):
    rows = _activity_state_rows(c)
    configured = _profile_value(c, "states").get(semantic)
    if configured is None:
        raise M42Error(
            f"tenant profile has no state mapping for {semantic!r}; rerun setup"
        )
    available = {int(r["Value"]) for r in rows}
    if configured not in available:
        raise M42Error(f"configured state {semantic!r}={configured} is not in "
                       f"live values: {sorted(available)}")
    return configured


def _semantic_for_state_value(c, value):
    if value is None:
        return None
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    configured = _profile_value(c, "states")
    matches = {semantic for semantic, configured_value in configured.items()
               if numeric == configured_value}
    if len(matches) > 1:
        raise M42Error(f"state value {value} has ambiguous semantics: {sorted(matches)}")
    return next(iter(matches)) if matches else None


def _resolve_state_value(c, state_name, *, allow_unreviewed=False):
    """Resolve a profile semantic, a numeric value, or a live display name.

    Numeric values and display names must also be one of the human-reviewed
    profile states unless allow_unreviewed is set: the live inventory contains
    states nobody reviewed, including closed-type ones that would slip past the
    close guard.
    """
    name = _normalize_label(state_name)
    rows = _activity_state_rows(c)
    available_values = {int(r["Value"]) for r in rows}
    reviewed_values = {
        value for value in _profile_value(c, "states").values() if value is not None
    }

    def reviewed(value):
        if allow_unreviewed or value in reviewed_values:
            return value
        raise M42Error(
            f"state value {value} is live but not part of the reviewed tenant "
            f"profile states {sorted(reviewed_values)}; use a configured state "
            "semantic, or pass --allow-unreviewed-state after explicit human approval"
        )

    if re.fullmatch(r"[0-9]+", name):
        value = int(name)
        if value in available_values:
            return reviewed(value)
        raise M42Error(f"state value {value} is not in live state values: "
                       f"{sorted(available_values)}")
    semantic = STATE_INPUT_ALIASES.get(name)
    if semantic and semantic in _profile_value(c, "states"):
        configured = _profile_value(c, "states", semantic)
        if configured in available_values:
            return configured
        raise M42Error(f"configured state {semantic!r}={configured} is not live")
    exact = {int(r["Value"]) for r in rows
             if _normalize_label(r.get("DisplayString")) == name}
    if len(exact) == 1:
        return reviewed(next(iter(exact)))
    if len(exact) > 1:
        raise M42Error(f"ambiguous state name {state_name!r}: values {sorted(exact)}")
    available = [(r.get("Value"), r.get("DisplayString")) for r in rows]
    raise M42Error(f"unknown state {state_name!r}. Live state values: "
                   f"{available}; configured semantics: "
                   f"{sorted(_profile_value(c, 'states'))}")


def _journal_type_pair(c, ticket_number):
    """Find (TypeId, ObjectId) for /api/journal/add: TypeId = ticket family TypeCase
    GUID, ObjectId = a journal entry type row id (UsedInType) valid for that family.
    Source of truth: an existing journal entry of the same ticket (copied via
    T()-navigation; needs no extra permissions). UsedInType is ticket-instance
    specific, so no cross-ticket or family-level fallback is safe."""
    q = asql_quote(ticket_number)
    # Some versions reject Expression-TypeID as an explicit column while still
    # returning it as fragment metadata. Request portable columns, then inspect
    # that metadata.
    rows = c.fragments(DD_JOURNAL,
                       where=f"T(SPSActivityClassBase).TicketNumber={q}",
                       columns="ID,UsedInType", max_records=1)
    if rows:
        entry = rows[0]
        type_id = entry.get("Expression-TypeID")
        uit = entry.get("UsedInType")
        if type_id and uit:
            return str(type_id), str(uit)
    raise M42Error("cannot determine a target-owned journal type pair: ticket has "
                   "no usable journal entries; refusing unsafe cross-ticket defaults")


def _journal_entry_belongs_to_ticket(c, journal_id, ticket_number):
    row = c.single(
        DD_JOURNAL,
        f"ID={asql_quote(journal_id)} AND "
        f"T(SPSActivityClassBase).TicketNumber={asql_quote(ticket_number)}",
        columns="ID")
    return bool(row and str(row.get("ID")) == str(journal_id))


class JournalPartial(Exception):
    """A journal shell exists but could not be verifiably filled."""

    def __init__(self, journal_id, error):
        super().__init__(error)
        self.journal_id = journal_id
        self.error = error


def _verify_journal_fill(c, journal_id, written):
    """Read a filled entry back. Some tenants answer a rejected PUT with
    HTTP 200 + null, which would otherwise leave an empty entry reported as written."""
    row = c.request("GET", f"/api/data/fragments/{DD_JOURNAL}/{journal_id}")
    if not isinstance(row, dict):
        raise M42Error("verification failed: the journal entry could not be read back")
    if written.get("OriginalSolutionHtml"):
        if not _plain_text_value(row.get("OriginalSolutionHtml")).strip():
            raise M42Error("verification failed: the fill was accepted but the "
                           "entry text is still empty")
    elif _int_or_raw(row.get("ActivityAction")) != written["ActivityAction"]:
        raise M42Error("verification failed: the fill was accepted but "
                       "ActivityAction reads back as "
                       f"{row.get('ActivityAction')!r}")
    visible = row.get("VisibleInPortal")
    if visible is not None and _flag(visible) != _flag(written["VisibleInPortal"]):
        raise M42Error("verification failed: VisibleInPortal reads back as "
                       f"{visible!r}, not the requested visibility")


def _create_journal_entry(c, ticket_number, activity_id, fields):
    """Create a journal shell linked to the ticket, fill it, and read it back.

    The steps are not atomic. M42Error means nothing was created; JournalPartial
    means a shell exists and carries its ID for cleanup.
    """
    type_id, used_in_type = _journal_type_pair(c, ticket_number)
    result = c.request("POST", "/api/journal/add",
                       body={"TypeId": type_id, "ObjectId": used_in_type,
                             "TargetObjectId": activity_id})
    if not isinstance(result, dict) or not result.get("JournalId"):
        raise M42Error(f"unexpected /api/journal/add response: {str(result)[:200]}")
    journal_id = result["JournalId"]
    try:
        if not _journal_entry_belongs_to_ticket(c, journal_id, ticket_number):
            raise M42Error("created journal entry is not linked to requested "
                           "ticket; refusing to fill it")
        body = {"ID": journal_id, **fields}
        c.request("PUT", f"/api/data/fragments/{DD_JOURNAL}", body=body)
        _verify_journal_fill(c, journal_id, body)
    except M42Error as e:
        raise JournalPartial(journal_id, str(e))
    return journal_id


def cmd_add_comment(args):
    c = load_client()
    tn = parse_ticket_number(args.ticket_number)
    if not args.text or not args.text.strip() or args.text.strip() == "---":
        fail("--text is required and must not be empty/whitespace/only a separator")
    act = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(tn)}", columns="ID")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    body_text = _plain_text_field(args.text)
    if getattr(args, "internal", False):
        portal = False
    elif getattr(args, "portal", False):
        portal = True
    else:
        visibility = c.tenant_profile["behavior"]["default_comment_visibility"]
        if visibility is None:
            raise M42Error(
                "tenant profile has no default comment visibility; rerun setup"
            )
        portal = visibility == "portal"
    journal_fragment = {
        "OriginalSolutionHtml": body_text,
        "ActivityAction": JOURNAL_COMMENT_ACTION,
        "VisibleInPortal": int(portal),
    }
    # Path B (no objects.Get/objects.Update needed): /api/journal/add creates the
    # linked entry, then a fragment PUT fills text and portal flag.
    # The two steps are NOT atomic: if the fill fails after the POST succeeded,
    # an empty entry exists — report its JournalId for cleanup instead of
    # silently falling back to Path A (which would duplicate the entry).
    try:
        journal_id = _create_journal_entry(c, tn, act["ID"], journal_fragment)
        out({"ok": True, "added": True, "ticket": args.ticket_number,
             "journal_id": journal_id,
             "path": "journal/add+fragment", "visible_in_portal": portal})
        return
    except JournalPartial as e:
        fail(f"journal entry created but not filled — entry {e.journal_id} on "
             f"{args.ticket_number} may be empty; inspect it with get-ticket, "
             f"then delete it with `delete-journal --journal-id {e.journal_id} "
             f"--ticket-number {args.ticket_number} --confirm` or fill it "
             f"manually. Do not re-run add-comment before that. error: {e.error}",
             journal_id=e.journal_id)
    except M42Error as e:
        first_error = str(e)
    # Path A fallback: documented object update with journal append
    # (needs objects.Get + objects.Update audiences + CI read/write).
    try:
        ci, object_id = _activity_owner(c, act["ID"])
    except M42Error as e:
        fail("could not add journal comment safely: target-owned journal pair is "
             f"unavailable and live owner lookup failed ({e})")
    obj = c.request("GET", f"/api/data/objects/{ci}/{object_id}?full=true")
    if not obj or not isinstance(obj.get(DD_JOURNAL), list):
        fail("could not add journal comment: /api/journal/add failed "
             f"({first_error}) and objects.Get unavailable (or journal not a "
             "list on this tenant) — the entry was NOT created")
    obj[DD_JOURNAL].append(journal_fragment)
    c.request("PUT", f"/api/data/objects/{ci}?full=true", body=obj)
    out({"ok": True, "added": True, "ticket": args.ticket_number,
         "path": "objects.update", "visible_in_portal": portal})


def _closed_state_values(c):
    """Live-validate operator-selected closed state."""
    rows = _activity_state_rows(c)
    configured = _profile_value(c, "states").get("closed")
    if configured is None:
        raise M42Error("tenant profile has no closed state; rerun setup")
    available = {int(r["Value"]) for r in rows}
    if configured not in available:
        raise M42Error(f"configured closed state {configured} is not in live "
                       f"values: {sorted(available)}")
    return {configured}


def _journal_action_value(c, action_name):
    configured = _profile_value(c, "journal_actions").get(action_name)
    return configured if configured is not None else JOURNAL_COMMENT_ACTION


def _journal_entry_id(entry):
    return entry.get("id") if entry else None


def _journal_warning(entry):
    """Warning text for an audit entry result from _gui_journal_entry, or None."""
    if not entry or not entry.get("id"):
        reason = entry.get("error") if entry else None
        return "journal entry was not created" + (f": {reason}" if reason else "")
    if not entry.get("filled"):
        return (f"journal entry {entry['id']} was created but not filled "
                f"({entry.get('error')}); clean it up before retrying")
    return None


def _task_close_solution_params(close_reason):
    """Matrix42 metadata used by native task-close journal rendering."""
    if (not isinstance(close_reason, int) or isinstance(close_reason, bool)
            or close_reason < 0):
        raise M42Error("task close reason must be a non-negative integer")
    return (
        '<?xml version="1.0" encoding="utf-16"?>\r\n'
        '<parameters xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\r\n'
        '  <JournalEntryParameterBase '
        'xsi:type="JournalEntryPickupValueParameter" name="closeReason" '
        'pickupClassName="SPSCommonPickupObjectStateReason" '
        f'value="{close_reason}">\r\n'
        '    <IsPortalMode>false</IsPortalMode>\r\n'
        '    <IsExportMode>false</IsExportMode>\r\n'
        '  </JournalEntryParameterBase>\r\n'
        '</parameters>'
    )


def _gui_journal_entry(c, ticket_number, action_name, body_text=None, portal=0,
                       *, close_reason=None, activity_id=None):
    """Append an internal audit entry using only a target-owned journal pair.

    The reviewed tenant profile can map action_name to a recognizable GUI
    ActivityAction. Without one, a plain comment carries explicit event text.
    Never raises M42Error: the audited transition already happened. Returns
    {"id", "filled", "error"}; id is None when no entry could be created.
    """
    action = _journal_action_value(c, action_name)
    fields = {"ActivityAction": action, "VisibleInPortal": int(portal)}
    if action == JOURNAL_COMMENT_ACTION and not body_text:
        body_text = PORTABLE_JOURNAL_TEXT[action_name]
    if body_text:
        fields["OriginalSolutionHtml"] = _plain_text_field(body_text)
    try:
        if (action_name == "close_task" and action != JOURNAL_COMMENT_ACTION
                and close_reason is not None):
            fields["OriginalSolution"] = _plain_text_value(body_text)
            fields["SolutionParams"] = _task_close_solution_params(close_reason)
        if activity_id is None:
            activity_id = _activity_id(c, ticket_number)
        journal_id = _create_journal_entry(c, ticket_number, activity_id, fields)
    except JournalPartial as e:
        return {"id": e.journal_id, "filled": False, "error": e.error}
    except M42Error as e:
        return {"id": None, "filled": False, "error": str(e)}
    return {"id": journal_id, "filled": True, "error": None}


def _activity_id(c, ticket_number):
    row = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(ticket_number)}",
                   columns="ID")
    if not row:
        raise M42Error(f"ticket not found: {ticket_number}")
    return row["ID"]


def _close_journal_entry(c, ticket_number, body_text, portal=0, *,
                         close_reason=None, family=None, activity_id=None):
    """Internal close audit entry using configured ticket family semantics."""
    ticket_family = family or _ticket_family(c, ticket_number)
    action = "close_task" if ticket_family == "task" else "close"
    return _gui_journal_entry(
        c, ticket_number, action, body_text, portal,
        close_reason=close_reason if action == "close_task" else None,
        activity_id=activity_id,
    )


def _record_close_work_time(c, activity_id, minutes, *, end=None):
    """Add one Matrix42 time-tracking fragment before ticket closure.

    The required CLI answer is additional time, not the ticket's aggregate.
    A zero answer explicitly means all work is already tracked and adds no row.
    Parent CI and closure activity type are resolved from live tenant data.
    """
    minutes = float(minutes)
    problem = _work_minutes_error(minutes)
    if problem:
        raise M42Error(f"work minutes {problem}")
    if minutes == 0:
        return None

    ci_type, owner_id = _activity_owner(c, activity_id)

    config = c.single(
        DD_TIME_TRACKING_CONFIG,
        "",
        columns="ID,Mode,TicketsClosureActivityType,SupportedActivityCiTypes",
    )
    if not config or config.get("TicketsClosureActivityType") is None:
        raise M42Error("tenant has no configured closure activity type for time tracking")
    supported = {
        value.strip()
        for value in str(config.get("SupportedActivityCiTypes") or "").split(",")
        if value.strip()
    }
    if supported and ci_type not in supported:
        raise M42Error(f"tenant time tracking does not support {ci_type}")
    try:
        activity_type = int(config["TicketsClosureActivityType"])
    except (TypeError, ValueError):
        raise M42Error("tenant closure activity type is not an integer")
    activity_types = c.fragments(
        DD_TIME_ACTIVITY_TYPE,
        where=f"Value={activity_type}",
        columns="ID,Value,DisplayString",
        page_size=100,
        max_records=100,
    )
    if not any(row.get("Value") is not None
               and int(row["Value"]) == activity_type for row in activity_types):
        raise M42Error(f"configured closure activity type {activity_type} is not live")

    end_dt = end or datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    end_dt = end_dt.astimezone(timezone.utc).replace(microsecond=0)
    begin_dt = end_dt - timedelta(minutes=minutes)
    end_text = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "CreatedDate": end_text,
        "Begin": begin_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "End": end_text,
        "Minutes": minutes,
        "ActivityType": activity_type,
        "User": _current_identity(c),
    }
    created = c.request(
        "POST", f"/api/data/fragments/{DD_TIME_TRACKING}", body=body)
    if isinstance(created, dict):
        created = created.get("ID")
    entry_id = str(created or "").strip()
    if not is_guid(entry_id):
        raise M42Error("time-tracking fragment returned no usable ID; refusing "
                       "to close because work-time recording is unverified")

    def read_link():
        row = c.request("GET", f"/api/data/fragments/{DD_TIME_TRACKING}/{entry_id}")
        if not isinstance(row, dict):
            raise M42Error("no usable ownership readback")
        owners = {
            str(value) for key, value in row.items()
            if key.startswith("UsedInType") and value
        }
        if owners and owners != {owner_id}:
            raise M42Error("entry is linked to a different owner; refusing to relink")
        return row, owners == {owner_id}

    # Concrete CI object IDs need their matching relation. Retain the base
    # relation only as a compatibility fallback on this same created row.
    relations = dict.fromkeys((f"UsedInType{ci_type}", "UsedInTypeSPSActivityTypeBase"))
    try:
        readback, linked = read_link()
        if linked:
            return entry_id
        errors = []
        for relation in relations:
            if not readback.get("TimeStamp"):
                raise M42Error("no concurrency timestamp")
            try:
                _fragment_put(c, DD_TIME_TRACKING, {
                    "ID": entry_id,
                    "TimeStamp": readback["TimeStamp"],
                    relation: owner_id,
                })
            except M42Error as e:
                errors.append(f"{relation}: {e}")
            # A failed/ignored PUT may still have applied. Verify ownership and
            # refresh the concurrency token before deciding whether to fall back.
            readback, linked = read_link()
            if linked:
                return entry_id
        detail = "; ".join(errors) or "relation updates did not attach the owner"
        raise M42Error(detail)
    except M42Error as e:
        raise M42Error(
            f"time-tracking entry {entry_id} could not be verified on the requested "
            f"ticket: {e}; refusing to close. Inspect this existing entry before "
            "retrying to avoid duplicate work time"
        ) from e


def _close_result(args, path, entry, processed, add_processed, work_time_entry,
                  auto_warning, **extra):
    result = {"ok": True, "closed": args.ticket_number, "reason": args.reason,
              "path": path,
              "journal_entry": _journal_entry_id(entry),
              "journal_warning": _journal_warning(entry),
              "processed_journal_entry": _journal_entry_id(processed),
              "processed_journal_warning": _journal_warning(processed)
              if add_processed else None,
              "auto_recipient_warning": auto_warning,
              "work_minutes": args.work_minutes,
              "work_time_entry": work_time_entry}
    result.update(extra)
    return result


def cmd_close_ticket(args):
    c = load_client()
    if not args.confirm:
        fail("--confirm is required: closing mutates ticket state (reopen "
             "afterwards only via reopen-ticket)")
    if getattr(args, "work_minutes", None) is None:
        fail("--work-minutes is required: ask how many additional working-time "
             "minutes must be recorded before closing (0 = already fully tracked)")
    tn = parse_ticket_number(args.ticket_number)
    q = asql_quote(tn)
    if not args.comment or not args.comment.strip():
        fail("--comment is required (non-empty): provide the internal plain-text "
             "solution summary described by the SKILL.md closing rules")
    if args.kb is not None and not is_guid(args.kb):
        fail("--kb must be the KB article GUID (KBArticle relation); use search-kb "
             "to find the article ID")
    act = c.single(DD_ACTIVITY, f"TicketNumber={q}", columns="ID,TicketNumber")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    common = _ticket_common_fragment(c, tn)
    state_before = common.get("State") if common else None
    closed_values = _closed_state_values(c)
    if state_before in closed_values:
        fail(f"ticket {args.ticket_number} is already closed")
    _check_expected_timestamp(args, common, tn)
    family = _ticket_family(c, tn)
    behavior = c.tenant_profile["behavior"]
    preclose_semantic = behavior["preclose_state_by_family"].get(family)
    preclose_state = (
        _resolve_semantic_state(c, preclose_semantic)
        if preclose_semantic is not None else None
    )
    add_processed_entry = (
        family in behavior["processed_journal_families"]
        and preclose_state is not None
        and state_before != preclose_state
    )
    auto_recipient = (family in behavior["auto_recipient_on_close"]
                      and not args.no_auto_recipient)
    close_reason = _profile_value(c, "close_reasons", args.reason)
    # Work time is booked first: a close that then fails must not lose it, and
    # a retry must not book it again. Everything after this line runs inside
    # _close_after_work_time so every failure reports the booked entry.
    work_time_entry = _record_close_work_time(
        c, act["ID"], args.work_minutes)
    booked = {"work_minutes": args.work_minutes,
              "work_time_entry": work_time_entry,
              "work_time_recorded": work_time_entry is not None,
              "retry_hint": WORK_TIME_RETRY_HINT if work_time_entry else None}
    context = {
        "act": act, "tn": tn, "common": common,
        "closed_values": closed_values, "family": family, "behavior": behavior,
        "preclose_state": preclose_state,
        "add_processed_entry": add_processed_entry,
        "auto_recipient": auto_recipient, "close_reason": close_reason,
        "work_time_entry": work_time_entry, "booked": booked,
    }
    try:
        result = _close_after_work_time(c, args, context)
    except SystemExit:
        raise
    except M42Error as e:
        fail(str(e), **{**booked, **e.extra})
    except Exception as e:  # noqa: BLE001 - keep the booked entry visible
        fail(f"unexpected error: {e}", **booked)
    out(result)


def _close_after_work_time(c, args, ctx):
    """Close the ticket after work time was booked. Raises M42Error carrying the
    booking fields (fail() attaches them) instead of calling fail() directly."""
    act, tn, common = ctx["act"], ctx["tn"], ctx["common"]
    closed_values, family, behavior = ctx["closed_values"], ctx["family"], ctx["behavior"]
    preclose_state, close_reason = ctx["preclose_state"], ctx["close_reason"]
    add_processed_entry, auto_recipient = ctx["add_processed_entry"], ctx["auto_recipient"]
    work_time_entry, booked = ctx["work_time_entry"], ctx["booked"]

    def stop(message, **extra):
        raise M42Error(message, **booked, **extra)

    processed = None
    path = "/api/problem/close" if family == "problem" else "/api/ticket/close"
    body = {
        "ObjectIds": [act["ID"]],
        "Comments": _plain_text_field(args.comment),
        "Reason": close_reason,
    }
    if args.kb:
        body["KBArticle"] = args.kb
    if args.notify_initiator:
        body["SendMailToInitiator"] = True
    try:
        c.request("POST", path, body=body)
        # Silent-200 guard: on tenants that answer permission failures with
        # HTTP 200 + null, a REJECTED close would look successful. Verify the
        # state actually moved to a closed value; otherwise fall through.
        state_after = _ticket_common_fragment(c, tn)
        state_after_val = state_after.get("State") if state_after else None
        if state_after_val not in closed_values:
            raise M42Error("close endpoint returned success but ticket state "
                           f"is still {state_after_val} (not closed) — treating "
                           "as rejected")
        # Add the optional processed entry only when reviewed behavior enables it.
        if add_processed_entry:
            processed = _gui_journal_entry(
                c, tn, "processed", portal=0, activity_id=act["ID"])
        entry = _close_journal_entry(
            c, tn, args.comment, portal=0, close_reason=close_reason,
            family=family, activity_id=act["ID"],
        )
        auto_warning = (_auto_assign_recipient(c, act["ID"])
                        if auto_recipient else None)
        return _close_result(args, "close-endpoint", entry, processed,
                             add_processed_entry, work_time_entry, auto_warning)
    except M42Error as e:
        endpoint_error = str(e)
    # The endpoint call failed or was rejected. A timeout may still have closed
    # the ticket, so re-read before deciding anything.
    common = _ticket_common_fragment(c, tn)
    if not common:
        stop(f"close endpoint failed ({endpoint_error}) and the ticket could not "
             "be re-read; verify its state manually")
    if common.get("State") in closed_values:
        entry = _close_journal_entry(
            c, tn, args.comment, portal=0, close_reason=close_reason,
            family=family, activity_id=act["ID"],
        )
        return _close_result(
            args, "close-endpoint", entry, None, False, work_time_entry, None,
            note=("close endpoint reported an error but the ticket reads back "
                  f"as closed (state {common.get('State')}); treated as success "
                  f"and only the close journal entry was written. error: "
                  f"{endpoint_error}"),
        )
    # Fallback: apply operator-reviewed state path, then write close audit entry.
    if family not in behavior["state_close_fallback_families"]:
        stop(f"close endpoint failed ({endpoint_error}); reviewed tenant behavior "
             f"does not allow state-close fallback for {family}")
    closed_state = _resolve_semantic_state(c, "closed")
    applied = []
    if preclose_state is not None and common.get("State") != preclose_state:
        try:
            common = _put_state_verified(c, tn, common, preclose_state)
        except M42Error as e:
            stop(f"close endpoint failed ({endpoint_error}); state-close fallback "
                 f"failed at the pre-close state: {e}", applied=applied)
        applied.append(f"State={preclose_state}")
        if add_processed_entry:
            processed = _gui_journal_entry(
                c, tn, "processed", portal=0, activity_id=act["ID"])
    try:
        _put_state_verified(c, tn, common, closed_state, Reason=close_reason)
    except M42Error as e:
        stop("state-close fallback could not verify closure; check ticket before "
             f"retrying: {e}", applied=applied)
    auto_warning = _auto_assign_recipient(c, act["ID"]) if auto_recipient else None
    entry = _close_journal_entry(
        c, tn, args.comment, portal=0, close_reason=close_reason,
        family=family, activity_id=act["ID"],
    )
    note = "close endpoint rejected the ticket; closed via state change instead"
    if _journal_warning(entry):
        note += (" WARNING: close journal entry failed — the required solution "
                 "comment is NOT in the journal; re-add with add-comment")
    return _close_result(args, "state-fragment-fallback", entry, processed,
                         add_processed_entry, work_time_entry, auto_warning,
                         note=note)


def cmd_reopen_ticket(args):
    """Reopen into configured live state, clear Reason, and add audit entry."""
    c = load_client()
    if not args.confirm:
        fail("--confirm is required: reopening mutates a closed ticket")
    tn = parse_ticket_number(args.ticket_number)
    act = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(tn)}", columns="ID")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    common = _ticket_common_fragment(c, tn)
    state_before = common.get("State") if common else None
    closed_values = _closed_state_values(c)
    if state_before not in closed_values:
        fail(f"ticket {args.ticket_number} is not closed (state={state_before})")
    _check_expected_timestamp(args, common, tn)
    reopen_semantic = c.tenant_profile["behavior"]["reopen_state"]
    if reopen_semantic is None:
        raise M42Error("tenant profile has no reopen state; rerun setup")
    reopen_state = _resolve_semantic_state(c, reopen_semantic)
    try:
        _put_state_verified(c, tn, common, reopen_state, Reason=None)
    except M42Error as e:
        fail(f"reopen failed: {e}")
    entry = _gui_journal_entry(c, tn, "reopen", args.comment, portal=0,
                               activity_id=act["ID"])
    auto_warning = None
    if (c.tenant_profile["behavior"]["auto_recipient_on_reopen"]
            and not args.no_auto_recipient):
        auto_warning = _auto_assign_recipient(c, act["ID"])
    out({"ok": True, "reopened": args.ticket_number,
         "state": reopen_state, "journal_entry": _journal_entry_id(entry),
         "journal_warning": _journal_warning(entry),
         "auto_recipient_warning": auto_warning,
         "portal_url": _portal_url(c, tn)})


def cmd_delete_journal(args):
    """Delete ONE journal entry (empty/orphaned artifacts from failed journal
    writes). Without --force only an empty plain comment (ActivityAction 0 or
    unset, no text) may be deleted; --confirm is always required (destructive,
    irreversible)."""
    c = load_client()
    if not args.confirm:
        fail("--confirm is required: journal deletion is irreversible")
    tn = parse_ticket_number(args.ticket_number)
    if not args.journal_id or not is_guid(args.journal_id):
        fail("--journal-id must be the journal entry GUID (see get-ticket "
             "journal[].id or the error output of add-comment)")
    entry = c.single(
        DD_JOURNAL,
        (f"ID={asql_quote(args.journal_id)} AND "
         f"T(SPSActivityClassBase).TicketNumber={asql_quote(tn)}"),
        columns="ID,ActivityAction,OriginalSolutionHtml")
    if not entry:
        exists = c.single(DD_JOURNAL, f"ID={asql_quote(args.journal_id)}",
                          columns="ID")
        if exists:
            fail(f"journal entry {args.journal_id} does not belong to ticket {tn}; "
                 "refusing cross-ticket deletion")
        fail(f"journal entry not found: {args.journal_id}")
    has_text = bool((entry.get("OriginalSolutionHtml") or "").strip())
    action = _int_or_raw(entry.get("ActivityAction"))
    is_template_entry = action not in (None, JOURNAL_COMMENT_ACTION)
    if has_text and not args.force:
        fail("entry still has text — refusing to delete without --force")
    if is_template_entry and not args.force:
        fail(f"entry uses journal template ActivityAction={action!r} (a native or "
             "mapped audit entry, not an empty comment) — refusing to delete "
             "without --force")
    c.request("DELETE", f"/api/data/fragments/{DD_JOURNAL}/{args.journal_id}")
    out({"ok": True, "deleted": args.journal_id, "ticket": args.ticket_number})


def cmd_my_tickets(args):
    """Open tickets for one user (or the token identity): the operator's daily
    entry point. Lists ticket number, state, age (days), subject — newest last."""
    c = load_client()
    if args.user:
        uid = _resolve_user_arg(c, args.user)
    else:
        uid = _current_identity(c)
    closed_values = _closed_state_values(c)
    closed_clause = ",".join(str(v) for v in sorted(closed_values))
    rows = c.fragments(DD_ACTIVITY,
                       where=(f"Recipient.ID={asql_quote(uid)} AND "
                              f"T(SPSCommonClassBase).State NOT IN "
                              f"({closed_clause})"),
                       columns=("ID,TicketNumber,Subject,CreatedDate,"
                                "T(SPSCommonClassBase).State.DisplayString as Status"),
                       max_records=500)
    today = datetime.now(timezone.utc).date()
    tickets = []
    for r in rows:
        age = None
        if r.get("CreatedDate"):
            try:
                cd = datetime.fromisoformat(
                    str(r["CreatedDate"]).replace("Z", "+00:00"))
                age = (today - cd.date()).days
            except ValueError:
                pass
        tickets.append({"ticket_number": r.get("TicketNumber"),
                        "state": r.get("Status"),
                        "age_days": age,
                        "subject": r.get("Subject")})
    tickets.sort(key=lambda t: ((t["age_days"] is None), -(t["age_days"] or 0)))
    out(_mark_truncated(
        {"ok": True, "user": uid, "count": len(tickets), "tickets": tickets}, rows))


def cmd_attachments(args):
    """List attachment metadata for a ticket. Read-only."""
    c = load_client()
    tn = parse_ticket_number(args.ticket_number)
    act = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(tn)}", columns="ID")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    try:
        rows = _ticket_attachments(c, tn, 500)
    except M42Error as e:
        out({"ok": True, "ticket": args.ticket_number, "count": 0,
             "attachments": [],
             "note": f"attachment DD not readable on this tenant ({str(e)[:120]})"})
        return
    out(_mark_truncated({"ok": True, "ticket": args.ticket_number,
                         "count": len(rows), "attachments": rows}, rows))


def cmd_search_kb(args):
    c = load_client()
    tags = [t.strip().lower() for t in args.tags.split(",") if t.strip()]
    if not tags:
        fail("--tags must contain at least one keyword")
    # Keyword matching stays client-side (Keywords is a comma list with no
    # verified server-side token match), but article bodies are fetched only
    # for the articles that match, instead of every portal-visible article.
    rows = c.fragments(DD_KB, where="VisibleInSSP = 1",
                       columns="ID,ArticleID,Subject,Keywords",
                       max_records=2000)
    scored = []
    for r in rows:
        kws = [k.strip().lower() for k in (r.get("Keywords") or "").split(",")]
        matches = sum(1 for t in tags if t in kws)
        if matches:
            scored.append((matches, r))
    scored.sort(key=lambda x: -x[0])
    selected = scored[:args.max]
    bodies = {}
    ids = [r["ID"] for _m, r in selected if r.get("ID")]
    if ids:
        in_clause = ",".join(asql_quote(i) for i in ids)
        for row in c.fragments(DD_KB, where=f"ID IN ({in_clause})",
                               columns="ID,SolutionText", max_records=len(ids)):
            bodies[row.get("ID")] = row.get("SolutionText")
    articles = [{"matches": m, **r, "SolutionText": bodies.get(r.get("ID"))}
                for m, r in selected]
    out(_mark_truncated({"ok": True, "count": len(scored), "articles": articles},
                        rows))


def cmd_list_services(args):
    c = load_client()
    # NOTE: plain catalog search — NOT filtered by what a specific user may order
    # (the API exposes no per-user entitlement filter here); do not promise
    # orderability to end users.
    rows = c.fragments(DD_SERVICE, where="",
                       columns="ID,Name,T(SPSCommonClassBase).State.DisplayString as Status")
    matched = rows
    if args.query:
        q = args.query.lower()
        matched = [r for r in rows if q in json.dumps(r, ensure_ascii=False).lower()]
    out(_mark_truncated(
        {"ok": True, "count": len(matched), "note": "unfiltered catalog; "
         "orderability per user is not checked", "services": matched[:args.max]},
        rows))


def cmd_list_categories(args):
    c = load_client()
    rows = c.fragments(DD_CATEGORY, where="", columns="ID,Name,Parent.Name as Parent")
    out(_mark_truncated({"ok": True, "count": len(rows), "categories": rows}, rows))


def cmd_list_pickup(args):
    c = load_client()
    dd = validate_dd_name(args.dd)
    rows = c.fragments(dd, where="", columns="ID,Value,DisplayString",
                       max_records=5000)
    out(_mark_truncated({"ok": True, "dd": dd, "count": len(rows), "values": rows},
                        rows))


def cmd_announcements(args):
    c = load_client()
    # Query portable columns and apply visibility/date filtering client-side.
    now = datetime.now(timezone.utc)
    rows = c.fragments(DD_ANNOUNCEMENT, where="",
                       columns="ID,Subject,Visible,VisibleFrom,VisibleUntil")
    active = []
    for r in rows:
        if r.get("Visible") in (0, 3):  # NEVER / RETIRED visibility
            continue
        vf, vu = r.get("VisibleFrom"), r.get("VisibleUntil")
        try:
            if vf and _parse_api_datetime(vf) > now:
                continue
            if vu and now >= _parse_api_datetime(vu, date_end=True):
                continue
        except ValueError:
            pass  # unparseable dates: keep entry, best-effort
        active.append(r)
    out(_mark_truncated({"ok": True, "count": len(active), "announcements": active},
                        rows))


def cmd_changes(args):
    c = load_client()
    lo = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    hi = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    rows = c.fragments(DD_CHANGE,
                       where=f"StartDateChange < #{hi}# AND EndDateChange > #{lo}#",
                       columns="ID,StartDateChange,EndDateChange")
    out(_mark_truncated({"ok": True, "count": len(rows), "changes": rows}, rows))


def cmd_user_data(args):
    c = load_client()
    user = _resolve_user_arg(c, args.user)
    row = c.single(DD_USER, f"ID={asql_quote(user)}",
                   columns="ID,DisplayName,FirstName,LastName,MailAddress,BusinessPhone,"
                           "MobilePhone,Department,Manager.ID as ManagerId,"
                           "Manager.DisplayName as ManagerName")
    if not row:
        fail(f"user not found: {args.user}")
    assets = c.fragments(DD_ASSET, where=f"AssignedUser={asql_quote(user)}",
                         columns="ID,Name,Description,InventoryNumber")
    out(_mark_truncated({"ok": True, "user": row, "assets": assets}, assets))


def _resolve_user_or_fail(c, ident):
    """Shared resolution: account name / email / display name. Raises M42Error
    on not-found and on ambiguous matches (candidates included)."""
    q = asql_quote(ident)
    rows = c.fragments(DD_ACCOUNT, f"AccountName={q}",
                       columns="ID,Owner.ID as OwnerId,Owner.DisplayName as OwnerName",
                       page_size=2, max_records=2)
    if len(rows) > 1:
        raise M42Error(f"ambiguous account name: {ident!r}")
    if rows:
        row = rows[0]
        owner = row.get("OwnerId")
        if isinstance(owner, dict):
            owner = owner.get("ID")
        if not is_guid(owner or ""):
            raise M42Error(f"account {ident!r} has no usable Person owner")
        return {"user_id": owner,
                "account_matched": True, "matched_by": "account"}
    rows = c.fragments(DD_USER, f"MailAddress={q}", columns="ID,DisplayName",
                       page_size=2, max_records=2)
    if len(rows) > 1:
        raise M42Error(f"ambiguous email address: {ident!r}")
    if rows:
        return {"user_id": rows[0]["ID"],
                "display_name": rows[0].get("DisplayName"),
                "matched_by": "email"}
    # A handful of rows is enough to tell "unique" from "ambiguous"; a name
    # shared by many people must not turn into a pagination failure.
    rows = c.fragments(DD_USER, f"DisplayName={q}",
                       columns="ID,DisplayName", page_size=10, max_records=10)
    if len(rows) == 1:
        return {"user_id": rows[0]["ID"], "display_name": rows[0].get("DisplayName"),
                "matched_by": "display_name"}
    if not rows:
        raise M42Error(f"user not found: {ident} (tried account, email, display name)")
    raise M42Error(f"ambiguous user: {ident}: "
                   f"{[r.get('DisplayName') for r in rows[:10]]}")


def _configure_output_streams():
    """JSON output is UTF-8 regardless of the locale. Without this a successful
    mutation followed by a UnicodeEncodeError on a non-UTF-8 stdout would be
    reported as a failure and retried."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass


def _add_expected_timestamp(parser):
    parser.add_argument(
        "--expected-timestamp", default=None, metavar="TIMESTAMP",
        help="the `timestamp` value reported by get-ticket; the command stops "
             "before writing when the ticket changed since it was read")


def main():
    _configure_output_streams()
    parser = argparse.ArgumentParser(prog="m42", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "setup",
        help="discover tenant choices, then write reviewed config with --profile-file",
    )
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", default=None,
                   help="DEPRECATED: exposes the token in the process list and "
                        "shell history. Export M42_API_TOKEN instead, or omit "
                        "both for a secure interactive prompt")
    p.add_argument("--profile-file", default=None,
                   help="operator-reviewed tenant-profile JSON; omit for read-only "
                        "discovery and setup questions")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("whoami", help="verify token works")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser(
        "tenant-config",
        help="show reviewed non-secret tenant behavior and value mappings",
    )
    p.set_defaults(func=cmd_tenant_config)

    p = sub.add_parser("resolve-user", help="find user GUID by account/email/name")
    p.add_argument("--user", required=True)
    p.set_defaults(func=cmd_resolve_user)

    p = sub.add_parser("search-tickets", help="ASQL query on tickets")
    p.add_argument("--where", required=True)
    p.add_argument("--columns", default=None)
    p.add_argument("--max", type=_max_records_arg, default=100,
                   help=f"row limit, 1..{MAX_RECORDS_CEILING} (default 100); the "
                        "output carries truncated=true when more rows may exist")
    p.set_defaults(func=cmd_search_tickets)

    p = sub.add_parser("get-ticket", help="full ticket + journal by ticket number")
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--portal-only", action="store_true",
                   help="only portal-visible journal entries")
    p.add_argument("--attachments", action="store_true",
                   help="also list ticket attachments (metadata)")
    p.add_argument("--columns", default=None,
                   help="extra ASQL columns for the ticket row (default covers "
                        "state/subject/initiator/urgency + ReminderDate)")
    p.set_defaults(func=cmd_get_ticket)

    p = sub.add_parser("create-ticket", help="create incident or service request")
    p.add_argument("--user", required=True, help="name/account/email or GUID")
    p.add_argument("--subject", required=True)
    p.add_argument("--description", required=True,
                   help="plain text only; newlines preserved, HTML escaped")
    p.add_argument("--category", default=None,
                   help="category GUID or exact name (mandatory on some tenants; "
                        "run list-categories first)")
    p.add_argument("--type", choices=["incident", "service-request"], default="incident")
    p.add_argument("--urgency", default=None,
                   help="configured urgency alias; omit for reviewed default")
    p.set_defaults(func=cmd_create_ticket)

    p = sub.add_parser("create-problem", help="create a problem record")
    p.add_argument("--subject", required=True)
    p.add_argument("--description", required=True,
                   help="plain text only; newlines preserved, HTML escaped")
    p.add_argument("--user", default=None, help="reporting user (optional)")
    p.add_argument("--urgency", default=None,
                   help="configured urgency alias; omit for reviewed default")
    p.set_defaults(func=cmd_create_problem)

    p = sub.add_parser("update-ticket",
                       help="update state/subject/urgency/priority/recipient/"
                            "resume-date of a ticket")
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--state", default=None,
                   help="configured state semantic (see tenant-config); a live "
                        "display name or numeric value is accepted only when it "
                        "maps to a reviewed profile state; a successful change "
                        "adds an internal journal audit entry")
    p.add_argument("--allow-unreviewed-state", action="store_true",
                   help="allow a live state value or display name that is not "
                        "in the reviewed profile states (closed states stay "
                        "blocked); only after explicit human approval")
    p.add_argument("--subject", default=None)
    p.add_argument("--urgency", default=None,
                   help="configured urgency alias (see tenant-config)")
    p.add_argument("--priority", type=_priority_arg, default=None,
                   help=f"numeric priority {PRIORITY_RANGE[0]}..{PRIORITY_RANGE[1]}; "
                        "written as given because the API exposes no priority "
                        "inventory to validate against")
    p.add_argument("--recipient", default=None, metavar="USER",
                   help="set responsible (SPSActivityClassBase.Recipient): "
                        "name/account/email or GUID")
    p.add_argument("--auto-recipient", action="store_true",
                   help="set recipient to the API token's own identity (may also "
                        "happen for setup-configured state transitions)")
    p.add_argument("--no-auto-recipient", action="store_true",
                   help="suppress setup-configured automatic recipient assignment")
    p.add_argument("--resume-at", default=None, metavar="DATETIME",
                   help="automatic resume date for paused tickets (writes "
                        "ReminderDate, same field the pause wizard uses); "
                        "ISO 8601 (naive times = UTC), e.g. 2026-09-10T08:00:00Z; "
                        "'never'/'clear' clears the resume date")
    p.add_argument("--category", default=None,
                   help="re-categorize the ticket: category GUID or exact name "
                        "(run list-categories first)")
    _add_expected_timestamp(p)
    p.set_defaults(func=cmd_update_ticket)

    p = sub.add_parser("list-roles",
                       help="list operator-approved forward roles from tenant config")
    p.set_defaults(func=cmd_list_roles)

    p = sub.add_parser("forward-ticket",
                       help="forward ticket to a configured role or user "
                            "using reviewed tenant assignment behavior")
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--target", required=True, metavar="USER",
                   help="configured role alias when --to-role; otherwise account, "
                        "email, display name, or user GUID")
    p.add_argument("--to-role", action="store_true",
                   help="forward to a role; a reviewed profile may enable the "
                        "tenant's role-forward journal label")
    p.add_argument("--comment", default=None,
                   help="optional plain-text internal note appended to the "
                        "forward entry; HTML is escaped")
    _add_expected_timestamp(p)
    p.set_defaults(func=cmd_forward_ticket)

    p = sub.add_parser("reopen-ticket",
                       help="reopen a closed ticket using reviewed state and "
                            "responsible-person behavior")
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--comment", default=None,
                   help="optional plain-text internal note stored with the "
                        "reopen entry; HTML is escaped")
    p.add_argument("--no-auto-recipient", action="store_true",
                   help="do not set the responsible to the token identity")
    p.add_argument("--confirm", action="store_true",
                   help="required: reopen mutates a closed ticket")
    _add_expected_timestamp(p)
    p.set_defaults(func=cmd_reopen_ticket)

    p = sub.add_parser("delete-journal",
                       help="delete ONE journal entry (empty/orphaned artifacts "
                            "from failed journal writes); destructive")
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--journal-id", required=True,
                   help="journal entry GUID (get-ticket journal[].id)")
    p.add_argument("--force", action="store_true",
                   help="allow deleting an entry that still carries text or "
                        "uses a native/mapped journal template (ActivityAction)")
    p.add_argument("--confirm", action="store_true",
                   help="required: deletion is irreversible")
    p.set_defaults(func=cmd_delete_journal)

    p = sub.add_parser("my-tickets",
                       help="open tickets of one user (default: token identity) "
                            "with age in days — the daily queue view")
    p.add_argument("--user", default=None, help="name/account/email or GUID")
    p.set_defaults(func=cmd_my_tickets)

    p = sub.add_parser("attachments", help="list attachments of a ticket (read-only)")
    p.add_argument("--ticket-number", required=True)
    p.set_defaults(func=cmd_attachments)

    p = sub.add_parser("add-comment", help="add journal comment")
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--text", required=True,
                   help="plain text only; newlines preserved, HTML escaped")
    visibility = p.add_mutually_exclusive_group()
    visibility.add_argument("--internal", action="store_true",
                            help="keep internal")
    visibility.add_argument("--portal", action="store_true",
                            help="make visible in Self Service Portal")
    p.set_defaults(func=cmd_add_comment)

    p = sub.add_parser(
        "close-ticket",
        help="record required work time, then close ticket/problem with reason + comment",
    )
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--reason", required=True,
                   help="configured close-reason alias (see tenant-config)")
    p.add_argument("--comment", default="",
                   help="required internal plain-text solution summary recorded "
                        "with the close entry; HTML is escaped")
    p.add_argument("--work-minutes", required=True, type=_nonnegative_minutes,
                   metavar="MINUTES",
                   help="additional working time to record before close "
                        f"(0..{MAX_WORK_MINUTES}); ask on every close and use 0 "
                        "only when already fully tracked; booked to the token "
                        "identity")
    p.add_argument("--kb", default=None, metavar="GUID",
                   help="link a KB article to the close (KBArticle relation, "
                        "article GUID from search-kb)")
    p.add_argument("--notify-initiator", action="store_true",
                   help="ask the server to notify the initiator "
                        "(SendMailToInitiator); the --comment text is part of "
                        "the close request and may then reach the requester")
    p.add_argument("--no-auto-recipient", action="store_true",
                   help="do not set the responsible to the token identity on close")
    p.add_argument("--confirm", action="store_true",
                   help="required: close mutates ticket state irreversibly "
                        "(reopen later only via reopen-ticket)")
    _add_expected_timestamp(p)
    p.set_defaults(func=cmd_close_ticket)

    p = sub.add_parser("search-kb", help="KB articles by keyword tags")
    p.add_argument("--tags", required=True, help="comma-separated keywords")
    p.add_argument("--max", type=_max_records_arg, default=10,
                   help=f"articles to return with bodies, 1..{MAX_RECORDS_CEILING}")
    p.set_defaults(func=cmd_search_kb)

    p = sub.add_parser("list-services", help="unfiltered catalog services")
    p.add_argument("--query", default=None)
    p.add_argument("--max", type=_max_records_arg, default=50)
    p.set_defaults(func=cmd_list_services)

    p = sub.add_parser("list-categories", help="service desk categories")
    p.set_defaults(func=cmd_list_categories)

    p = sub.add_parser("list-pickup", help="list pickup values of a Data Definition")
    p.add_argument("--dd", required=True, help="e.g. SPSCommonPickupObjectStatus")
    p.set_defaults(func=cmd_list_pickup)

    p = sub.add_parser("announcements", help="active announcements")
    p.set_defaults(func=cmd_announcements)

    p = sub.add_parser("changes", help="changes in last/next 24h")
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("user-data",
                       help="person details + assigned assets (returns personal "
                            "data; share only inside the named ticket scope)")
    p.add_argument("--user", required=True)
    p.set_defaults(func=cmd_user_data)

    args = parser.parse_args()
    try:
        args.func(args)
    except M42Error as e:
        fail(str(e), **e.extra)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        fail(f"unexpected error: {e}")


if __name__ == "__main__":
    main()
