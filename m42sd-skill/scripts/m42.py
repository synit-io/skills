#!/usr/bin/env python3
"""Matrix42 ESM Public API CLI for helpdesk agent workflows.

Stateless subcommands, JSON output. Config: env M42_BASE_URL/M42_API_TOKEN
or m42_config.json next to this script (written by `setup`).
"""
import argparse
import copy
import html
import ipaddress
import json
import math
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "m42_config.json")

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


class M42Error(Exception):
    pass


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


def _nonnegative_minutes(value):
    """Argparse type for an explicit close-time work-duration answer."""
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number of minutes")
    if not math.isfinite(minutes) or minutes < 0:
        raise argparse.ArgumentTypeError("must be a finite number at least 0")
    return minutes


def parse_ticket_number(ticket_number, c=None):
    """Validate and normalize a tenant ticket number."""
    tn = str(ticket_number).strip().upper()
    match = TICKET_NUMBER_RE.match(tn)
    if not match or len(tn) > 64:
        raise M42Error(f"invalid ticket number format: {ticket_number!r} "
                       f"(expected a prefix followed by digits)")
    return tn, None


def is_guid(value):
    return bool(GUID_RE.match(str(value).strip()))


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
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise M42Error(
                f"tenant profile {section}.{key} must be a non-negative integer"
            )


def validate_tenant_profile(profile):
    """Validate operator-reviewed tenant values without tenant defaults."""
    if not isinstance(profile, dict):
        raise M42Error("tenant profile must be a JSON object")
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
    urgency_default = profile.get("urgency_default")
    if urgency_default is not None and urgency_default not in merged["urgency"]:
        raise M42Error(
            "tenant profile urgency_default must name a configured urgency alias"
        )
    merged["urgency_default"] = urgency_default
    if "state_group" in profile:
        value = profile["state_group"]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise M42Error("tenant profile state_group must be null or a non-negative integer")
        merged["state_group"] = value
    if "impact_default" in profile:
        value = profile["impact_default"]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
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
        if family is not None and family not in TICKET_FAMILIES:
            raise M42Error(
                f"tenant profile ticket_prefixes.{prefix} has unknown family {family!r}"
            )
        merged["ticket_prefixes"][prefix.strip().upper()] = family
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
        merged["roles"][alias] = {"id": role["id"], "name": name}
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
        if not isinstance(portal_template, str) or "{ticket_number}" not in portal_template:
            raise M42Error(
                "tenant profile portal_url_template must contain {ticket_number} or be null"
            )
        parsed_portal = urllib.parse.urlparse(
            portal_template.replace("{ticket_number}", "ticket")
        )
        if (parsed_portal.scheme != "https" or not parsed_portal.hostname
                or parsed_portal.username or parsed_portal.password):
            raise M42Error("tenant profile portal_url_template must be an HTTPS URL")
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


