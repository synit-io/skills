#!/usr/bin/env python3
"""Inspect a saved HTML document for AI discovery and content review evidence.

This tool does not fetch URLs, render JavaScript, validate an entire website,
or calculate a search ranking score. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_LINKS = 300
MAX_HEADINGS = 150
MAX_ENTITIES = 100
LOW_TEXT_REVIEW_WORDS = 40
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
NONCONTENT_TAGS = {"script", "style", "template", "noscript", "head", "title"}
PRODUCT_TYPES = {"Product", "ProductGroup", "ProductModel"}
OFFER_TYPES = {"Offer", "AggregateOffer"}
SERVICE_SOFTWARE_TYPES = {"Service", "ProfessionalService", "SoftwareApplication", "WebApplication", "MobileApplication"}
ROBOT_META_NAMES = {
    "robots", "googlebot", "googlebot-news", "googlebot-image", "bingbot",
    "msnbot", "slurp", "duckduckbot", "baiduspider", "yandex", "yandexbot",
    "oai-searchbot", "gptbot", "chatgpt-user", "claudebot", "claude-searchbot",
    "claude-user", "perplexitybot", "perplexity-user",
}
PRODUCT_FIELDS = (
    "name", "@id", "sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13",
    "gtin14", "brand", "model", "version", "description", "url", "offers",
    "isVariantOf", "hasVariant", "variesBy",
)
OFFER_FIELDS = (
    "@id", "description", "price", "lowPrice", "highPrice", "priceCurrency", "availability",
    "priceValidUntil", "url", "seller", "itemCondition", "itemOffered",
    "priceSpecification", "shippingDetails", "hasMerchantReturnPolicy",
)
SERVICE_SOFTWARE_FIELDS = (
    "@id", "name", "url", "description", "provider", "areaServed", "offers",
    "softwareVersion", "operatingSystem", "applicationCategory", "license",
    "downloadUrl", "softwareHelp", "inLanguage",
)


def compact(value: str) -> str:
    return " ".join(value.split())


def bounded(value: str, limit: int = 240) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def public_http_url(value: str, base: str | None = None) -> str | None:
    """Filter URL syntax and private literal hosts; never resolve DNS or fetch."""
    try:
        candidate = urljoin(base, value.strip()) if base else value.strip()
        parts = urlsplit(candidate)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        _ = parts.port  # Reject malformed port declarations.
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in candidate) or "\\" in candidate:
            return None
        host = parts.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host or re.fullmatch(r"[\d.]+", host):
                return None
        return candidate
    except (ValueError, TypeError):
        return None


@dataclass
class Frame:
    tag: str
    suppressed: bool
    in_main: bool
    in_chrome: bool
    heading_index: int | None = None
    anchor_index: int | None = None
    ld_parts: list[str] | None = None


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[Frame] = []
        self.language: str | None = None
        self.title_parts: list[str] = []
        self.title_count = 0
        self.descriptions: list[str] = []
        self.meta_robots: list[dict] = []
        self.link_elements: list[dict] = []
        self.base_hrefs: list[str] = []
        self.headings: list[dict] = []
        self.anchors: list[dict] = []
        self.segments: list[tuple[str, bool, bool]] = []
        self.ld_scripts: list[str] = []
        self.visible_main_regions = 0
        self.script_count = 0
        self.data_nosnippet_elements = 0
        self.images = {"total": 0, "visible_missing_alt": 0, "visible_empty_alt": 0, "hidden": 0}
        self.table_count = 0
        self.main_table_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        parent = self.stack[-1] if self.stack else None
        style = attrs_dict.get("style") or ""
        hidden = (
            "hidden" in attrs_dict
            or (attrs_dict.get("aria-hidden") or "").lower() == "true"
            or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", style, re.I))
        )
        suppressed = bool(parent and parent.suppressed) or hidden or tag in NONCONTENT_TAGS
        begins_main = tag == "main" or (attrs_dict.get("role") or "").lower() == "main"
        in_main = bool(parent and parent.in_main) or begins_main
        in_chrome = bool(parent and parent.in_chrome) or tag in {"nav", "footer"}
        frame = Frame(tag, suppressed, in_main, in_chrome)

        if tag == "html" and self.language is None:
            self.language = attrs_dict.get("lang")
        if tag == "title":
            self.title_count += 1
        if tag == "base" and attrs_dict.get("href") is not None:
            self.base_hrefs.append(attrs_dict["href"] or "")
        if tag == "meta":
            name = (attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content") or ""
            if name == "description":
                self.descriptions.append(content)
            if name in ROBOT_META_NAMES:
                self.meta_robots.append({"name": name, "content": content})
        if tag == "link":
            self.link_elements.append({
                "rel": (attrs_dict.get("rel") or "").lower().split(),
                "href": attrs_dict.get("href") or "",
                "type": (attrs_dict.get("type") or "").lower(),
                "hreflang": attrs_dict.get("hreflang"),
            })
        if tag == "script":
            self.script_count += 1
            inert = any(item.tag in {"template", "noscript"} for item in self.stack)
            if not inert and (attrs_dict.get("type") or "").lower().split(";", 1)[0].strip() == "application/ld+json":
                frame.ld_parts = []
        if begins_main and not suppressed:
            self.visible_main_regions += 1
        if "data-nosnippet" in attrs_dict:
            self.data_nosnippet_elements += 1
        if re.fullmatch(r"h[1-6]", tag) and not suppressed:
            frame.heading_index = len(self.headings)
            self.headings.append({"level": int(tag[1]), "parts": [], "in_main": in_main})
        if tag == "a" and attrs_dict.get("href") is not None and not suppressed:
            frame.anchor_index = len(self.anchors)
            self.anchors.append({"href": attrs_dict["href"] or "", "parts": [], "in_main": in_main})
        if tag == "img":
            self.images["total"] += 1
            if suppressed:
                self.images["hidden"] += 1
            elif "alt" not in attrs_dict:
                self.images["visible_missing_alt"] += 1
            elif not compact(attrs_dict.get("alt") or ""):
                self.images["visible_empty_alt"] += 1
        if tag == "table" and not suppressed:
            self.table_count += 1
            if in_main and not in_chrome:
                self.main_table_count += 1
        if tag not in VOID_TAGS:
            self.stack.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i].tag == tag), None)
        if index is None:
            return
        for frame in self.stack[index:]:
            if frame.ld_parts is not None:
                self.ld_scripts.append("".join(frame.ld_parts))
        del self.stack[index:]

    def handle_data(self, data: str) -> None:
        if not self.stack:
            if compact(data):
                self.segments.append((data, False, False))
            return
        frame = self.stack[-1]
        if frame.ld_parts is not None:
            frame.ld_parts.append(data)
        if any(item.tag == "title" for item in self.stack):
            self.title_parts.append(data)
        if frame.suppressed:
            return
        for item in self.stack:
            if item.heading_index is not None:
                self.headings[item.heading_index]["parts"].append(data)
            if item.anchor_index is not None:
                self.anchors[item.anchor_index]["parts"].append(data)
        if compact(data):
            self.segments.append((data, frame.in_main, frame.in_chrome))

    def finish(self) -> None:
        self.close()
        for frame in self.stack:
            if frame.ld_parts is not None:
                self.ld_scripts.append("".join(frame.ld_parts))
                frame.ld_parts = None


def schema_types(node: dict) -> list[str]:
    raw = node.get("@type", [])
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [re.sub(r"^https?://schema\.org/", "", item) for item in values if isinstance(item, str)]


def field_evidence(node: dict, fields: tuple[str, ...]) -> dict:
    result = {}
    for field in fields:
        if field not in node:
            result[field] = {"present": False}
            continue
        value = node[field]
        item = {"present": True, "empty": value is None or value == "" or value == [] or value == {}}
        if isinstance(value, str):
            item["value"] = bounded(compact(value))
        elif value is None or isinstance(value, (int, float, bool)):
            item["value"] = value
        elif isinstance(value, list):
            item.update({"kind": "array", "items": len(value)})
        else:
            item.update({"kind": "object", "keys": sorted(value)[:30]})
        result[field] = item
    return result


def inspect_jsonld(scripts: list[str]) -> dict:
    records, entities, types = [], [], Counter()
    entity_count = 0
    service_software_entities = []
    service_software_count = 0
    for number, raw in enumerate(scripts, 1):
        try:
            data = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-JSON constant: " + value)))
            if not isinstance(data, (dict, list)):
                raise ValueError("JSON-LD document must contain an object or array")
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            records.append({"script": number, "valid_json": False, "error": bounded(str(exc))})
            continue
        records.append({"script": number, "valid_json": True})
        pending = [(data, "$")]
        while pending:
            item, path = pending.pop()
            if isinstance(item, dict):
                names = schema_types(item)
                types.update(names)
                relevant = set(names) & (PRODUCT_TYPES | OFFER_TYPES)
                if relevant:
                    entity_count += 1
                    if len(entities) < MAX_ENTITIES:
                        fields = PRODUCT_FIELDS if set(names) & PRODUCT_TYPES else OFFER_FIELDS
                        entities.append({"script": number, "json_path": path, "types": names, "fields": field_evidence(item, fields)})
                if set(names) & SERVICE_SOFTWARE_TYPES:
                    service_software_count += 1
                    if len(service_software_entities) < MAX_ENTITIES:
                        service_software_entities.append({"script": number, "json_path": path, "types": names, "fields": field_evidence(item, SERVICE_SOFTWARE_FIELDS)})
                pending.extend((value, path + "." + key) for key, value in reversed(list(item.items())) if isinstance(value, (dict, list)))
            elif isinstance(item, list):
                pending.extend((value, f"{path}[{i}]") for i, value in reversed(list(enumerate(item))) if isinstance(value, (dict, list)))
    return {
        "script_count": len(scripts), "scripts": records,
        "type_counts": dict(sorted(types.items())), "product_offer_entity_count": entity_count,
        "product_offer_entities": entities, "entities_truncated": entity_count > len(entities),
        "service_software_entity_count": service_software_count,
        "service_software_entities": service_software_entities,
        "service_software_entities_truncated": service_software_count > len(service_software_entities),
        "validation_scope": "JSON syntax and observed fields only; no full JSON-LD expansion, schema validation, rich-result eligibility check, or @id reference resolution.",
    }


def extract_excerpts(text: str, structured: dict) -> list[dict]:
    patterns: list[tuple[str, str]] = [
        ("price_or_currency", r"(?:[$€£¥]\s*\d[\d.,]*|\b(?:USD|EUR|GBP|JPY|CHF|CAD|AUD)\s*\d[\d.,]*|\b\d[\d.,]*\s*(?:USD|EUR|GBP|JPY|CHF|CAD|AUD)\b)"),
        ("version_or_release", r"\b(?:v\d+(?:\.\d+){1,3}|(?:version|release)(?:\s+v?\d+(?:\.\d+){0,3})?|beta|preview|deprecated)\b"),
    ]
    names = []
    for entity in structured["product_offer_entities"]:
        if set(entity["types"]) & PRODUCT_TYPES:
            name = entity["fields"].get("name", {}).get("value")
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    patterns.extend(("product_name", re.escape(name)) for name in names[:3])
    result, seen = [], set()
    for category, pattern in patterns:
        for count, match in enumerate(re.finditer(pattern, text, flags=re.I)):
            if count >= (1 if category == "product_name" else 3):
                break
            start, end = max(0, match.start() - 75), min(len(text), match.end() + 125)
            excerpt = bounded(text[start:end], 240)
            key = (category, excerpt)
            if key not in seen:
                seen.add(key)
                result.append({"category": category, "matched_text": match.group(), "excerpt": excerpt})
    return result


def analyze_html(html: str, source_url: str, source_file: str = "<memory>") -> dict:
    validated_url = public_http_url(source_url)
    if validated_url is None:
        raise ValueError("--url must be an absolute HTTP(S) URL without credentials or a private literal hostname/IP")
    parser = DocumentParser()
    parser.feed(html)
    parser.finish()
    observations = []

    def note(severity: str, code: str, message: str, **evidence: object) -> None:
        observations.append({"severity": severity, "code": code, "message": message, "evidence": evidence})

    effective_base = validated_url
    if parser.base_hrefs:
        resolved = public_http_url(parser.base_hrefs[0], validated_url)
        if resolved:
            effective_base = resolved
        else:
            note("warning", "UNUSABLE_BASE_URL", "The first base URL was excluded; relative URLs are reported against the supplied page URL.")
    main_selected = parser.visible_main_regions > 0
    selected = [text for text, is_main, chrome in parser.segments if not main_selected or (is_main and not chrome)]
    text = compact(" ".join(selected))
    words = len(text.split())
    title = compact(" ".join(parser.title_parts))
    headings = [{"level": row["level"], "text": compact(" ".join(row["parts"])), "in_main": row["in_main"]} for row in parser.headings]
    h1_count = sum(row["level"] == 1 for row in headings)
    metadata = {
        "title": bounded(title, 600), "title_element_count": parser.title_count,
        "language": parser.language, "descriptions": [bounded(value, 600) for value in parser.descriptions],
        "canonical_urls": [], "hreflang": [], "markdown_alternates": [], "describedby": [],
        "robots_meta": parser.meta_robots, "effective_base_url": effective_base,
        "data_nosnippet_element_count": parser.data_nosnippet_elements,
    }
    excluded_links = 0
    for row in parser.link_elements:
        url = public_http_url(row["href"], effective_base) if row["href"] else None
        if not url:
            excluded_links += 1
            continue
        if "canonical" in row["rel"] and url not in metadata["canonical_urls"]:
            metadata["canonical_urls"].append(url)
        if "alternate" in row["rel"] and row["hreflang"]:
            metadata["hreflang"].append({"language": row["hreflang"], "url": url})
        if "alternate" in row["rel"] and row["type"].split(";", 1)[0].strip() == "text/markdown":
            metadata["markdown_alternates"].append(url)
        if "describedby" in row["rel"]:
            metadata["describedby"].append(url)
    links, seen = [], set()
    for row in parser.anchors:
        url = public_http_url(row["href"], effective_base)
        if url is None:
            excluded_links += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        if len(links) < MAX_LINKS:
            links.append({"url": url, "text": bounded(compact(" ".join(row["parts"]))), "in_main": row["in_main"], "external_host": urlsplit(url).hostname != urlsplit(validated_url).hostname})
    structured = inspect_jsonld(parser.ld_scripts)
    if not title:
        note("warning", "EMPTY_TITLE", "No nonempty title text was found in the saved HTML.")
    elif parser.title_count > 1:
        note("warning", "MULTIPLE_TITLES", "Multiple title elements were observed; review the page template.", count=parser.title_count)
    if h1_count != 1:
        note("review", "H1_STRUCTURE", "Review whether the heading structure identifies the page clearly; this is not a ranking requirement.", visible_h1_count=h1_count)
    if not metadata["canonical_urls"]:
        note("review", "CANONICAL_NOT_OBSERVED", "No usable HTML canonical link was observed. Check intended URL handling and HTTP headers before changing anything.")
    elif len(metadata["canonical_urls"]) > 1:
        note("warning", "MULTIPLE_CANONICALS", "Different canonical destinations were found in HTML.", urls=metadata["canonical_urls"])
    for meta in parser.meta_robots:
        value = meta["content"]
        if re.search(r"(?<![\w-])(?:noindex|none)(?![\w-])", value, re.I):
            note("review", "INDEXING_RESTRICTION_META", "An indexing restriction was observed. Confirm its intended agent scope and purpose.", **meta)
        if re.search(r"(?<![\w-])nosnippet(?![\w-])|\bmax-snippet\s*:\s*0\b", value, re.I):
            note("review", "SNIPPET_RESTRICTION_META", "A snippet restriction was observed. Check provider behavior and the owner's preference.", **meta)
    if parser.images["visible_missing_alt"]:
        note("review", "IMAGES_WITHOUT_ALT", "Some apparently visible images lack an alt attribute; review their informational purpose.", count=parser.images["visible_missing_alt"])
    if words < LOW_TEXT_REVIEW_WORDS and parser.script_count:
        note("review", "LOW_TEXT_WITH_SCRIPTS", "Limited extracted text and scripts were observed. Compare the rendered page before inferring missing content; this threshold is a review heuristic, not an SEO word-count target.", words=words, review_threshold=LOW_TEXT_REVIEW_WORDS, scripts=parser.script_count)
    if not text:
        note("warning", "NO_EXTRACTED_TEXT", "No content text was extracted from the selected HTML region. Verify the input and rendered page.")
    invalid_jsonld = [row for row in structured["scripts"] if not row["valid_json"]]
    if invalid_jsonld:
        note("warning", "INVALID_JSONLD_JSON", "Some JSON-LD script content is not valid supported JSON.", scripts=invalid_jsonld)
    return {
        "schema_version": 1,
        "scope": {"kind": "single_saved_html_document", "file": source_file, "url": validated_url, "network_requests": 0, "javascript_rendered": False},
        "metadata": metadata,
        "headings": {"total": len(headings), "h1_count": h1_count, "items": headings[:MAX_HEADINGS], "truncated": len(headings) > MAX_HEADINGS},
        "content": {"selection": "visible_main_regions_excluding_nav_footer" if main_selected else "document_text_including_possible_navigation", "extraction_method": "HTML attribute heuristic; CSS and layout are not evaluated", "main_region_count": parser.visible_main_regions, "characters": len(text), "words": words, "preview": bounded(text, 1500), "excerpts": extract_excerpts(text, structured), "tables_in_document": parser.table_count, "tables_in_main": parser.main_table_count, "images": parser.images, "script_elements": parser.script_count},
        "links": {"unique_http_urls": len(seen), "items": links, "truncated": len(seen) > MAX_LINKS, "excluded_link_values": excluded_links, "filter": "HTTP(S), no credentials or private literal hosts; DNS and destination access are not checked."},
        "structured_data": structured,
        "observations": observations,
        "review_questions": [
            "Which source is authoritative for each product, plan, model, version, region, and current price?",
            "Are aliases, former names, variant identifiers, prerequisites, and unsupported capabilities explicit?",
            "Do price statements preserve currency, billing unit, commitment, taxes, region, and effective date?",
            "Do visible facts, JSON-LD, Markdown equivalents, and merchant feeds agree for the same variant?",
            "Are claims supported by scoped evidence, and are historical, beta, and planned features distinguished?",
            "Do the linked sources actually answer customers' suitability, compatibility, support, and return-policy questions?",
        ],
        "limitations": [
            "This is one supplied HTML snapshot, not a website crawl or a rendered-browser audit.",
            "HTTP status, redirects, response headers (including X-Robots-Tag), robots.txt, CDN behavior, authentication, and actual indexing are unknown.",
            "HTMLParser is not a browser DOM repair or CSS/layout engine. External CSS, pseudo-elements, shadow DOM, scripts, and interaction-dependent content are not evaluated.",
            "hidden, aria-hidden, and inline display:none/visibility:hidden are treated as suppressed; actual visual/accessibility behavior may differ.",
            "Structured-data presence is not proof of correctness, eligibility, indexing, citation, or recommendation. @id references are not resolved.",
            "Bot-named meta tags are inventoried as written; provider support and precedence are not inferred.",
            "Missing observations are review prompts, not proof that a product lacks a fact or that a provider cannot retrieve it.",
        ],
    }


def render_text(report: dict) -> str:
    lines = ["Content evidence review", f"Source: {report['scope']['url']}", "Scope: one saved HTML document; no network access or JavaScript rendering.", "", f"Title: {report['metadata']['title'] or '(not observed)'}", f"Language: {report['metadata']['language'] or '(not observed)'}", f"Text: {report['content']['words']} words; {report['content']['selection']}", f"Headings: {report['headings']['total']}; H1: {report['headings']['h1_count']}", f"JSON-LD: {report['structured_data']['script_count']} script(s); types: {', '.join(report['structured_data']['type_counts']) or '(none observed)'}", "", "Observations:"]
    for item in report["observations"]:
        lines.append(f"- [{item['severity']}] {item['code']}: {item['message']}")
        if item["evidence"]:
            lines.append("  Evidence: " + json.dumps(item["evidence"], ensure_ascii=False))
    if not report["observations"]:
        lines.append("- No configured heuristic observations; this is not a complete pass or SEO certification.")
    lines.extend(["", "Evidence excerpts:"])
    lines.extend(f"- {item['category']}: {item['excerpt']}" for item in report["content"]["excerpts"])
    lines.extend(["", "Review questions:"] + ["- " + question for question in report["review_questions"]])
    lines.extend(["", "Limits:"] + ["- " + limitation for limitation in report["limitations"]])
    return "\n".join(lines) + "\n"


def write_report(path: Path, text: str, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".content-audit-", delete=False) as output:
            temporary = Path(output.name)
            output.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--file", required=True, type=Path, help="Saved UTF-8 HTML document")
    cli.add_argument("--url", required=True, help="Public HTTP(S) page URL used to resolve relative links; never fetched")
    cli.add_argument("--format", choices=("json", "text"), default="json")
    cli.add_argument("--output", type=Path, help="Write the report here instead of stdout")
    cli.add_argument("--force", action="store_true", help="Allow replacement of an existing report (never the input HTML)")
    args = cli.parse_args(argv)
    try:
        if args.output and args.output.resolve() == args.file.resolve():
            raise ValueError("Output must not replace the input HTML file")
        with args.file.open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError(f"HTML input exceeds the {MAX_INPUT_BYTES}-byte limit; supply a smaller representative snapshot")
        html = raw.decode("utf-8-sig")
        report = analyze_html(html, args.url, str(args.file.resolve()))
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else render_text(report)
        if args.output:
            write_report(args.output, output, args.force)
        else:
            sys.stdout.write(output)
        return 0
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        print(f"content_audit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
