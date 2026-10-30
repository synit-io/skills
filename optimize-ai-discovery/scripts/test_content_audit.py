#!/usr/bin/env python3
"""Focused regression tests for the saved-HTML content inspector."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from content_audit import analyze_html, main, public_http_url, render_text


class ContentAuditTests(unittest.TestCase):
    def audit(self, html, url="https://example.com/products/flow/"):
        return analyze_html(html, url)

    def codes(self, report):
        return {item["code"] for item in report["observations"]}

    def test_main_extraction_ignores_scripts_hidden_chrome_and_head(self):
        html = """<!doctype html><html lang="en"><head>
        <title>Flow product</title><style>.price { color: red }</style>
        <script>const secretSentinel = 'DO_NOT_EXTRACT_SCRIPT';</script>
        </head><body><nav>OUTSIDE_NAV</nav><main>
        <h1>Flow <em>Enterprise</em></h1><p>€24 per seat billed annually.</p>
        <p hidden>HIDDEN_ATTRIBUTE</p><div aria-hidden="true">ARIA_HIDDEN</div>
        <span style="display:none">DISPLAY_HIDDEN</span>
        <span style="visibility: hidden !important">VISIBILITY_HIDDEN</span>
        <template>INERT_TEMPLATE</template><noscript>NOSCRIPT_BLOCK</noscript>
        <nav>INSIDE_NAV</nav><footer>INSIDE_FOOTER</footer>
        <p>Version 3.2 supports SAML. SCIM is not supported.</p>
        </main><footer>OUTSIDE_FOOTER</footer></body></html>"""
        report = self.audit(html)
        preview = report["content"]["preview"]
        self.assertIn("Flow Enterprise", preview)
        self.assertIn("SCIM is not supported", preview)
        for sentinel in ("DO_NOT_EXTRACT_SCRIPT", "HIDDEN_ATTRIBUTE", "ARIA_HIDDEN", "DISPLAY_HIDDEN", "VISIBILITY_HIDDEN", "INERT_TEMPLATE", "NOSCRIPT_BLOCK", "INSIDE_NAV", "INSIDE_FOOTER", "OUTSIDE_NAV", "OUTSIDE_FOOTER"):
            self.assertNotIn(sentinel, preview)
        self.assertNotIn("DO_NOT_EXTRACT_SCRIPT", json.dumps(report))
        self.assertEqual(report["metadata"]["title"], "Flow product")
        self.assertEqual(report["headings"]["items"][0]["text"], "Flow Enterprise")
        self.assertEqual(report["content"]["selection"], "visible_main_regions_excluding_nav_footer")

    def test_relative_metadata_and_base_resolution(self):
        report = self.audit("""<head><base href="https://example.com/docs/">
        <link rel="canonical" href="product/">
        <link rel="alternate" type="text/markdown; charset=utf-8" href="product/index.md">
        <link rel="describedby" href="llms.txt">
        <link rel="alternate" hreflang="de" href="/de/produkt/">
        </head><body><a href="../pricing/">Prices</a></body>""")
        self.assertEqual(report["metadata"]["canonical_urls"], ["https://example.com/docs/product/"])
        self.assertEqual(report["metadata"]["markdown_alternates"], ["https://example.com/docs/product/index.md"])
        self.assertEqual(report["metadata"]["describedby"], ["https://example.com/docs/llms.txt"])
        self.assertEqual(report["metadata"]["hreflang"][0], {"language": "de", "url": "https://example.com/de/produkt/"})
        self.assertEqual(report["links"]["items"][0]["url"], "https://example.com/pricing/")

    def test_unsafe_links_are_not_returned_and_duplicates_merge(self):
        report = self.audit("""<a href="/pricing/">Pricing</a><a href="/pricing/">Again</a>
        <a href="javascript:secret()">Script</a><a href="data:text/plain,test">Data</a>
        <a href="mailto:support@example.com">Email</a>
        <a href="http://127.0.0.1/admin">Private</a><a href="https://user:pass@example.com/">Credentials</a>
        <a href="https://other.example.com/docs/#setup">External</a>""")
        urls = [item["url"] for item in report["links"]["items"]]
        self.assertEqual(urls, ["https://example.com/pricing/", "https://other.example.com/docs/#setup"])
        self.assertEqual(report["links"]["excluded_link_values"], 5)
        self.assertEqual(report["links"]["unique_http_urls"], 2)
        self.assertTrue(report["links"]["items"][1]["external_host"])

    def test_public_url_filter_is_syntactic_and_does_not_resolve_dns(self):
        for value in ("https://localhost/", "http://192.168.1.1/", "http://[::1]/", "http://127.1/", "http://internal/", "https://a.local/", "https://example.com/a b", "https://example.com\\@localhost/", "https://example.com:bad/", "file:///tmp/a"):
            self.assertIsNone(public_http_url(value), value)
        self.assertEqual(public_http_url("/p?q=1#info", "https://example.com/"), "https://example.com/p?q=1#info")
        self.assertIsNotNone(public_http_url("https://8.8.8.8/"))

    def test_jsonld_graph_embedded_offer_and_full_type_uri(self):
        data = {"@context": "https://schema.org", "@graph": [
            {"@type": "Product", "@id": "#flow", "name": "Flow", "sku": "FLOW-3", "offers": {"@type": "https://schema.org/Offer", "price": "24.00", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"}},
            {"@type": ["ProductGroup", "Thing"], "name": "Flow family", "hasVariant": [{"@id": "#flow"}]},
        ]}
        report = self.audit(f'<script type="application/ld+json">{json.dumps(data)}</script><main><h1>Flow</h1><p>€24 per seat. Version 3.2.</p></main>')
        structured = report["structured_data"]
        self.assertEqual(structured["product_offer_entity_count"], 3)
        self.assertEqual(structured["type_counts"], {"Offer": 1, "Product": 1, "ProductGroup": 1, "Thing": 1})
        product, offer, group = structured["product_offer_entities"]
        self.assertEqual(product["json_path"], "$.@graph[0]")
        self.assertEqual(product["fields"]["sku"]["value"], "FLOW-3")
        self.assertFalse(product["fields"]["gtin"]["present"])
        self.assertEqual(offer["fields"]["priceCurrency"]["value"], "EUR")
        self.assertEqual(group["fields"]["hasVariant"]["items"], 1)
        self.assertIn("@id reference resolution", structured["validation_scope"])
        categories = {item["category"] for item in report["content"]["excerpts"]}
        self.assertEqual(categories, {"product_name", "price_or_currency", "version_or_release"})

    def test_malformed_jsonld_is_reported_without_discarding_valid_documents(self):
        report = self.audit('<script type="application/ld+json">{"@type":</script><script type="application/ld+json">{"@type":"Organization"}</script>')
        self.assertFalse(report["structured_data"]["scripts"][0]["valid_json"])
        self.assertTrue(report["structured_data"]["scripts"][1]["valid_json"])
        self.assertIn("INVALID_JSONLD_JSON", self.codes(report))
        self.assertEqual(report["structured_data"]["type_counts"], {"Organization": 1})

    def test_non_json_constants_and_scalar_scripts_are_not_accepted(self):
        report = self.audit('<script type="application/ld+json">{"price":NaN}</script><script type="application/ld+json">42</script>')
        self.assertEqual([item["valid_json"] for item in report["structured_data"]["scripts"]], [False, False])

    def test_service_and_software_identity_preserves_qualifiers_and_offer(self):
        data = {"@graph": [
            {"@type": "ProfessionalService", "name": "Data consultancy", "areaServed": ["DE", "AT", "CH"]},
            {"@type": ["https://schema.org/SoftwareApplication", "Product"], "name": "Directory reader",
             "softwareVersion": "v0.2.1", "operatingSystem": "Linux, Docker",
             "license": "https://example.com/license", "description": "Read-only; no writes.",
             "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR", "description": "Internal use only"}},
        ]}
        structured = self.audit(f'<script type="application/ld+json">{json.dumps(data)}</script>')["structured_data"]
        service, software = structured["service_software_entities"]
        self.assertEqual(structured["service_software_entity_count"], 2)
        self.assertEqual(service["fields"]["areaServed"]["items"], 3)
        self.assertFalse(service["fields"]["softwareVersion"]["present"])
        self.assertEqual(software["json_path"], "$.@graph[1]")
        self.assertEqual(software["fields"]["softwareVersion"]["value"], "v0.2.1")
        self.assertEqual(software["fields"]["license"]["value"], "https://example.com/license")
        self.assertEqual(software["fields"]["description"]["value"], "Read-only; no writes.")
        self.assertEqual(structured["product_offer_entity_count"], 2)
        self.assertEqual(structured["product_offer_entities"][1]["fields"]["price"]["value"], "0")
        self.assertEqual(structured["product_offer_entities"][1]["fields"]["description"]["value"], "Internal use only")
        self.assertFalse(structured["service_software_entities_truncated"])

    def test_service_software_evidence_is_bounded_without_losing_total_count(self):
        data = [{"@type": "Service", "name": str(i)} for i in range(101)]
        structured = self.audit(f'<script type="application/ld+json">{json.dumps(data)}</script>')["structured_data"]
        self.assertEqual(structured["service_software_entity_count"], 101)
        self.assertEqual(len(structured["service_software_entities"]), 100)
        self.assertTrue(structured["service_software_entities_truncated"])
        self.assertEqual(structured["product_offer_entity_count"], 0)

    def test_decimal_price_is_not_inferred_to_be_a_version(self):
        report = self.audit('<main><h1>Product</h1><p>USD 24.00 per seat.</p></main>')
        categories = {item["category"] for item in report["content"]["excerpts"]}
        self.assertEqual(categories, {"price_or_currency"})

    def test_inert_template_does_not_create_active_jsonld_evidence(self):
        report = self.audit('<template><script type="application/ld+json">{"@type":"Product","name":"Inert"}</script></template><p>Actual content</p>')
        self.assertEqual(report["structured_data"]["script_count"], 0)
        self.assertNotIn("Inert", report["content"]["preview"])

    def test_meta_directive_scope_is_preserved(self):
        report = self.audit('<meta name="googlebot" content="noindex, max-snippet:0"><meta name="robots" content="nofollow"><main><h1>Product</h1><p data-nosnippet>Visible detail</p></main>')
        notes = {item["code"]: item for item in report["observations"]}
        self.assertEqual(notes["INDEXING_RESTRICTION_META"]["evidence"]["name"], "googlebot")
        self.assertEqual(notes["SNIPPET_RESTRICTION_META"]["evidence"]["name"], "googlebot")
        self.assertEqual(report["metadata"]["data_nosnippet_element_count"], 1)
        self.assertIn("Visible detail", report["content"]["preview"])
        self.assertIn("HTTP status", " ".join(report["limitations"]))
        self.assertEqual(report["scope"]["network_requests"], 0)

    def test_role_main_selects_content_and_fallback_is_labelled(self):
        report = self.audit('<nav>Navigation text</nav><section role="main"><h1>Actual product</h1><table><tr><td>Plan</td></tr></table></section>')
        self.assertNotIn("Navigation text", report["content"]["preview"])
        self.assertEqual(report["content"]["tables_in_main"], 1)
        fallback = self.audit('<nav>Navigation text</nav><article>Other content</article>')
        self.assertIn("Navigation text", fallback["content"]["preview"])
        self.assertIn("including_possible_navigation", fallback["content"]["selection"])

    def test_unicode_entities_and_meaning_are_preserved(self):
        report = self.audit('<html lang="de"><title>Geräte &amp; Zubehör</title><main><h1>München – Größe XL</h1><p>€29,90 pro Monat. Keine SCIM-Unterstützung.</p></main></html>')
        self.assertEqual(report["metadata"]["title"], "Geräte & Zubehör")
        self.assertIn("Keine SCIM-Unterstützung", report["content"]["preview"])
        self.assertEqual(report["metadata"]["language"], "de")
        self.assertIn("€29,90", report["content"]["excerpts"][0]["matched_text"])

    def test_shell_heuristic_has_explicit_threshold_and_no_score(self):
        report = self.audit('<title>App</title><div id="app"></div><script src="bundle.js"></script>')
        note = next(item for item in report["observations"] if item["code"] == "LOW_TEXT_WITH_SCRIPTS")
        self.assertEqual(note["evidence"]["words"], 0)
        self.assertEqual(note["evidence"]["review_threshold"], 40)
        self.assertIn("not an SEO", note["message"])
        self.assertNotIn("score", report)

    def test_alt_counts_distinguish_missing_empty_and_hidden(self):
        report = self.audit('<main><img src="a.png"><img src="b.png" alt=""><img src="c.png" alt="Product"><img hidden src="d.png"></main>')
        self.assertEqual(report["content"]["images"], {"total": 4, "visible_missing_alt": 1, "visible_empty_alt": 1, "hidden": 1})

    def test_multiple_canonicals_and_invalid_base_are_observations(self):
        report = self.audit('<base href="http://localhost/"><link rel="canonical" href="/a"><link rel="canonical" href="/b">')
        self.assertIn("UNUSABLE_BASE_URL", self.codes(report))
        self.assertIn("MULTIPLE_CANONICALS", self.codes(report))
        self.assertEqual(report["metadata"]["canonical_urls"], ["https://example.com/a", "https://example.com/b"])

    def test_cli_output_utf8_no_clobber_and_input_protection(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source, output = root / "page.html", root / "report.json"
            source.write_text('\ufeff<title>München</title><main><h1>Produkt</h1><p>€10 monatlich</p></main>', encoding="utf-8")
            args = ["--file", str(source), "--url", "https://example.com/", "--output", str(output)]
            self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(output.read_text())["metadata"]["title"], "München")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
                self.assertEqual(main(["--file", str(source), "--url", "https://example.com/", "--output", str(source), "--force"]), 2)
            self.assertEqual(main(args + ["--force"]), 0)
            self.assertIn("<h1>", source.read_text())

    def test_cli_bad_source_url_is_actionable_error(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / "page.html"
            source.write_text("<h1>Test</h1>")
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                result = main(["--file", str(source), "--url", "http://localhost/"])
            self.assertEqual(result, 2)
            self.assertIn("absolute HTTP(S)", error.getvalue())

    def test_text_output_retains_limitations_and_evidence(self):
        report = self.audit('<title>Product</title><main><h1>Product</h1><p>EUR 24 per seat.</p></main>')
        output = render_text(report)
        self.assertIn("one saved HTML", output)
        self.assertIn("EUR 24", output)
        self.assertIn("X-Robots-Tag", output)


if __name__ == "__main__":
    unittest.main()