class Client:
    def __init__(self, base_url, api_token, tenant_profile=None):
        self.base_url = normalize_base_url(base_url)
        self.api_token = api_token
        self.tenant_profile = validate_tenant_profile(
            {} if tenant_profile is None else tenant_profile)
        self._access_token = None
        self._access_exp = 0
        self._state_rows_cache = {}

    def _access(self):
        if self._access_token and time.time() < self._access_exp - 30:
            return self._access_token
        data = None
        errors = []
        content_types = ("application/json;charset=UTF-8", "text/json")
        for index, content_type in enumerate(content_types):
            req = urllib.request.Request(
                self.base_url + "/api/ApiToken/GenerateAccessTokenFromApiToken/",
                data=b"{}", method="POST",
                headers={
                    "Authorization": "Bearer " + self.api_token,
                    "Content-Type": content_type,
                })
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:200]
                errors.append(f"{content_type}: HTTP {e.code}: {detail!r}")
                if e.code in (401, 403) or index == len(content_types) - 1:
                    raise M42Error(f"token exchange failed: {'; '.join(errors)}")
        if data is None:
            raise M42Error("token exchange returned no response")
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
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    raise M42Error(f"non-JSON response on {method} {path}: "
                                   f"{raw[:200]!r}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            # Access-token lifetime is a guess (~280 s); a mid-session 401 means
            # it expired earlier — refresh once and retry the same request.
            if e.code == 401 and "GenerateAccessTokenFromApiToken" not in path:
                self._access_token = None
                self._access_exp = 0
                headers = dict(headers)
                headers["Authorization"] = "Bearer " + self._access()
                req = urllib.request.Request(url, data=data, method=method,
                                             headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        raw = resp.read().decode("utf-8")
                        if not raw.strip():
                            return None
                        try:
                            return json.loads(raw)
                        except json.JSONDecodeError:
                            raise M42Error(
                                f"non-JSON response on {method} {path}: "
                                f"{raw[:200]!r}")
                except urllib.error.HTTPError as e2:
                    retry_detail = e2.read().decode("utf-8", "replace")[:300]
                    raise M42Error(
                        f"HTTP {e2.code} on retry of {method} {path}: "
                        f"{retry_detail}")
                except urllib.error.URLError as retry_error:
                    if isinstance(getattr(retry_error, "reason", None),
                                  (TimeoutError, socket.timeout)):
                        raise M42Error(f"timeout on retry of {method} {path}")
                    raise M42Error(
                        f"connection error on retry of {method} {path}: "
                        f"{retry_error.reason}")
                except TimeoutError:
                    raise M42Error(f"timeout on retry of {method} {path}")
            raise M42Error(f"HTTP {e.code} on {method} {path}: {detail}")
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                raise M42Error(f"timeout on {method} {path}")
            raise M42Error(f"connection error on {method} {path}: {e.reason}")
        except TimeoutError:
            raise M42Error(f"timeout on {method} {path}")

    def fragments(self, dd, where="", columns="ID", page_size=1000, max_records=10000):
        """Page and deduplicate fragments; reject servers that repeat a full page."""
        if page_size < 1 or max_records < 1:
            raise M42Error("page_size and max_records must be positive")
        out = []
        seen = set()
        page = 0
        while True:
            params = {"where": where, "columns": columns,
                      "pageSize": page_size, "pageNumber": page}
            batch = self.request("GET", f"/api/data/fragments/{dd}", params=params)
            if not isinstance(batch, list):
                raise M42Error(f"unexpected response for {dd}: {str(batch)[:200]}")
            count_before = len(out)
            for row in batch:
                rid = row.get("ID") if isinstance(row, dict) else None
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                out.append(row)
                if len(out) >= max_records:
                    return out
            if len(batch) < page_size or len(out) >= max_records:
                break
            if len(out) == count_before:
                raise M42Error(f"pagination made no progress for {dd} at page {page}")
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


def load_client():
    env_base = os.environ.get("M42_BASE_URL")
    env_token = os.environ.get("M42_API_TOKEN")
    base = env_base
    token = env_token
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
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
    return client


def out(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def fail(msg, **extra):
    result = {"ok": False, "error": msg}
    result.update(extra)
    out(result)
    sys.exit(1)


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
            return {"available": True, "data_definition": dd, "rows": rows}
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
        "possibly_truncated": len(tickets["rows"]) >= ticket_limit,
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
    elif profile["roles"]:
        warnings.append(f"could not live-verify roles: {roles.get('error')}")
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


def cmd_setup(args):
    token = args.token or os.environ.get("M42_API_TOKEN")
    if not token:
        import getpass
        token = getpass.getpass("API token: ")
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
    # create with 0600 from the start (no world-readable window)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    result = {"ok": True, "configured": True, "written": CONFIG_PATH,
              "warning": "token stored plaintext; protect this file",
              "tenant_review_warnings": warnings,
              "token_expiry": _token_jwt_expiry(token)}
    out(result)


def cmd_tenant_config(args):
    """Return reviewed non-secret tenant choices used by operational commands."""
    c = load_client()
    review = None
    if getattr(c, "profile_source", None) == "config" and os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        review = stored.get("tenant_review")
    out({
        "ok": True,
        "base_url": c.base_url,
        "profile_source": getattr(c, "profile_source", None),
        "tenant_profile": c.tenant_profile,
        "tenant_review": review,
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
              "token_expiry": _token_jwt_expiry(_token_jwt())}
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
                       page_size=min(args.max, 1000), max_records=args.max)
    out({"ok": True, "count": len(rows), "tickets": rows})


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


def cmd_get_ticket(args):
    c = load_client()
    tn, ci = parse_ticket_number(args.ticket_number, c)
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
    journal = c.fragments(
        DD_JOURNAL,
        # Prefer documented T() navigation; retain the object-expression query
        # below as a guarded cross-version fallback.
        where=f"T(SPSActivityClassBase).TicketNumber={q}",
        columns="ID,CreatedDate,ActivityAction,Creator.ID as CreatorId,"
                "OriginalSolutionHtml,VisibleInPortal",
        max_records=5000)
    if not journal:
        journal = c.fragments(
            DD_JOURNAL,
            where=f"[Expression-ObjectID]='{act['ID']}'",
            columns="ID,CreatedDate,ActivityAction,Creator.ID as CreatorId,"
                    "OriginalSolutionHtml,VisibleInPortal",
            max_records=5000)
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
              "journal": entries}
    if args.attachments:
        try:
            atts = c.fragments("SPSActivityClassAttachment",
                               where=f"T(SPSActivityClassBase).TicketNumber={asql_quote(tn)}",
                               columns="ID,Name,CreatedDate,FileSize",
                               max_records=200)
            result["attachments"] = atts
        except M42Error as e:
            result["attachments"] = []
            result["attachments_note"] = f"not readable on this tenant ({str(e)[:120]})"
    out(result)


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
    """Accept a GUID (validated) or a name/email/account -> user GUID."""
    if user.count("-") == 4 and is_guid(user):
        return user.strip()
    return _resolve_user_or_fail(c, user)["user_id"]


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
    rows = _created_activity_candidates(c, result, args.subject)
    if not rows:
        out({"ok": True, "object_id": None, "ticket_number": None,
             "type": args.type,
             "note": "created, but readback failed; check via search-tickets"})
        return
    if len(rows) > 1:
        out({"ok": True, "object_id": None, "ticket_number": None,
             "type": args.type, "candidates": rows,
             "note": "multiple matches for subject; verify via search-tickets "
                     "before acting on the new ticket"})
        return
    out({"ok": True, "object_id": rows[0]["ID"],
         "ticket_number": rows[0].get("TicketNumber"),
         "portal_url": _portal_url(c, rows[0].get("TicketNumber") or ""),
         "type": args.type})


def _resolve_category_name(c, category):
    """Category GUID by exact name (or pass through a GUID)."""
    if is_guid(category):
        return category.strip()
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
    rows = _created_activity_candidates(c, result, args.subject)
    if not rows:
        out({"ok": True, "object_id": None, "ticket_number": None,
             "type": "problem",
             "note": "created, but readback failed; check via search-tickets"})
        return
    if len(rows) > 1:
        out({"ok": True, "object_id": None, "ticket_number": None,
             "type": "problem", "candidates": rows,
             "note": "multiple matches for subject; verify via search-tickets"})
        return
    out({"ok": True, "object_id": rows[0]["ID"],
         "ticket_number": rows[0].get("TicketNumber"), "type": "problem"})


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
    """Read the ticket's SPSCommonClassBase fragment through T() navigation."""
    return c.single(
        DD_ACTIVITY, f"TicketNumber={asql_quote(ticket_number)}",
        columns="ID,T(SPSCommonClassBase).ID as CID,"
                "T(SPSCommonClassBase).State as State,"
                "T(SPSCommonClassBase).TimeStamp as TimeStamp")


def _fragment_put(c, dd, body):
    """PUT a single fragment via the fragments endpoint (works without objects.Update)."""
    return c.request("PUT", f"/api/data/fragments/{dd}", body=body)


def _activity_time_stamp(c, activity_id):
    """Fresh SPSActivityClassBase TimeStamp (concurrency token) for a ticket."""
    row = c.single(DD_ACTIVITY, f"ID='{activity_id}'", columns="ID,TimeStamp")
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
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", ci_type) or not is_guid(owner_id):
        raise M42Error("ticket owner metadata is invalid")
    return ci_type, owner_id


def cmd_forward_ticket(args):
    """Forward to configured role/user field and reviewed state behavior."""
    c = load_client()
    tn, ci = parse_ticket_number(args.ticket_number, c)
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
            _fragment_put(c, DD_COMMON, {"ID": common["CID"],
                                         "State": forward_state,
                                         "TimeStamp": common["TimeStamp"]})
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
    jid = _gui_journal_entry(c, tn, action, hint, portal=0)
    warning = _journal_warning(jid)
    out({"ok": True, "forwarded": args.ticket_number, "to": target_name,
         "role": bool(args.to_role), "state": applied.get("State"),
         "journal_entry": jid, "journal_warning": warning})


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
    if args.recipient and args.auto_recipient:
        fail("--recipient and --auto-recipient are mutually exclusive")
    if args.auto_recipient and args.no_auto_recipient:
        fail("--auto-recipient and --no-auto-recipient are mutually exclusive")
    c = load_client()
    tn, ci = parse_ticket_number(args.ticket_number, c)
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
    state_value = None
    state_semantic = None
    state_before_semantic = None
    if args.state:
        # Closing via --state is blocked: it would write a closed state without Reason,
        # without the mandatory solution comment and without the GUI-parity
        # close journal entry. Closing = close-ticket.
        state_value = _resolve_state_value(c, args.state)
        if state_value in closed_values:
            fail("--state closed is not supported: closing requires reason + "
                 "solution comment + close journal entry — use close-ticket "
                 "instead")
        state_semantic = _semantic_for_state_value(c, state_value)
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
    acting_user = _current_identity() if args.auto_recipient or implicit_auto else None
    applied = {}
    errors = []
    # state lives in SPSCommonClassBase -> update that fragment directly
    if args.state:
        if not common:
            fail("ticket has no common fragment (unexpected)")
        try:
            _fragment_put(c, DD_COMMON, {"ID": common["CID"], "State": state_value,
                                         "TimeStamp": common["TimeStamp"]})
            applied["State"] = state_value
        except M42Error as e:
            errors.append(f"state: {e}")
    # State-change audit entry. Known states use recognizable Matrix42 actions;
    # every other state gets an explicit internal transition comment.
    # Only when the state PUT actually succeeded (a failed PUT must not produce
    # a journal entry claiming the transition).
    state_entry_actions = {
        "in_progress": "takeover",
        "paused": "pause",
        "solved": "solved",
    }
    journal_entry = None
    if "State" in applied and state_value != state_before:
        if state_semantic == "in_progress" and state_before_semantic == "paused":
            action_name = "resume"
        elif state_semantic in state_entry_actions:
            action_name = state_entry_actions[state_semantic]
        else:
            action_name = "state_change"
        body_text = (f"State changed to {args.state}."
                     if action_name == "state_change" else None)
        journal_entry = _gui_journal_entry(
            c, tn, action_name, body_text, portal=0)
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
             f"errors: {'; '.join(errors)}", applied=list(applied))
    out({"ok": True, "updated": args.ticket_number, "applied": applied,
         "journal_entry": journal_entry,
         "journal_warning": _journal_warning(journal_entry)
         if "State" in applied and state_value != state_before else None})


