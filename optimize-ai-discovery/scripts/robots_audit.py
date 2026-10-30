#!/usr/bin/env python3
"""Read-only, explicit local robots.txt rule model. Python 3.10+, no dependencies.

This is not a universal provider-conformance checker or an HTTP/CDN audit.
The CLI never fetches URLs and never changes a robots.txt file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
TOKEN = re.compile(r"^[A-Za-z_-]+$")
HEX_PAIR = re.compile(r"^[0-9A-Fa-f]{2}$")


def normalize_octets(value: str) -> str:
    """Decode escaped unreserved octets; preserve escaped reserved octets.

    Non-ASCII text becomes uppercase UTF-8 percent escapes. Percent escapes are
    normalized once, never recursively. Raw URI delimiters remain delimiters.
    """
    result = []
    pos = 0
    while pos < len(value):
        char = value[pos]
        if char == "%" and pos + 2 < len(value) and HEX_PAIR.fullmatch(value[pos + 1:pos + 3]):
            octet = int(value[pos + 1:pos + 3], 16)
            result.append(chr(octet) if octet in UNRESERVED else f"%{octet:02X}")
            pos += 3
        else:
            if ord(char) > 127:
                result.extend(f"%{octet:02X}" for octet in char.encode("utf-8"))
            else:
                result.append(char)
            pos += 1
    return "".join(result)


def octet_length(value: str) -> int:
    """Count normalized literal or escaped octets, not printed escape digits."""
    count = 0
    pos = 0
    while pos < len(value):
        if value[pos] == "%" and pos + 2 < len(value) and HEX_PAIR.fullmatch(value[pos + 1:pos + 3]):
            pos += 3
        else:
            pos += 1
        count += 1
    return count


def normalized_tokens(value: str) -> list[str]:
    """Keep escaped octets atomic so wildcards cannot split a %HH triplet."""
    result = []
    pos = 0
    while pos < len(value):
        if value[pos] == "%" and pos + 2 < len(value) and HEX_PAIR.fullmatch(value[pos + 1:pos + 3]):
            result.append(value[pos:pos + 3])
            pos += 3
        else:
            result.append(value[pos])
            pos += 1
    return result


def wildcard_match(pattern: list[str], target: list[str], anchored: bool) -> bool:
    """Greedy wildcard matching; avoid exponential regex backtracking."""
    pi = ti = 0
    star = -1
    retry = 0
    while ti < len(target):
        if pi < len(pattern) and pattern[pi] == "*":
            star = pi
            pi += 1
            retry = ti
        elif pi < len(pattern) and pattern[pi] == target[ti]:
            pi += 1
            ti += 1
        elif pi == len(pattern) and not anchored:
            return True
        elif star >= 0:
            retry += 1
            ti = retry
            pi = star + 1
        else:
            return False
    while pi < len(pattern) and pattern[pi] == "*":
        pi += 1
    return pi == len(pattern)


@dataclass
class Rule:
    directive: str
    pattern: str
    line: int
    normalized: str = field(init=False)
    specificity: int = field(init=False)
    pattern_tokens: list[str] = field(init=False, repr=False)
    anchored: bool = field(init=False)

    def __post_init__(self):
        normalized = normalize_octets(self.pattern)
        anchored = normalized.endswith("$")
        body = normalized[:-1] if anchored else normalized
        body = re.sub(r"\*+", "*", body)
        # A trailing wildcard adds no constraint to a prefix rule.
        if not anchored:
            body = body.rstrip("*")
        self.normalized = body + ("$" if anchored else "")
        self.specificity = octet_length(body)
        self.pattern_tokens = normalized_tokens(body)
        self.anchored = anchored

    def matches(self, path: str) -> bool:
        return wildcard_match(self.pattern_tokens, normalized_tokens(path), self.anchored)

    def to_dict(self) -> dict:
        return {"directive": self.directive, "pattern": self.pattern,
                "line": self.line, "normalized_pattern": self.normalized,
                "specificity_octets": self.specificity}


@dataclass
class Group:
    agents: list[str]
    start_line: int
    rules: list[Rule] = field(default_factory=list)
    has_rule_records: bool = False


@dataclass
class Document:
    source: str
    groups: list[Group] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    extra_records: list[dict] = field(default_factory=list)

    @property
    def valid(self):
        return not any(item["severity"] == "error" for item in self.diagnostics)

    def issue(self, severity, code, line, detail):
        self.diagnostics.append({"severity": severity, "code": code,
                                 "line": line, "detail": detail})

    def to_dict(self):
        return {"source": self.source, "valid": self.valid,
                "groups": [{"agents": group.agents, "start_line": group.start_line,
                            "rules": [rule.to_dict() for rule in group.rules]}
                           for group in self.groups],
                "extra_records": self.extra_records, "diagnostics": self.diagnostics}


def parse_robots(text: str, source="<text>") -> Document:
    document = Document(source)
    if text.startswith("\ufeff"):
        text = text[1:]
        document.issue("info", "bom_removed", 1, "Ignored the leading UTF-8 BOM.")
    current = None
    for line_number, raw in enumerate(text.split("\n"), 1):
        raw = raw.removesuffix("\r")
        content = raw.split("#", 1)[0].strip(" \t")
        if not content:
            continue
        if any(ord(char) < 32 for char in content if char != "\t") or "\x7f" in content:
            document.issue("error", "control_character", line_number,
                           "A record contains an unsupported control character.")
            continue
        if ":" not in content:
            document.issue("error", "missing_colon", line_number,
                           "Expected a field-name: value record.")
            continue
        key, value = content.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or current.has_rule_records:
                current = Group([], line_number)
                document.groups.append(current)
            if value != "*" and not TOKEN.fullmatch(value):
                document.issue("error", "invalid_agent_token", line_number,
                               "Use '*' or an ASCII product token containing letters, '-' and '_'; do not use a full HTTP User-Agent or a version suffix.")
            current.agents.append(value)
            continue
        if key in ("allow", "disallow"):
            if current is None:
                document.issue("error", "rule_without_group", line_number,
                               "Allow/Disallow must follow a User-agent group.")
                continue
            current.has_rule_records = True
            if not value:
                document.issue("info", "empty_rule_ignored", line_number,
                               "An empty Allow/Disallow value supplies no path restriction.")
                continue
            if not value.startswith("/"):
                document.issue("error", "non_absolute_rule", line_number,
                               "The local model requires path rules to start with '/'.")
                continue
            if any(char.isspace() for char in value):
                document.issue("error", "whitespace_in_rule", line_number,
                               "Encode whitespace in a path instead of using raw whitespace.")
                continue
            if re.search(r"%(?![0-9A-Fa-f]{2})", value):
                document.issue("warning", "literal_percent", line_number,
                               "A '%' is not followed by two hexadecimal digits; confirm the intended URL encoding.")
            rule = Rule(key, value, line_number)
            if any(previous.directive == key and previous.normalized == rule.normalized
                   for previous in current.rules):
                document.issue("info", "duplicate_rule", line_number,
                               "This normalized rule duplicates another rule in the group.")
            current.rules.append(rule)
            continue
        record = {"field": key, "value": value, "line": line_number}
        document.extra_records.append(record)
        if key == "sitemap":
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                document.issue("warning", "sitemap_url", line_number,
                               "Sitemap normally contains an absolute HTTP(S) URL; no URL is fetched.")
        elif key == "crawl-delay":
            document.issue("info", "provider_specific_directive", line_number,
                           "Crawl-delay is preserved but not applied to path decisions; support differs by provider.")
        else:
            document.issue("warning", "unmodeled_directive", line_number,
                           f"'{key}' is preserved but not evaluated; check the target provider's documentation.")
    if not document.groups:
        document.issue("info", "no_agent_groups", None,
                       "No User-agent groups were parsed. This local text model has no path restrictions.")
    return document


def load_robots(path: str) -> Document:
    return parse_robots(Path(path).read_text(encoding="utf-8"), str(path))


def request_path(value: str) -> str:
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("URLs must not contain raw whitespace or control characters.")
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError("URLs must use valid percent escapes; encode a literal '%' as '%25'.")
    parts = urlsplit(value)
    if value.startswith("/") and not value.startswith("//"):
        path = parts.path or "/"
    elif parts.scheme in ("http", "https") and parts.netloc:
        if parts.username is not None or parts.password is not None:
            raise ValueError("Do not pass URLs containing credentials.")
        path = parts.path or "/"
    else:
        raise ValueError("Supply an absolute HTTP(S) URL or a root-relative path beginning with '/'.")
    # A bare query separator can matter to path matching; fragments never do.
    if "?" in value.split("#", 1)[0]:
        path += "?" + parts.query
    return normalize_octets(path)


def evaluate(document: Document, agent: str, url: str, ua_mode="longest-substring") -> dict:
    if not TOKEN.fullmatch(agent):
        raise ValueError("--agent must be a product token: letters, '-' and '_' only.")
    path = request_path(url)
    result = {"agent": agent, "url": url, "normalized_path_and_query": path,
              "allowed": None, "decision": "unknown", "reason": None,
              "selected_groups": [], "winning_rules": [], "diagnostics": []}
    if not document.valid:
        result["reason"] = "source_contains_errors"
        return result
    candidates = []
    matched_tokens = set()
    for group in document.groups:
        matched = [token for token in group.agents if token != "*" and
                   ((token.lower() in agent.lower()) if ua_mode == "longest-substring"
                    else token.lower() == agent.lower())]
        if matched:
            matched_tokens.update(token.lower() for token in matched)
            candidates.append((max(len(token) for token in matched), group))
    if candidates:
        longest = max(score for score, _ in candidates)
        selected = [group for score, group in candidates if score == longest]
        if len(matched_tokens) > 1:
            result["diagnostics"].append({"severity": "warning",
                "code": "overlapping_agent_tokens",
                "detail": "More than one distinct token matches this product token. The selected matching policy chooses the longest; verify the provider's group-selection behavior.",
                "matching_tokens": sorted(matched_tokens)})
    else:
        selected = [group for group in document.groups if "*" in group.agents]
    result["selected_groups"] = [{"agents": group.agents, "start_line": group.start_line}
                                  for group in selected]
    matching = [rule for group in selected for rule in group.rules if rule.matches(path)]
    if matching:
        longest = max(rule.specificity for rule in matching)
        winners = [rule for rule in matching if rule.specificity == longest]
        allowed = any(rule.directive == "allow" for rule in winners)
        result["winning_rules"] = [rule.to_dict() for rule in winners]
        result["reason"] = "allow_wins_tie" if len({rule.directive for rule in winners}) > 1 else "most_specific_rule"
    else:
        allowed = True
        result["reason"] = "no_matching_rule" if selected else "no_matching_group"
    result["allowed"] = allowed
    result["decision"] = "allow" if allowed else "disallow"
    return result


LIMITATIONS = [
    "Local text evaluation only; no HTTP request or modification is performed.",
    "robots.txt expresses crawler preferences; it is not authentication or an indexing/answer guarantee.",
    "Fetch status, redirects, robots caching, file-size limits, encoding recovery, CDN/WAF rules and published bot identity are not checked.",
    "A supplied URL is evaluated against the supplied file; correct scheme/host/port and robots-file scope must be verified separately.",
    "Provider-specific directives and user-triggered fetching policies are not applied.",
    "Matching is an explicit approximation for review, not a claim of universal RFC/provider compliance; confirm ambiguous token or wildcard cases with the intended provider.",
]


def audit(document, agents, urls, candidate=None, ua_mode="longest-substring"):
    result = {"schema_version": 1, "tool": "robots_audit",
              "matching_policy": {"user_agent": ua_mode,
                "group_selection": "Merge equally specific matching groups; use '*' groups only if no specific token matches.",
                "rule_selection": "Longest normalized pattern in octets, with Allow winning equal-length ties.",
                "wildcards": "'*' matches zero or more normalized octets; terminal '$' anchors the end; duplicate '*' and unanchored trailing '*' are normalized.",
                "specificity": "Escapes count as one octet; interior '*' counts as one pattern octet; terminal '$' is excluded.",
                "encoding": "UTF-8 percent encoding; unreserved escapes decoded; reserved escapes preserved; path plus query, excluding fragment."},
              "baseline": document.to_dict(), "evaluations": [],
              "limitations": LIMITATIONS, "summary": {"broadened": 0, "narrowed": 0, "unchanged": 0, "unknown": 0}}
    if candidate is not None:
        result["candidate"] = candidate.to_dict()
    for agent in agents:
        for url in urls:
            before = evaluate(document, agent, url, ua_mode)
            if candidate is None:
                result["evaluations"].append(before)
                continue
            after = evaluate(candidate, agent, url, ua_mode)
            if before["allowed"] is None or after["allowed"] is None:
                change = "unknown"
            elif before["allowed"] == after["allowed"]:
                change = "unchanged"
            else:
                change = "broadened" if after["allowed"] else "narrowed"
            result["summary"][change] += 1
            result["evaluations"].append({"agent": agent, "url": url,
                "change": change, "before": before, "after": after})
    return result


def format_text(report):
    lines = ["ROBOTS.TXT LOCAL AUDIT", f"Matching policy: {report['matching_policy']['user_agent']}"]
    for name in ("baseline", "candidate"):
        if name not in report:
            continue
        document = report[name]
        lines.append(f"{name.title()}: {document['source']} (valid local syntax: {document['valid']})")
        for issue in document["diagnostics"]:
            lines.append(f"  {issue['severity'].upper()} line {issue['line']}: {issue['code']}: {issue['detail']}")
    for item in report["evaluations"]:
        if "change" in item:
            lines.append(f"{item['agent']} {item['url']}: {item['before']['decision']} -> {item['after']['decision']} [{item['change']}]")
            decisions = [("before", item["before"]), ("after", item["after"])]
        else:
            lines.append(f"{item['agent']} {item['url']}: {item['decision']}")
            decisions = [("decision", item)]
        for phase, decision in decisions:
            group_lines = ", ".join(str(group["start_line"]) for group in decision["selected_groups"]) or "none"
            rule_lines = ", ".join(str(rule["line"]) for rule in decision["winning_rules"]) or "none"
            lines.append(f"  {phase}: groups at lines {group_lines}; winning rules at lines {rule_lines}; {decision['reason']}")
            for issue in decision["diagnostics"]:
                lines.append(f"  {issue['severity'].upper()}: {issue['detail']}")
    if "candidate" in report:
        lines.append("Comparison counts: " + json.dumps(report["summary"], sort_keys=True))
    lines.append("Limitations:")
    lines.extend("  - " + text for text in report["limitations"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Existing local robots.txt")
    parser.add_argument("--candidate", help="Proposed local robots.txt; compare without changing either file")
    parser.add_argument("--agent", action="append", default=[], help="Crawler product token; repeat for multiple agents")
    parser.add_argument("--url", action="append", default=[], help="Absolute HTTP(S) URL or root-relative path; repeat")
    parser.add_argument("--ua-mode", choices=("longest-substring", "exact"), default="longest-substring")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--fail-on-broaden", action="store_true", help="Exit 1 if a compared test URL changes from disallow to allow")
    args = parser.parse_args(argv)
    if bool(args.agent) != bool(args.url):
        parser.error("Provide both --agent and --url to evaluate paths, or omit both for a syntax audit.")
    if args.fail_on_broaden and (not args.candidate or not args.agent):
        parser.error("--fail-on-broaden requires --candidate and at least one --agent/--url test pair.")
    try:
        baseline = load_robots(args.file)
        candidate = load_robots(args.candidate) if args.candidate else None
        report = audit(baseline, args.agent, args.url, candidate, args.ua_mode)
    except (OSError, UnicodeError, ValueError) as error:
        message = {"error": str(error), "tool": "robots_audit", "no_files_changed": True}
        print(json.dumps(message, ensure_ascii=False) if args.format == "json" else f"Error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else format_text(report))
    if not baseline.valid or (candidate is not None and not candidate.valid):
        return 2
    if args.fail_on_broaden and report["summary"]["broadened"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
