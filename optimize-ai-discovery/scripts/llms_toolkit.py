#!/usr/bin/env python3
"""Build, inspect, and locally serve llms.txt from a reviewed JSON manifest.

Python 3.10+, standard library only. This tool never fetches linked URLs.
Its linter checks a documented practical Markdown subset, not agent adoption.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit

VERSION = "1.0.0"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024  # Operational tool limit, not a proposal limit.
PLACEHOLDER = re.compile(r"\{\{[^}]+\}\}|__REPLACE_ME__|\[INSERT [^\]]+\]", re.I)


class ContentError(ValueError):
    def __init__(self, report):
        super().__init__("Content validation failed")
        self.report = report


def report(kind):
    return {"tool": "llms-toolkit", "version": VERSION, "kind": kind,
            "valid": True, "errors": [], "warnings": [], "info": []}


def add(result, severity, code, message, location=""):
    entry = {"code": code, "message": message}
    if location:
        entry["location"] = location
    result[severity].append(entry)
    if severity == "errors":
        result["valid"] = False


def is_example_url(url):
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == x or host.endswith("." + x)
               for x in ("example.com", "example.org", "example.net", "example", "invalid", "test"))


def url_problem(url, require_absolute=True):
    if not isinstance(url, str) or not url:
        return "Supply a nonempty URL string."
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in url):
        return "URLs must not contain whitespace or control characters."
    if any(c in url for c in '\\<>"{}|^'):
        return "Percent-encode characters that are not safe in a URL."
    if re.search(r"%(?![0-9A-Fa-f]{2})", url):
        return "A percent escape must contain two hexadecimal digits."
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return "The URL host, bracket syntax, or port is malformed."
    if parts.username is not None or parts.password is not None:
        return "Do not put credentials in public links."
    if parts.scheme.lower() in ("javascript", "data", "file", "vbscript"):
        return "Executable, local-file, and embedded-data URL schemes are not accepted."
    if require_absolute and (parts.scheme.lower() not in ("http", "https") or not parts.hostname):
        return "Use an absolute HTTP or HTTPS URL."
    return None


def url_key(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))


def single_line(value, location, result, allow_empty=False):
    if not isinstance(value, str):
        add(result, "errors", "text_type", "Expected a string.", location)
        return False
    if not allow_empty and not value.strip():
        add(result, "errors", "empty_text", "Expected nonempty text.", location)
        return False
    if value != value.strip():
        add(result, "errors", "outer_whitespace", "Remove leading and trailing whitespace.", location)
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in value):
        add(result, "errors", "control_character", "Use one line of text without control characters.", location)
    if PLACEHOLDER.search(value):
        add(result, "errors", "unresolved_placeholder", "Replace template markers with verified content.", location)
    return True


def validate_manifest(data, allow_example=False):
    result = report("manifest")
    if not isinstance(data, dict):
        add(result, "errors", "root_type", "The manifest must be a JSON object.", "$")
        return result
    allowed = {"schema_version", "name", "summary", "guidance", "sections", "reviewed_on"}
    for key in data.keys() - allowed:
        add(result, "errors", "unknown_field", "Unknown top-level field.", str(key))
    if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        add(result, "errors", "schema_version", "schema_version must be integer 1.", "schema_version")
    single_line(data.get("name"), "name", result)
    if "summary" in data:
        single_line(data["summary"], "summary", result)
    if "reviewed_on" in data:
        value = data["reviewed_on"]
        try:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            dt.date.fromisoformat(value)
        except ValueError:
            add(result, "errors", "review_date", "Use a real date in YYYY-MM-DD form.", "reviewed_on")
    guidance = data.get("guidance", [])
    if not isinstance(guidance, list):
        add(result, "errors", "guidance_type", "guidance must be an array of paragraph strings.", "guidance")
    else:
        for i, value in enumerate(guidance):
            single_line(value, f"guidance[{i}]", result)
    sections = data.get("sections", [])
    if not isinstance(sections, list):
        add(result, "errors", "sections_type", "sections must be an array.", "sections")
        return result
    headings, urls = set(), set()
    for i, section in enumerate(sections):
        loc = f"sections[{i}]"
        if not isinstance(section, dict):
            add(result, "errors", "section_type", "Each section must be an object.", loc)
            continue
        for key in section.keys() - {"title", "links"}:
            add(result, "errors", "unknown_field", "Unknown section field.", f"{loc}.{key}")
        title = section.get("title")
        if single_line(title, loc + ".title", result):
            key = title.casefold()
            if key in headings:
                add(result, "errors", "duplicate_heading", "Use distinct section titles.", loc + ".title")
            headings.add(key)
        links = section.get("links")
        if not isinstance(links, list) or not links:
            add(result, "errors", "links_type", "Each manifest section needs a nonempty links array.", loc + ".links")
            continue
        for j, link in enumerate(links):
            subloc = f"{loc}.links[{j}]"
            if not isinstance(link, dict):
                add(result, "errors", "link_type", "Each link must be an object.", subloc)
                continue
            for key in link.keys() - {"title", "url", "description"}:
                add(result, "errors", "unknown_field", "Unknown link field.", f"{subloc}.{key}")
            single_line(link.get("title"), subloc + ".title", result)
            if "description" in link:
                single_line(link["description"], subloc + ".description", result, allow_empty=True)
            url = link.get("url")
            problem = url_problem(url)
            if problem:
                add(result, "errors", "url", problem, subloc + ".url")
                continue
            key = url_key(url)
            if key in urls:
                add(result, "errors", "duplicate_url", "Each destination must appear only once in this manifest.", subloc + ".url")
            urls.add(key)
            if is_example_url(url):
                add(result, "warnings" if allow_example else "errors", "example_url",
                    "Reserved example/test destination: use --allow-example only for a demonstration.", subloc + ".url")
    if not urls:
        add(result, "info", "no_links", "A heading-only file is possible; useful navigation usually needs reviewed links.")
    result["link_count"] = len(urls)
    result["section_count"] = len(sections)
    return result


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("The manifest contains a duplicate JSON object key.")
        obj[key] = value
    return obj


def load_manifest(path, allow_example=False):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        result = report("manifest")
        add(result, "errors", "manifest_size", "Manifest exceeds this tool's 4 MiB operational limit.")
        raise ContentError(result)
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite numbers are not JSON.")))
    except (UnicodeError, ValueError) as exc:
        result = report("manifest")
        add(result, "errors", "invalid_json", "Use valid UTF-8 JSON, with unique object keys and finite numbers.")
        raise ContentError(result) from exc
    result = validate_manifest(data, allow_example=allow_example)
    if not result["valid"]:
        raise ContentError(result)
    return data, result


def escape_text(text):
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", text)


def render_manifest(data, allow_example=False):
    result = validate_manifest(data, allow_example=allow_example)
    if not result["valid"]:
        raise ContentError(result)
    lines = ["# " + escape_text(data["name"]), ""]
    if data.get("summary"):
        lines.extend(["> " + escape_text(data["summary"]), ""])
    if data.get("reviewed_on"):
        lines.extend(["Index review date: " + data["reviewed_on"] + ".", ""])
    for paragraph in data.get("guidance", []):
        lines.extend([escape_text(paragraph), ""])
    for section in data.get("sections", []):
        lines.extend(["## " + escape_text(section["title"]), ""])
        for link in section["links"]:
            # Preserve URL components; encode parentheses so Markdown cannot split the destination.
            url = quote(link["url"], safe=":/?#[]@!$&'*+,;=%~-._")
            item = "- [" + escape_text(link["title"]) + "](" + url + ")"
            if link.get("description"):
                item += ": " + escape_text(link["description"])
            lines.append(item)
        lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def atomic_write(path, payload, force=False):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".llms-", dir=target.parent, delete=False) as out:
            temporary = out.name
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o644)
        if force:
            os.replace(temporary, target)
            temporary = None
        else:
            # Same-directory hard link gives an atomic create that cannot clobber another writer.
            os.link(temporary, target)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def ensure_distinct_output(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("The output must not replace the source manifest, including a path alias.")
    if destination.exists() and os.path.samefile(source, destination):
        raise ValueError("The output must not replace a hard-link alias of the source manifest.")


def bracket(text, start, opener="[", closer="]"):
    if start >= len(text) or text[start] != opener:
        return None
    depth, i = 1, start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return None


def ref_key(value):
    return " ".join(value.split()).casefold()


def parse_link(text, references):
    first = bracket(text, 0)
    if first is None:
        return None, None, "Start each file-list item with a Markdown link."
    label, end = first
    if not label.strip():
        return None, None, "Give the link a nonempty label."
    if end < len(text) and text[end] == "(":
        if text[end + 1:end + 2] == "<":
            close = text.find(">", end + 2)
            if close < 0:
                return None, None, "The angle-bracket URL is not closed."
            url = text[end + 2:close]
            tail = text[close + 1:]
            match = re.match(r'''\s*(?:"(?:[^"\\]|\\.)*"\s*|'(?:[^'\\]|\\.)*'\s*)?\)''', tail)
            if not match:
                return None, None, "The Markdown link destination is not closed."
            rest = tail[match.end():]
        else:
            destination = bracket(text, end, "(", ")")
            if destination is None:
                return None, None, "The Markdown link destination is not closed."
            raw, stop = destination
            match = re.fullmatch(r'''(\S+?)(?:\s+(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))?''', raw)
            if not match:
                return None, None, "The link destination needs a URL, with an optional quoted title."
            url = re.sub(r"\\([\\()])", r"\1", match.group(1))
            rest = text[stop:]
    else:
        key = label
        if end < len(text) and text[end] == "[":
            second = bracket(text, end)
            if second is None:
                return None, None, "The reference label is not closed."
            key, end = second
            key = key or label
        url = references.get(ref_key(key))
        if url is None:
            return None, None, "The reference-style link has no matching definition."
        rest = text[end:]
    return url, rest, None


def lint_text(text):
    result = report("llms.txt")
    text = text.removeprefix("\ufeff")
    if re.search(r"^\s*(?:<!doctype\s+html|<html\b|<head\b|<body\b)", text, re.I):
        add(result, "errors", "html_document", "The response is an HTML document, not a Markdown index.")
    if "\x00" in text:
        add(result, "errors", "nul_character", "Remove NUL characters from the text.")
    if len(text.encode("utf-8")) > 100_000:
        add(result, "warnings", "large_index", "Consider scoped indexes; 100 kB is this tool's advisory threshold, not a format limit.")
    lines = text.splitlines()
    references, definition_lines, heading_lines, skipped, fence = {}, set(), {}, set(), None
    for i, line in enumerate(lines):
        fencing = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fencing:
            mark = fencing.group(1)
            if fence is None:
                fence = (mark[0], len(mark))
            elif mark[0] == fence[0] and len(mark) >= fence[1]:
                fence = None
            skipped.add(i)
            continue
        if fence:
            skipped.add(i)
            continue
        definition = re.match(r'^\s{0,3}\[([^\]]+)\]:\s*(?:<([^>]*)>|(\S+))(?:\s+.*)?$', line)
        if definition:
            key = ref_key(definition.group(1))
            if key in references:
                add(result, "warnings", "duplicate_reference", "The first matching link definition is used.", f"line {i + 1}")
            else:
                references[key] = definition.group(2) or definition.group(3)
            definition_lines.add(i)
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})(?:[ \t]+(.*)|$)", line)
        if heading:
            heading_lines[i] = (len(heading.group(1)), re.sub(r"\s+#+\s*$", "", heading.group(2) or "").strip())
        elif i > 0 and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", line) and lines[i - 1].strip() and i - 1 not in skipped and i - 1 not in heading_lines:
            heading_lines[i - 1] = (1 if line.lstrip().startswith("=") else 2, lines[i - 1].strip())
            skipped.add(i)
    if fence:
        add(result, "warnings", "unclosed_fence", "An unclosed fenced block needs manual review.")
    visible = [i for i, line in enumerate(lines) if line.strip() and i not in definition_lines]
    h1 = [i for i, (level, _) in heading_lines.items() if level == 1]
    if not h1:
        add(result, "errors", "missing_h1", "Start with an H1 containing the project or site name.")
    elif len(h1) > 1:
        add(result, "errors", "multiple_h1", "Keep one project/site H1.")
    if visible and (not h1 or visible[0] != h1[0]):
        add(result, "errors", "h1_position", "The first content must be the project/site H1.")
    sections, urls, current, last_link = {}, [], None, False
    for i, line in enumerate(lines):
        if i in definition_lines or not line.strip():
            continue
        loc = f"line {i + 1}"
        if i in heading_lines:
            level, title = heading_lines[i]
            last_link = False
            if not title:
                add(result, "errors", "empty_heading", "Give the heading a title.", loc)
            if level == 2:
                key = title.casefold()
                if key in sections:
                    add(result, "warnings", "duplicate_heading", "Distinct section names make navigation clearer.", loc)
                sections.setdefault(key, 0)
                current = key
            elif level > 2:
                add(result, "errors", "heading_level", "Use H2 for file-list sections; keep other guidance before them without headings.", loc)
            continue
        if current is None:
            continue
        if i in skipped:
            # Setext underline belongs to its heading; other fenced content needs manual inspection.
            if i > 0 and i - 1 in heading_lines and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", line):
                continue
            add(result, "warnings", "complex_section_content", "Fenced content within a link section needs manual format review.", loc)
            continue
        item = re.match(r"^\s{0,3}(?:[-+*]|\d+[.)])\s+(.+)$", line)
        if item is None:
            if line.startswith(("  ", "\t")) and last_link:
                continue  # A Markdown list item's continued notes.
            add(result, "errors", "section_non_link_content", "Place general guidance before the H2 file-list sections.", loc)
            last_link = False
            continue
        url, rest, problem = parse_link(item.group(1), references)
        if problem:
            add(result, "errors", "file_list_link", problem, loc)
            last_link = False
            continue
        last_link = True
        sections[current] += 1
        if rest.strip() and not rest.lstrip().startswith(":"):
            add(result, "warnings", "annotation_separator", "Separate optional link notes with a colon for proposal-compatible parsing.", loc)
        problem = url_problem(url, require_absolute=False)
        if problem:
            add(result, "errors", "url", problem, loc)
            continue
        parts = urlsplit(url)
        if not parts.scheme:
            add(result, "warnings", "relative_url", "Relative URL: resolve it against the published file before checking reachability.", loc)
        elif parts.scheme.lower() not in ("http", "https"):
            add(result, "warnings", "non_http_url", "Check whether the intended consumer can retrieve this URL scheme.", loc)
        elif not parts.hostname:
            add(result, "errors", "url_host", "HTTP(S) destinations need a host.", loc)
        key = url_key(url)
        if key in urls:
            add(result, "warnings", "duplicate_url", "The destination already appears in this index.", loc)
        urls.append(key)
        if is_example_url(url):
            add(result, "warnings", "example_url", "Replace example/test URLs before publication.", loc)
    for heading, count in sections.items():
        if count == 0:
            add(result, "warnings", "empty_section", "An empty file-list section provides no navigation.", heading)
    if not urls:
        add(result, "info", "no_links", "H1 alone is allowed; add reviewed links when useful.")
    result["link_count"] = len(urls)
    result["section_count"] = len(sections)
    result["scope"] = "Local structure and URL syntax only; no link fetching, factual verification, or adoption/ranking assessment."
    return result


class ManifestState:
    def __init__(self, manifest, allow_example=False):
        self.manifest = Path(manifest)
        self.allow_example = allow_example
        self.lock = threading.Lock()
        self.payload = b""
        self.etag = ""
        self.stale = False
        self.refresh(initial=True)

    def refresh(self, initial=False):
        with self.lock:
            try:
                data, _ = load_manifest(self.manifest, self.allow_example)
                payload = render_manifest(data, self.allow_example)
            except (OSError, ValueError) as exc:
                if initial:
                    raise
                if not self.stale:
                    print("Manifest is unavailable or invalid; serving the last valid index.", file=sys.stderr)
                self.stale = True
                return self.payload, self.etag, True
            self.payload = payload
            self.etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
            self.stale = False
            return self.payload, self.etag, False


def etag_matches(header, etag):
    if not header:
        return False
    return any(token.strip() == "*" or token.strip().removeprefix("W/") == etag
               for token in header.split(","))


def validate_endpoint(path):
    if not re.fullmatch(r"/(?:[A-Za-z0-9._~-]+/)*llms\.txt", path) or any(x in (".", "..") for x in path.split("/")):
        raise ValueError("Use an absolute endpoint path ending in llms.txt, without query, traversal, or empty segments.")


def make_handler(state, path="/llms.txt"):
    """Return the real HTTP handler independently of socket creation for portable tests."""
    validate_endpoint(path)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "LLMSPreview/" + VERSION

        def respond(self, head=False):
            if self.path.split("?", 1)[0] != path:
                self.plain(404, b"Not found.\n", head)
                return
            payload, etag, stale = state.refresh()
            unchanged = etag_matches(self.headers.get("If-None-Match"), etag)
            self.send_response(304 if unchanged else 200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-LLMS-Stale", "true" if stale else "false")
            if not unchanged:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not head and not unchanged:
                self.body(payload)

        def plain(self, status, payload, head=False):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if status == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if not head:
                self.body(payload)

        def body(self, payload):
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.respond()

        def do_HEAD(self):
            self.respond(head=True)

        def do_POST(self):
            self.plain(405, b"Use GET or HEAD.\n")

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

        def log_message(self, fmt, *args):
            # Do not echo arbitrary request data in terminal logs.
            pass

    return Handler


def make_server(manifest, host="127.0.0.1", port=8765, path="/llms.txt", allow_example=False):
    validate_endpoint(path)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Choose an explicit host; the default is local-only 127.0.0.1.")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Use a port from 0 to 65535; port 0 selects an ephemeral local port.")
    state = ManifestState(manifest, allow_example)

    class PreviewServer(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            print("A local preview request could not be completed.", file=sys.stderr)

    server = PreviewServer((host, port), make_handler(state, path))
    server.daemon_threads = True
    server.manifest_state = state
    return server


def output(result, as_json=False):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(("PASS" if result["valid"] else "FAIL") + ": " + result["kind"])
    for severity in ("errors", "warnings", "info"):
        for item in result[severity]:
            loc = " (" + item["location"] + ")" if item.get("location") else ""
            print(f"{severity[:-1] if severity != 'info' else 'info'} [{item['code']}]{loc}: {item['message']}")
    if "output" in result:
        print("Output: " + result["output"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-manifest", "build", "serve"):
        sub = commands.add_parser(command)
        sub.add_argument("--manifest", required=True, type=Path)
        sub.add_argument("--allow-example", action="store_true", help="Allow reserved example/test destinations for demonstrations only.")
        if command != "serve":
            sub.add_argument("--json", action="store_true", help="Emit a machine-readable validation report.")
        if command == "build":
            sub.add_argument("--output", required=True, type=Path)
            sub.add_argument("--force", action="store_true", help="Atomically replace an existing output file.")
        elif command == "serve":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", default=8765, type=int)
            sub.add_argument("--path", default="/llms.txt")
    lint = commands.add_parser("lint")
    lint.add_argument("--file", required=True, type=Path)
    lint.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "lint":
            result = lint_text(args.file.read_text(encoding="utf-8-sig"))
        elif args.command == "serve":
            server = make_server(args.manifest, args.host, args.port, args.path, args.allow_example)
            print(f"Local preview at http://{args.host}:{server.server_port}{args.path}; Ctrl-C to stop.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        else:
            data, result = load_manifest(args.manifest, args.allow_example)
            if args.command == "build":
                ensure_distinct_output(args.manifest, args.output)
                payload = render_manifest(data, args.allow_example)
                atomic_write(args.output, payload, force=args.force)
                result["output"] = str(args.output)
                result["bytes"] = len(payload)
                result["sha256"] = hashlib.sha256(payload).hexdigest()
        output(result, getattr(args, "json", False))
        return 0 if result["valid"] else 1
    except ContentError as exc:
        output(exc.report, getattr(args, "json", False))
        return 1
    except (OSError, ValueError, UnicodeError) as exc:
        result = report(args.command)
        message = "Output already exists; choose another destination or use --force." if isinstance(exc, FileExistsError) else str(exc)
        add(result, "errors", "operation", message)
        output(result, getattr(args, "json", False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