def _token_jwt(c=None):
    """Return the API token JWT string for identity decoding: env var wins over
    config file (mirrors load_client precedence)."""
    tok = os.environ.get("M42_API_TOKEN")
    if tok:
        return tok
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("api_token") or ""
    return ""


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


def _current_identity(c=None):
    """Person GUID of the API token's identity. The API token is a JWT whose
    payload carries UserFragmentId for the token owner's SPSUserClassBase row.
    Works for both config-file and env-var setup."""
    payload = _decode_jwt_payload(_token_jwt())
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
        # date-only: treat as local midnight -> keep date literal semantics
        try:
            d = datetime.strptime(str(value).strip(), "%Y-%m-%d")
        except ValueError:
            raise M42Error(f"invalid date/time: {value!r} (use ISO 8601, "
                           f"e.g. 2026-09-10T08:00:00Z or 2026-09-10)")
        return d.strftime("%Y-%m-%dT00:00:00Z")
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


def _resolve_state_value(c, state_name):
    """Resolve and validate numeric values, live display names, or profile semantics."""
    name = _normalize_label(state_name)
    rows = _activity_state_rows(c)
    available_values = {int(r["Value"]) for r in rows}
    if name.isdigit():
        value = int(name)
        if value in available_values:
            return value
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
        return next(iter(exact))
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


