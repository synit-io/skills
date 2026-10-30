#!/usr/bin/env python3
"""Contract tests; all temporary files stay beside this test and are removed."""
import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

import llms_toolkit as toolkit


def manifest():
    return {
        "schema_version": 1,
        "name": "Flow [Team] — München",
        "summary": "Reviewed product information.",
        "guidance": ["Read current prices together with the billing period."],
        "sections": [{"title": "Products", "links": [{
            "title": "Flow [Team]", "url": "https://vendor.test/products/flow?plan=(team)",
            "description": "Current capabilities and limitations."}]}],
    }


class ManifestTests(unittest.TestCase):
    def test_minimal_and_deterministic(self):
        data = {"schema_version": 1, "name": "Project"}
        self.assertEqual(toolkit.render_manifest(data), b"# Project\n")
        self.assertEqual(toolkit.render_manifest(manifest(), True), toolkit.render_manifest(manifest(), True))

    def test_utf8_and_markdown_escape(self):
        rendered = toolkit.render_manifest(manifest(), True).decode()
        self.assertIn("München", rendered)
        self.assertIn(r"Flow \[Team\]", rendered)
        self.assertIn("plan=%28team%29", rendered)
        self.assertTrue(toolkit.lint_text(rendered)["valid"])

    def test_examples_are_explicit(self):
        self.assertFalse(toolkit.validate_manifest(manifest())["valid"])
        result = toolkit.validate_manifest(manifest(), True)
        self.assertTrue(result["valid"])
        self.assertIn("example_url", [x["code"] for x in result["warnings"]])

    def test_newline_cannot_create_sections_or_link_annotations(self):
        for target, key in (("root", "name"), ("link", "title"), ("link", "description")):
            with self.subTest(target=target, key=key):
                data = manifest()
                obj = data if target == "root" else data["sections"][0]["links"][0]
                obj[key] = "Safe\n## Injected"
                self.assertFalse(toolkit.validate_manifest(data, True)["valid"])
        data = manifest()
        data["name"] = "Name\u2028## Hidden line"
        self.assertFalse(toolkit.validate_manifest(data, True)["valid"])

    def test_duplicate_and_unknown_fields(self):
        data = manifest()
        data["sections"].append(copy.deepcopy(data["sections"][0]))
        result = toolkit.validate_manifest(data, True)
        self.assertEqual({x["code"] for x in result["errors"]}, {"duplicate_heading", "duplicate_url"})
        self.assertFalse(toolkit.validate_manifest({"schema_version": True, "name": "Valid"})["valid"])
        self.assertFalse(toolkit.validate_manifest({"schema_version": 1, "name": "Valid", "typo": []})["valid"])

    def test_url_schemes_credentials_and_encoding(self):
        for url in ("javascript:alert(1)", "https://user:password@vendor.test", "file:///tmp/a", "https://host/a b", "https://host/%bad%", "https://[broken/path"):
            with self.subTest(url=url):
                data = manifest()
                data["sections"][0]["links"][0]["url"] = url
                self.assertFalse(toolkit.validate_manifest(data, True)["valid"])
        self.assertIsNone(toolkit.url_problem("https://[2001:db8::1]:443/a?x=2#details"))

    def test_review_date_and_placeholders(self):
        data = {"schema_version": 1, "name": "TODO is a legitimate product name", "reviewed_on": "2024-02-29"}
        self.assertTrue(toolkit.validate_manifest(data)["valid"])
        data["reviewed_on"] = "2025-02-29"
        self.assertFalse(toolkit.validate_manifest(data)["valid"])
        data = {"schema_version": 1, "name": "{{company_name}}"}
        self.assertFalse(toolkit.validate_manifest(data)["valid"])

    def test_atomic_no_clobber_then_replace(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            output = Path(folder) / "llms.txt"
            toolkit.atomic_write(output, b"old")
            with self.assertRaises(FileExistsError):
                toolkit.atomic_write(output, b"new")
            self.assertEqual(output.read_bytes(), b"old")
            toolkit.atomic_write(output, b"new", force=True)
            self.assertEqual(output.read_bytes(), b"new")
            self.assertEqual([x.name for x in Path(folder).iterdir()], ["llms.txt"])

    def test_invalid_json_and_cli_report(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "manifest.json"
            source.write_text('{"name":"A","name":"B","schema_version":1}')
            with self.assertRaises(toolkit.ContentError):
                toolkit.load_manifest(source)
            source.write_text(json.dumps(manifest()))
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                code = toolkit.main(["validate-manifest", "--manifest", str(source), "--allow-example", "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(capture.getvalue())["valid"])

    def test_build_cannot_overwrite_its_manifest_through_aliases(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "manifest.json"
            original = json.dumps(manifest()).encode("utf-8")
            source.write_bytes(original)
            symlink = Path(folder) / "symlink.txt"
            symlink.symlink_to(source)
            hardlink = Path(folder) / "hardlink.txt"
            os.link(source, hardlink)
            for destination in (source, symlink, hardlink):
                with self.subTest(destination=destination.name):
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = toolkit.main(["build", "--manifest", str(source), "--allow-example",
                                             "--output", str(destination), "--force", "--json"])
                    self.assertEqual(code, 2)
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(destination.read_bytes(), original)


class LintTests(unittest.TestCase):
    def test_h1_only_is_valid(self):
        self.assertTrue(toolkit.lint_text("\ufeff# Project\n")["valid"])
        self.assertTrue(toolkit.lint_text("Project\n=======\n")["valid"])

    def test_prose_bullets_and_reference_links(self):
        text = "# Project\n\n> Summary\n\nPlain guidance.\n\n- A useful reminder.\n\n## Docs\n\n- [Current][spec]: Current documentation.\n- [Pricing][]: Prices.\n\n[spec]: <https://vendor.test/docs>\n[Pricing]: https://vendor.test/prices \"Pricing title\"\n"
        result = toolkit.lint_text(text)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["link_count"], 2)

    def test_inline_complex_destination(self):
        for text in ("- [A](https://host/a(b)c): Details", '- [A](<https://host/a(b)c> "Page title"): Details', '- [A](https://host/a "Page title"): Details'):
            result = toolkit.lint_text("# Site\n\n## Docs\n" + text + "\n")
            self.assertTrue(result["valid"], result)

    def test_invalid_structures(self):
        for text in ("<html><body>Denied</body></html>", "No heading", "# A\n# B", "# A\n## Docs\nInstructions after sections.", "# A\n### Nested heading", "# A\n## Docs\n- Plain text", "# A\n## Docs\n- [Broken](https://host"):
            with self.subTest(text=text):
                self.assertFalse(toolkit.lint_text(text)["valid"])

    def test_duplicate_and_relative_links_warn(self):
        text = "# Site\n## Docs\n- [A](/docs): One\n- [B](/docs): Two\n"
        result = toolkit.lint_text(text)
        self.assertTrue(result["valid"])
        codes = {x["code"] for x in result["warnings"]}
        self.assertTrue({"duplicate_url", "relative_url"}.issubset(codes))

    def test_code_does_not_become_a_heading(self):
        text = "# Site\n\n```text\n# Example, not a heading\n```\n\n## Docs\n- [A](https://host/docs)\n"
        self.assertTrue(toolkit.lint_text(text)["valid"])

    def test_unsafe_url_fails(self):
        self.assertFalse(toolkit.lint_text("# Site\n## Docs\n- [A](javascript:alert(1))\n")["valid"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.source = Path(self.temp.name) / "manifest.json"
        self.source.write_text(json.dumps(manifest()), encoding="utf-8")
        self.state = toolkit.ManifestState(self.source, allow_example=True)
        self.handler = toolkit.make_handler(self.state)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method="GET", path="/llms.txt", headers=None):
        request = f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n"
        request += "".join(f"{key}: {value}\r\n" for key, value in (headers or {}).items())
        request += "\r\n"
        sock = MemorySocket(request.encode("ascii"))
        self.handler(sock, ("127.0.0.1", 0), object())
        response = http.client.HTTPResponse(MemorySocket(sock.written.getvalue()), method=method)
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def test_get_head_and_etag(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn("charset=utf-8", headers["Content-Type"])
        self.assertEqual(int(headers["Content-Length"]), len(body))
        status, head_headers, head_body = self.request("HEAD")
        self.assertEqual((status, head_body), (200, b""))
        self.assertEqual(head_headers["Content-Length"], headers["Content-Length"])
        status, _, body304 = self.request(headers={"If-None-Match": '"other", W/' + headers["ETag"]})
        self.assertEqual((status, body304), (304, b""))

    def test_live_update_invalid_fallback_and_recovery(self):
        _, before_headers, before = self.request()
        data = manifest()
        data["name"] = "Updated project"
        self.source.write_text(json.dumps(data), encoding="utf-8")
        status, after_headers, after = self.request(headers={"If-None-Match": before_headers["ETag"]})
        self.assertEqual(status, 200)
        self.assertNotEqual(before_headers["ETag"], after_headers["ETag"])
        self.assertNotEqual(before, after)
        self.source.write_text("{unfinished", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            status, stale_headers, stale = self.request()
        self.assertEqual((status, stale), (200, after))
        self.assertEqual(stale_headers["X-LLMS-Stale"], "true")
        self.source.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.request()[1]["X-LLMS-Stale"], "false")

    def test_unsupported_paths_methods_and_endpoint_validation(self):
        self.assertEqual(self.request(path="/private")[0], 404)
        self.assertEqual(self.request(path="//[malformed")[0], 404)
        self.assertEqual(self.request("POST")[0], 405)
        with self.assertRaises(ValueError):
            toolkit.make_server(self.source, path="/../llms.txt", allow_example=True)
        with self.assertRaises(ValueError):
            toolkit.make_server(self.source, port=99999, allow_example=True)

    def test_scoped_path(self):
        self.handler = toolkit.make_handler(self.state, path="/docs/llms.txt")
        self.assertEqual(self.request(path="/docs/llms.txt")[0], 200)
        self.assertEqual(self.request()[0], 404)


class MemorySocket:
    """Socket-shaped byte streams exercise BaseHTTPRequestHandler without networking."""
    def __init__(self, incoming):
        self.incoming = incoming
        self.written = io.BytesIO()

    def makefile(self, mode, buffering=None):
        return io.BytesIO(self.incoming)

    def sendall(self, data):
        self.written.write(data)


@unittest.skipUnless(os.environ.get("LLMS_TOOLKIT_LIVE_HTTP") == "1",
                     "Optional live-loopback check; set LLMS_TOOLKIT_LIVE_HTTP=1 where local sockets are permitted.")
class LiveHTTPTests(unittest.TestCase):
    def test_local_socket_transport(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        source = Path(temp.name) / "manifest.json"
        source.write_text(json.dumps(manifest()), encoding="utf-8")
        server = toolkit.make_server(source, port=0, allow_example=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            client.request("GET", "/llms.txt")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"Flow", response.read())
            etag = response.getheader("ETag")
            client.close()
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            client.request("GET", "/llms.txt", headers={"If-None-Match": etag})
            response = client.getresponse()
            self.assertEqual((response.status, response.read()), (304, b""))
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
