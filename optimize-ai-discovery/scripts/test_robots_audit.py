#!/usr/bin/env python3
"""Regression tests for meaningful robots policy and encoding edge cases."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from robots_audit import audit, evaluate, main, normalize_octets, parse_robots, request_path


class RobotsAuditTests(unittest.TestCase):
    def decide(self, text, url, agent="ExampleBot", mode="longest-substring"):
        document = parse_robots(text)
        self.assertTrue(document.valid, document.diagnostics)
        return evaluate(document, agent, url, mode)

    def test_specific_group_does_not_inherit_wildcard(self):
        baseline = parse_robots("User-agent: *\nDisallow: /private/\n")
        candidate = parse_robots("User-agent: *\nDisallow: /private/\n\nUser-agent: OAI-SearchBot\nAllow: /\n")
        report = audit(baseline, ["OAI-SearchBot"], ["https://example.com/private/a"], candidate)
        self.assertEqual(report["summary"]["broadened"], 1)
        self.assertEqual(report["evaluations"][0]["change"], "broadened")

    def test_duplicate_groups_are_merged(self):
        text = "User-agent: ExampleBot\nDisallow: /private\nUser-agent: ExampleBot\nAllow: /private/public\n"
        answer = self.decide(text, "/private/public/a")
        self.assertTrue(answer["allowed"])
        self.assertEqual(len(answer["selected_groups"]), 2)
        self.assertFalse(self.decide(text, "/private/a")["allowed"])

    def test_equal_allow_disallow_tie(self):
        text = "User-agent: *\nDisallow: /same\nAllow: /same\n"
        result = self.decide(text, "/same/path")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "allow_wins_tie")

    def test_longest_matching_rule(self):
        text = "User-agent: *\nDisallow: /\nAllow: /products/\nDisallow: /products/internal/\n"
        self.assertTrue(self.decide(text, "/products/a")["allowed"])
        self.assertFalse(self.decide(text, "/products/internal/a")["allowed"])
        self.assertFalse(self.decide(text, "/about")["allowed"])

    def test_wildcard_end_anchor_and_empty_star(self):
        text = "User-agent: *\nDisallow: /*.pdf$\n"
        self.assertFalse(self.decide(text, "/a.pdf")["allowed"])
        self.assertFalse(self.decide(text, "/.pdf")["allowed"])
        self.assertTrue(self.decide(text, "/a.pdf?download=1")["allowed"])
        self.assertTrue(self.decide(text, "/a.pdf/extra")["allowed"])

    def test_query_is_part_of_match_fragment_is_not(self):
        text = "User-agent: *\nDisallow: /*?sort=\nAllow: /public\n"
        self.assertFalse(self.decide(text, "https://example.com/items?sort=price#part")["allowed"])
        self.assertTrue(self.decide(text, "https://example.com/items#?sort=price")["allowed"])
        self.assertEqual(request_path("https://example.com/a?"), "/a?")

    def test_percent_unreserved_decoded_once(self):
        text = "User-agent: *\nDisallow: /%66%6f%6f/bar\n"
        self.assertFalse(self.decide(text, "/foo/bar")["allowed"])
        self.assertFalse(self.decide(text, "/%66oo/bar")["allowed"])
        self.assertTrue(self.decide(text, "/%2566oo/bar")["allowed"])

    def test_percent_reserved_preserves_distinction(self):
        text = "User-agent: *\nDisallow: /foo%2Fbar\n"
        self.assertFalse(self.decide(text, "/foo%2fbar")["allowed"])
        self.assertTrue(self.decide(text, "/foo/bar")["allowed"])

    def test_percent_encoded_star_is_literal(self):
        text = "User-agent: *\nDisallow: /literal/%2A$\n"
        self.assertFalse(self.decide(text, "/literal/%2a")["allowed"])
        self.assertTrue(self.decide(text, "/literal/anything")["allowed"])

    def test_unicode_matches_utf8_encoding(self):
        text = "User-agent: *\nDisallow: /café/\n"
        self.assertFalse(self.decide(text, "/caf%C3%A9/item")["allowed"])
        self.assertFalse(self.decide(text, "/café/item")["allowed"])
        self.assertTrue(self.decide(text, "/cafe/item")["allowed"])

    def test_specificity_counts_octets_not_escape_text_length(self):
        text = "User-agent: *\nDisallow: /caf%C3%A9\nAllow: /café\n"
        answer = self.decide(text, "/café")
        self.assertTrue(answer["allowed"])
        self.assertEqual({r["specificity_octets"] for r in answer["winning_rules"]}, {6})

    def test_bom_comments_case_and_case_sensitive_paths(self):
        text = "\ufeff# introduction\nUSER-AGENT: examplebot # inline\nDisallow: /Private # note\n"
        self.assertFalse(self.decide(text, "/Private", "ExampleBot")["allowed"])
        self.assertTrue(self.decide(text, "/private", "ExampleBot")["allowed"])

    def test_empty_disallow_has_no_path_effect_and_ends_group(self):
        text = "User-agent: ExampleBot\nDisallow:\nUser-agent: OtherBot\nDisallow: /\n"
        self.assertTrue(self.decide(text, "/", "ExampleBot")["allowed"])
        self.assertFalse(self.decide(text, "/", "OtherBot")["allowed"])

    def test_multiple_agents_before_rules_share_group(self):
        text = "User-agent: ExampleBot\n\n# still same group\nUser-agent: OtherBot\nDisallow: /x\n"
        self.assertFalse(self.decide(text, "/x", "ExampleBot")["allowed"])
        self.assertFalse(self.decide(text, "/x", "OtherBot")["allowed"])

    def test_longest_substring_policy_and_overlap_warning(self):
        text = "User-agent: Bot\nDisallow: /\nUser-agent: ExampleBot\nAllow: /\n"
        answer = self.decide(text, "/", "ExampleBot")
        self.assertTrue(answer["allowed"])
        self.assertEqual(answer["diagnostics"][0]["code"], "overlapping_agent_tokens")
        self.assertFalse(self.decide("User-agent: Example\nDisallow: /\n", "/", "ExampleBot")["allowed"])

    def test_exact_policy_has_different_explicit_behavior(self):
        text = "User-agent: Example\nDisallow: /\nUser-agent: *\nAllow: /\n"
        self.assertTrue(self.decide(text, "/", "ExampleBot", "exact")["allowed"])

    def test_unknown_directives_preserved_without_ending_group(self):
        text = "User-agent: ExampleBot\nSitemap: https://example.com/sitemap.xml\nCrawl-delay: 2\nNoindex: /x\nDisallow: /private\n"
        document = parse_robots(text)
        self.assertTrue(document.valid)
        self.assertEqual([r["field"] for r in document.extra_records], ["sitemap", "crawl-delay", "noindex"])
        self.assertFalse(evaluate(document, "ExampleBot", "/private")["allowed"])
        self.assertTrue(evaluate(document, "ExampleBot", "/x")["allowed"])

    def test_malformed_input_gives_unknown_instead_of_false_assurance(self):
        for content in ("Allow: /\n", "User-agent: ExampleBot\nDisallow /private\n", "User-agent: ExampleBot/1.0\nDisallow: /\n"):
            document = parse_robots(content)
            self.assertFalse(document.valid)
            answer = evaluate(document, "ExampleBot", "/private")
            self.assertIsNone(answer["allowed"])
            self.assertEqual(answer["decision"], "unknown")

    def test_empty_file_and_no_group_match(self):
        self.assertTrue(self.decide("", "/")["allowed"])
        self.assertTrue(self.decide("User-agent: OtherBot\nDisallow: /\n", "/")["allowed"])

    def test_trailing_wildcard_equivalence_allows_tie(self):
        text = "User-agent: *\nDisallow: /same***\nAllow: /same\n"
        self.assertTrue(self.decide(text, "/same/extra")["allowed"])

    def test_stars_do_not_cause_exponential_backtracking(self):
        text = "User-agent: *\nDisallow: /" + "*a" * 30 + "b$\n"
        self.assertTrue(self.decide(text, "/" + "a" * 1500 + "c")["allowed"])

    def test_invalid_url_and_full_user_agent_are_rejected(self):
        document = parse_robots("User-agent: *\nDisallow: /\n")
        for url in ("ftp://example.com/a", "https://user:secret@example.com/a", "/bad path", "/bad%xx"):
            with self.assertRaises(ValueError):
                evaluate(document, "ExampleBot", url)
        with self.assertRaises(ValueError):
            evaluate(document, "ExampleBot/1.0", "/")

    def test_comparison_reports_unknown_if_candidate_invalid(self):
        before = parse_robots("User-agent: *\nDisallow: /\n")
        after = parse_robots("User-agent: *\nAllow /\n")
        result = audit(before, ["ExampleBot"], ["/"], after)
        self.assertEqual(result["summary"]["unknown"], 1)

    def test_cli_json_exit_codes_and_no_mutation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            before = Path(directory) / "before.txt"
            after = Path(directory) / "after.txt"
            before_text = "User-agent: *\nDisallow: /private\n"
            after_text = "User-agent: *\nAllow: /\n"
            before.write_text(before_text, encoding="utf-8")
            after.write_text(after_text, encoding="utf-8")
            arguments = ["--file", str(before), "--candidate", str(after), "--agent", "ExampleBot", "--url", "/private", "--format", "json", "--fail-on-broaden"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(arguments)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["summary"]["broadened"], 1)
            self.assertEqual(before.read_text(), before_text)
            self.assertEqual(after.read_text(), after_text)
            after.write_text("Disallow: /\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(arguments)
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