def cmd_add_comment(args):
    c = load_client()
    tn, ci = parse_ticket_number(args.ticket_number, c)
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
    # Path B (no objects.Get/objects.Update needed): /api/journal/add creates the
    # linked entry, then a fragment PUT fills text and portal flag.
    # The two steps are NOT atomic: if the PUT fails after the POST succeeded,
    # an empty entry exists — report its JournalId for cleanup instead of
    # silently falling back to Path A (which would duplicate the entry).
    journal_id = None
    try:
        type_id, used_in_type = _journal_type_pair(c, tn)
        result = c.request("POST", "/api/journal/add",
                           body={"TypeId": type_id, "ObjectId": used_in_type,
                                 "TargetObjectId": act["ID"]})
        if not isinstance(result, dict) or not result.get("JournalId"):
            raise M42Error(f"unexpected /api/journal/add response: {str(result)[:200]}")
        journal_id = result["JournalId"]
        if not _journal_entry_belongs_to_ticket(c, journal_id, tn):
            raise M42Error("created journal entry is not linked to requested ticket; "
                           "refusing to fill it")
        c.request("PUT", f"/api/data/fragments/{DD_JOURNAL}",
                  body={"ID": journal_id,
                        "OriginalSolutionHtml": body_text,
                        "ActivityAction": JOURNAL_COMMENT_ACTION,
                        "VisibleInPortal": int(portal)})
        out({"ok": True, "added": True, "ticket": args.ticket_number,
             "journal_id": journal_id,
             "path": "journal/add+fragment", "visible_in_portal": portal})
        return
    except M42Error as e:
        if journal_id:
            fail(f"journal entry created but not filled — entry {journal_id} on "
                 f"{args.ticket_number} is empty; delete it with "
                 f"`delete-journal --journal-id {journal_id} --ticket-number "
                 f"{args.ticket_number} --confirm` or fill it manually. error: {e}",
                 journal_id=journal_id)
        first_error = str(e)
    # Path A fallback: documented object update with journal append
    # (needs objects.Get + objects.Update audiences + CI read/write).
    journal_fragment = {
        "OriginalSolutionHtml": body_text,
        "ActivityAction": JOURNAL_COMMENT_ACTION,
        "VisibleInPortal": int(portal),
    }
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


def _journal_warning(journal_entry):
    if journal_entry is None:
        return "journal entry was not created"
    if isinstance(journal_entry, str) and "unfilled:" in journal_entry:
        return "journal entry was created but not filled; clean it up before retrying"
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
                       *, close_reason=None):
    """Append an internal audit entry using only a target-owned journal pair.

    The reviewed tenant profile can map action_name to a recognizable GUI
    ActivityAction. Without one, a plain comment carries explicit event text.
    Returns JournalId, an unfilled marker, or None when creation was impossible.
    """
    journal_id = None
    try:
        type_id, used_in_type = _journal_type_pair(c, ticket_number)
        jid = c.request("POST", "/api/journal/add",
                        body={"TypeId": type_id, "ObjectId": used_in_type,
                              "TargetObjectId": _activity_id(c, ticket_number)})
        if isinstance(jid, dict) and jid.get("JournalId"):
            journal_id = jid["JournalId"]
            if not _journal_entry_belongs_to_ticket(c, journal_id, ticket_number):
                raise JournalPartial(
                    journal_id,
                    "created entry is not linked to requested ticket")
            action = _journal_action_value(c, action_name)
            body = {"ID": journal_id, "ActivityAction": action,
                    "VisibleInPortal": int(portal)}
            if action == JOURNAL_COMMENT_ACTION and not body_text:
                body_text = PORTABLE_JOURNAL_TEXT[action_name]
            if body_text:
                body["OriginalSolutionHtml"] = _plain_text_field(body_text)
            if (action_name == "close_task" and action != JOURNAL_COMMENT_ACTION
                    and close_reason is not None):
                body["OriginalSolution"] = _plain_text_value(body_text)
                body["SolutionParams"] = _task_close_solution_params(close_reason)
            try:
                c.request("PUT", f"/api/data/fragments/{DD_JOURNAL}", body=body)
            except M42Error as e:
                # POST succeeded, fill failed -> empty entry exists. Report the
                # id so the operator/agent can clean it up (delete-journal).
                raise JournalPartial(journal_id, str(e))
            return journal_id
    except JournalPartial as e:
        return f"{e.journal_id} (unfilled: {e.error})"
    except M42Error as e:
        if journal_id:
            return f"{journal_id} (unfilled: {e})"
    return None


class JournalPartial(Exception):
    def __init__(self, journal_id, error):
        super().__init__(error)
        self.journal_id = journal_id
        self.error = error


def _activity_id(c, ticket_number):
    row = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(ticket_number)}",
                   columns="ID")
    if not row:
        raise M42Error(f"ticket not found: {ticket_number}")
    return row["ID"]


def _close_journal_entry(c, ticket_number, body_text, portal=0, *,
                         close_reason=None, family=None):
    """Internal close audit entry using configured ticket family semantics."""
    ticket_family = family or _ticket_family(c, ticket_number)
    action = "close_task" if ticket_family == "task" else "close"
    if action == "close_task":
        return _gui_journal_entry(
            c, ticket_number, action, body_text, portal,
            close_reason=close_reason,
        )
    return _gui_journal_entry(c, ticket_number, action, body_text, portal)


def _record_close_work_time(c, activity_id, minutes, *, end=None):
    """Add one Matrix42 time-tracking fragment before ticket closure.

    The required CLI answer is additional time, not the ticket's aggregate.
    A zero answer explicitly means all work is already tracked and adds no row.
    Parent CI and closure activity type are resolved from live tenant data.
    """
    minutes = float(minutes)
    if not math.isfinite(minutes) or minutes < 0:
        raise M42Error("work minutes must be a finite number at least 0")
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
        "User": _current_identity(),
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


def cmd_close_ticket(args):
    c = load_client()
    if not args.confirm:
        fail("--confirm is required: closing mutates ticket state (reopen "
             "afterwards only via reopen-ticket)")
    if getattr(args, "work_minutes", None) is None:
        fail("--work-minutes is required: ask how many additional working-time "
             "minutes must be recorded before closing (0 = already fully tracked)")
    tn, ci = parse_ticket_number(args.ticket_number, c)
    q = asql_quote(tn)
    if not args.comment or not args.comment.strip():
        fail("--comment is required (non-empty): provide the internal plain-text "
             "solution summary described by the SKILL.md closing rules")
    act = c.single(DD_ACTIVITY, f"TicketNumber={q}", columns="ID,TicketNumber")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    common = _ticket_common_fragment(c, tn)
    state_before = common.get("State") if common else None
    closed_values = _closed_state_values(c)
    if state_before in closed_values:
        fail(f"ticket {args.ticket_number} is already closed")
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
    auto_recipient_on_close = family in behavior["auto_recipient_on_close"]
    close_reason = _profile_value(c, "close_reasons", args.reason)
    processed_jid = None
    work_time_entry = _record_close_work_time(
        c, act["ID"], args.work_minutes)
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
            processed_jid = _gui_journal_entry(
                c, tn, "processed", portal=0)
        jid = _close_journal_entry(
            c, tn, args.comment, portal=0, close_reason=close_reason,
            family=family,
        )
        if auto_recipient_on_close and not args.no_auto_recipient:
            try:
                _fragment_put(c, DD_ACTIVITY,
                              {"ID": act["ID"],
                               "TimeStamp": _activity_time_stamp(c, act["ID"]),
                               "Recipient": _current_identity()})
            except M42Error:
                pass
        out({"ok": True, "closed": args.ticket_number, "reason": args.reason,
             "path": "close-endpoint", "journal_entry": jid,
             "journal_warning": _journal_warning(jid),
             "processed_journal_entry": processed_jid,
             "processed_journal_warning": _journal_warning(processed_jid)
             if add_processed_entry else None,
             "work_minutes": args.work_minutes,
             "work_time_entry": work_time_entry})
        return
    except M42Error as e:
        endpoint_error = str(e)
    # Fallback: apply operator-reviewed state path, then write close audit entry.
    if family not in behavior["state_close_fallback_families"]:
        fail(
            f"close endpoint failed ({endpoint_error}); reviewed tenant behavior "
            f"does not allow state-close fallback for {family}"
        )
    if not common:
        fail(f"close endpoint failed ({endpoint_error}) and ticket has no "
             f"common fragment for fallback close")
    closed_state = _resolve_semantic_state(c, "closed")
    if preclose_state is not None and state_before != preclose_state:
        _fragment_put(c, DD_COMMON, {"ID": common["CID"], "State": preclose_state,
                                     "TimeStamp": common["TimeStamp"]})
        if add_processed_entry:
            processed_jid = _gui_journal_entry(
                c, tn, "processed", portal=0)
        common = _ticket_common_fragment(c, tn)  # fresh TimeStamp for step 2
        if not common:
            fail(f"close endpoint failed ({endpoint_error}); ticket was moved "
                 f"to {preclose_state} but the final {closed_state} step could "
                 f"not re-read the fragment — verify state and finish manually",
                 applied=[f"State={preclose_state}"])
    _fragment_put(c, DD_COMMON, {"ID": common["CID"],
                                 "State": closed_state,
                                 "Reason": close_reason,
                                 "TimeStamp": common["TimeStamp"]})
    readback = _ticket_common_fragment(c, tn)
    if not readback or readback.get("State") not in closed_values:
        fail(
            "state-close fallback could not verify closure; check ticket before retrying",
            work_minutes=args.work_minutes, work_time_entry=work_time_entry,
        )
    if auto_recipient_on_close and not args.no_auto_recipient:
        try:
            _fragment_put(c, DD_ACTIVITY,
                          {"ID": act["ID"],
                           "TimeStamp": _activity_time_stamp(c, act["ID"]),
                           "Recipient": _current_identity()})
        except M42Error:
            pass
    jid = _close_journal_entry(
        c, tn, args.comment, portal=0, close_reason=close_reason,
        family=family,
    )
    note = "close endpoint rejected the ticket; closed via state change instead"
    if jid is None or (isinstance(jid, str) and "unfilled" in jid):
        note += " WARNING: close journal entry failed — the required solution "
        note += "comment is NOT in the journal; re-add with add-comment"
    out({"ok": True, "closed": args.ticket_number, "reason": args.reason,
         "path": "state-fragment-fallback",
         "journal_entry": jid,
         "journal_warning": _journal_warning(jid),
         "processed_journal_entry": processed_jid,
         "processed_journal_warning": _journal_warning(processed_jid)
         if add_processed_entry else None,
         "work_minutes": args.work_minutes,
         "work_time_entry": work_time_entry,
         "note": note})


def cmd_reopen_ticket(args):
    """Reopen into configured live state, clear Reason, and add audit entry."""
    c = load_client()
    if not args.confirm:
        fail("--confirm is required: reopening mutates a closed ticket")
    tn, ci = parse_ticket_number(args.ticket_number, c)
    act = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(tn)}", columns="ID")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    common = _ticket_common_fragment(c, tn)
    state_before = common.get("State") if common else None
    closed_values = _closed_state_values(c)
    if state_before not in closed_values:
        fail(f"ticket {args.ticket_number} is not closed (state={state_before})")
    reopen_semantic = c.tenant_profile["behavior"]["reopen_state"]
    if reopen_semantic is None:
        raise M42Error("tenant profile has no reopen state; rerun setup")
    reopen_state = _resolve_semantic_state(c, reopen_semantic)
    _fragment_put(c, DD_COMMON, {"ID": common["CID"], "State": reopen_state,
                                 "Reason": None,
                                 "TimeStamp": common["TimeStamp"]})
    jid = _gui_journal_entry(c, tn, "reopen", args.comment, portal=0)
    if (c.tenant_profile["behavior"]["auto_recipient_on_reopen"]
            and not args.no_auto_recipient):
        try:
            _fragment_put(c, DD_ACTIVITY,
                          {"ID": act["ID"],
                           "TimeStamp": _activity_time_stamp(c, act["ID"]),
                           "Recipient": _current_identity()})
        except M42Error:
            pass
    out({"ok": True, "reopened": args.ticket_number,
         "state": reopen_state, "journal_entry": jid,
         "journal_warning": _journal_warning(jid),
         "portal_url": _portal_url(c, tn)})


def cmd_delete_journal(args):
    """Delete ONE journal entry (empty/orphaned artifacts from failed journal
    writes). Refuses entries that still carry text unless --force; --confirm
    required (destructive, irreversible)."""
    c = load_client()
    if not args.confirm:
        fail("--confirm is required: journal deletion is irreversible")
    tn, ci = parse_ticket_number(args.ticket_number, c)
    if not args.journal_id or not is_guid(args.journal_id):
        fail("--journal-id must be the journal entry GUID (see get-ticket "
             "journal[].id or the error output of add-comment)")
    rows = c.fragments(
        DD_JOURNAL,
        where=(f"ID={asql_quote(args.journal_id)} AND "
               f"T(SPSActivityClassBase).TicketNumber={asql_quote(tn)}"),
                       columns="ID,ActivityAction,OriginalSolutionHtml",
                       max_records=1)
    if not rows:
        exists = c.single(DD_JOURNAL, f"ID={asql_quote(args.journal_id)}",
                          columns="ID")
        if exists:
            fail(f"journal entry {args.journal_id} does not belong to ticket {tn}; "
                 "refusing cross-ticket deletion")
        fail(f"journal entry not found: {args.journal_id}")
    entry = rows[0]
    has_text = bool((entry.get("OriginalSolutionHtml") or "").strip())
    if has_text and not args.force:
        fail("entry still has text — refusing to delete without --force")
    c.request("DELETE", f"/api/data/fragments/{DD_JOURNAL}/{args.journal_id}")
    out({"ok": True, "deleted": args.journal_id, "ticket": args.ticket_number})


def cmd_my_tickets(args):
    """Open tickets for one user (or the token identity): the operator's daily
    entry point. Lists ticket number, state, age (days), subject — newest last."""
    c = load_client()
    if args.user:
        uid = _resolve_user_arg(c, args.user)
    else:
        uid = _current_identity()
    closed_values = _closed_state_values(c)
    closed_clause = ",".join(str(v) for v in sorted(closed_values))
    rows = c.fragments(DD_ACTIVITY,
                       where=(f"Recipient.ID='{uid}' AND "
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
    out({"ok": True, "user": uid, "count": len(tickets), "tickets": tickets})


def cmd_attachments(args):
    """List attachment metadata for a ticket. Read-only."""
    c = load_client()
    tn, ci = parse_ticket_number(args.ticket_number, c)
    act = c.single(DD_ACTIVITY, f"TicketNumber={asql_quote(tn)}", columns="ID")
    if not act:
        fail(f"ticket not found: {args.ticket_number}")
    try:
        rows = c.fragments("SPSActivityClassAttachment",
                           where=f"T(SPSActivityClassBase).TicketNumber={asql_quote(tn)}",
                           columns="ID,Name,CreatedDate,FileSize",
                           max_records=500)
    except M42Error as e:
        out({"ok": True, "ticket": args.ticket_number, "count": 0,
             "attachments": [],
             "note": f"attachment DD not readable on this tenant ({str(e)[:120]})"})
        return
    out({"ok": True, "ticket": args.ticket_number,
         "count": len(rows), "attachments": rows})


def cmd_search_kb(args):
    c = load_client()
    tags = [t.strip().lower() for t in args.tags.split(",") if t.strip()]
    rows = c.fragments(DD_KB, where="VisibleInSSP = 1",
                       columns="ID,ArticleID,Subject,Keywords,SolutionText",
                       max_records=2000)
    scored = []
    for r in rows:
        kws = [k.strip().lower() for k in (r.get("Keywords") or "").split(",")]
        matches = sum(1 for t in tags if t in kws)
        if matches:
            scored.append((matches, r))
    scored.sort(key=lambda x: -x[0])
    out({"ok": True, "count": len(scored),
         "articles": [{"matches": m, **r} for m, r in scored[:args.max]]})


def cmd_list_services(args):
    c = load_client()
    # NOTE: plain catalog search — NOT filtered by what a specific user may order
    # (the API exposes no per-user entitlement filter here). --user is accepted
    # for compatibility and ignored; do not promise orderability to end users.
    rows = c.fragments("SPSArticleClassBase", where="",
                       columns="ID,Name,T(SPSCommonClassBase).State.DisplayString as Status")
    if args.query:
        q = args.query.lower()
        rows = [r for r in rows if q in json.dumps(r, ensure_ascii=False).lower()]
    out({"ok": True, "count": len(rows), "note": "unfiltered catalog; "
         "orderability per user is not checked", "services": rows[:args.max]})


def cmd_list_categories(args):
    c = load_client()
    rows = c.fragments(DD_CATEGORY, where="", columns="ID,Name,Parent.Name as Parent")
    out({"ok": True, "count": len(rows), "categories": rows})


def cmd_list_pickup(args):
    c = load_client()
    rows = c.fragments(args.dd, where="", columns="ID,Value,DisplayString",
                       max_records=5000)
    out({"ok": True, "dd": args.dd, "count": len(rows), "values": rows})


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
    out({"ok": True, "count": len(active), "announcements": active})


def cmd_changes(args):
    c = load_client()
    lo = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    hi = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    rows = c.fragments(DD_CHANGE,
                       where=f"StartDateChange < #{hi}# AND EndDateChange > #{lo}#",
                       columns="ID,StartDateChange,EndDateChange")
    out({"ok": True, "count": len(rows), "changes": rows})


def cmd_user_data(args):
    c = load_client()
    user = _resolve_user_arg(c, args.user)
    row = c.single(DD_USER, f"ID='{user}'",
                   columns="ID,DisplayName,FirstName,LastName,MailAddress,BusinessPhone,"
                           "MobilePhone,Department,Manager.ID as ManagerId,"
                           "Manager.DisplayName as ManagerName")
    if not row:
        fail(f"user not found: {args.user}")
    assets = c.fragments("SPSAssetClassBase", where=f"AssignedUser='{user}'",
                         columns="ID,Name,Description,InventoryNumber")
    out({"ok": True, "user": row, "assets": assets})


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
    rows = c.fragments(DD_USER, f"DisplayName={q}",
                       columns="ID,DisplayName", page_size=10)
    if len(rows) == 1:
        return {"user_id": rows[0]["ID"], "display_name": rows[0].get("DisplayName"),
                "matched_by": "display_name"}
    if not rows:
        raise M42Error(f"user not found: {ident} (tried account, email, display name)")
    raise M42Error(f"ambiguous user: {ident}: "
                   f"{[r.get('DisplayName') for r in rows[:10]]}")


def main():
    parser = argparse.ArgumentParser(prog="m42", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "setup",
        help="discover tenant choices, then write reviewed config with --profile-file",
    )
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", default=None,
                   help="API token; omit for secure prompt (avoids token in "
                        "shell history and process list)")
    p.add_argument("--profile-file", default=None,
                   help="operator-reviewed tenant-profile JSON; omit for read-only "
                        "discovery and setup questions")
    p.add_argument("--verify", action="store_true",
                   help="deprecated compatibility flag; setup always performs "
                        "read-only live discovery before writing")
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
    p.add_argument("--max", type=int, default=100)
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
                   help="configured state semantic, live display name, or live value; "
                        "see tenant-config; a "
                        "successful change adds an internal journal audit entry")
    p.add_argument("--subject", default=None)
    p.add_argument("--urgency", default=None,
                   help="configured urgency alias (see tenant-config)")
    p.add_argument("--priority", type=int, default=None)
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
    p.set_defaults(func=cmd_reopen_ticket)

    p = sub.add_parser("delete-journal",
                       help="delete ONE journal entry (empty/orphaned artifacts "
                            "from failed journal writes); destructive")
    p.add_argument("--ticket-number", required=True)
    p.add_argument("--journal-id", required=True,
                   help="journal entry GUID (get-ticket journal[].id)")
    p.add_argument("--force", action="store_true",
                   help="allow deleting an entry that still carries text")
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
                   help="additional working time to record before close; ask on "
                        "every close and use 0 only when already fully tracked")
    p.add_argument("--kb", default=None, metavar="ID",
                   help="link a KB article to the close (KBArticle field)")
    p.add_argument("--notify-initiator", action="store_true",
                   help="ask the server to notify the initiator (SendMailToInitiator)")
    p.add_argument("--no-auto-recipient", action="store_true",
                   help="do not set the responsible to the token identity on close")
    p.add_argument("--confirm", action="store_true",
                   help="required: close mutates ticket state irreversibly "
                        "(reopen later only via reopen-ticket)")
    p.set_defaults(func=cmd_close_ticket)

    p = sub.add_parser("search-kb", help="KB articles by keyword tags")
    p.add_argument("--tags", required=True, help="comma-separated keywords")
    p.add_argument("--max", type=int, default=10)
    p.set_defaults(func=cmd_search_kb)

    p = sub.add_parser("list-services", help="unfiltered catalog services")
    p.add_argument("--user", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--max", type=int, default=50)
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

    p = sub.add_parser("user-data", help="person details + assigned assets")
    p.add_argument("--user", required=True)
    p.set_defaults(func=cmd_user_data)

    args = parser.parse_args()
    try:
        args.func(args)
    except M42Error as e:
        fail(str(e))
    except Exception as e:  # noqa: BLE001 - CLI boundary
        fail(f"unexpected error: {e}")


if __name__ == "__main__":
    main()
