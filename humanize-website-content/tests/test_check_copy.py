"""Offline regression tests for the website copy checker."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import check_copy as script


class CheckTextTests(unittest.TestCase):
    def test_clear_german_copy_and_hyphens_are_allowed(self):
        text = "Wir verbinden Ihre On-Premises-Systeme.\nKI-gestützt prüfen wir die Daten."
        self.assertEqual(script.check_text(text, "page.md"), [])

    def test_every_configured_phrase_is_detected_in_uppercase(self):
        for phrase in script.PHRASES:
            with self.subTest(phrase=phrase):
                findings = script.check_text(phrase.upper(), "page.md")
                self.assertTrue(any("KI-Floskel" in item for item in findings))

    def test_phrases_match_german_casefolding_and_variable_whitespace(self):
        for text in (
            "Neue Maßstäbe setzen.",
            "MASSGESCHNEIDERTE LÖSUNG",
            "Entdecken\tSie uns.",
            "In\u00a0der heutigen digitalen Welt",
        ):
            with self.subTest(text=text):
                findings = script.check_text(text, "page.md")
                self.assertTrue(any("KI-Floskel" in item for item in findings))

    def test_phrase_substrings_inside_words_are_not_flagged(self):
        for text in ("Unwillkommen bei Gästen.", "Entdecken Siegel im Archiv.", "Mehr als Nullen."):
            with self.subTest(text=text):
                self.assertEqual(script.check_text(text, "page.md"), [])

    def test_dashes_and_findings_keep_source_line_numbers(self):
        findings = script.check_text("Klarer Text.\nSatz — Zusatz.\nSatz – Zusatz.", "page.md")
        self.assertEqual(len(findings), 2)
        self.assertTrue(findings[0].startswith("page.md:2:"))
        self.assertTrue(findings[1].startswith("page.md:3:"))

    def test_leading_indentation_is_not_a_spacing_finding(self):
        self.assertEqual(script.check_text("  Text.\n    Text.\n\tText.", "page.tsx"), [])
        findings = script.check_text("  Text  mit Lücke.", "page.tsx")
        self.assertEqual(len(findings), 1)
        self.assertIn("Mehrfache Leerzeichen", findings[0])


class CheckCopyCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text="Wir prüfen Ihre Anforderungen."):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def run_main(paths, stdin=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        stream = io.StringIO(stdin) if isinstance(stdin, str) else stdin
        with mock.patch.object(sys, "argv", ["check_copy.py", *map(str, paths)]), \
                mock.patch.object(sys, "stdin", stream), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = script.main()
        return status, stdout.getvalue(), stderr.getvalue()

    def test_clean_file_returns_zero_without_rewriting_it(self):
        page = self.write("page.md", "Grüße aus Köln.\r\n")
        original = page.read_bytes()
        status, stdout, stderr = self.run_main([page])
        self.assertEqual(status, 0)
        self.assertIn("Keine", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(page.read_bytes(), original)

    def test_findings_return_one(self):
        page = self.write("page.md", "Entdecken Sie unsere Angebote.")
        original = page.read_bytes()
        status, stdout, stderr = self.run_main([page])
        self.assertEqual(status, 1)
        self.assertIn(f"{page}:1:", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(page.read_bytes(), original)

    def test_utf8_bom_does_not_hide_a_phrase_at_line_start(self):
        page = self.write("page.md", "\ufeffNeue Maßstäbe setzen.")
        status, stdout, stderr = self.run_main([page])
        self.assertEqual(status, 1)
        self.assertIn(f"{page}:1:", stdout)
        self.assertEqual(stderr, "")

    def test_missing_path_returns_two_and_never_reports_success(self):
        status, stdout, stderr = self.run_main([self.root / "missing.md"])
        self.assertEqual(status, 2)
        self.assertNotIn("Keine", stdout)
        self.assertIn("nicht gefunden", stderr)

    def test_invalid_utf8_returns_two(self):
        page = self.root / "page.md"
        page.write_bytes(b"\xff\xfe")
        status, stdout, stderr = self.run_main([page])
        self.assertEqual(status, 2)
        self.assertNotIn("Keine", stdout)
        self.assertIn("UTF-8", stderr)

    def test_unreadable_file_returns_two(self):
        page = self.write("page.md")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            status, stdout, stderr = self.run_main([page])
        self.assertEqual(status, 2)
        self.assertNotIn("Keine", stdout)
        self.assertIn("denied", stderr)

    def test_directory_traversal_error_returns_two(self):
        def fail_walk(top, *, onerror, **kwargs):
            onerror(PermissionError("directory denied"))
            return iter(())

        with mock.patch.object(script.os, "walk", side_effect=fail_walk):
            status, stdout, stderr = self.run_main([self.root])
        self.assertEqual(status, 2)
        self.assertNotIn("Keine", stdout)
        self.assertIn("directory denied", stderr)

    def test_errors_take_precedence_but_valid_inputs_are_still_checked(self):
        page = self.write("page.md", "Mehr als nur Software.")
        for paths in ([self.root / "missing", page], [page, self.root / "missing"]):
            with self.subTest(paths=paths):
                status, stdout, stderr = self.run_main(paths)
                self.assertEqual(status, 2)
                self.assertIn("KI-Floskel", stdout)
                self.assertIn("nicht gefunden", stderr)

    def test_stdin_and_files_can_be_combined(self):
        page = self.write("page.md")
        status, stdout, stderr = self.run_main(["-", page], "Satz — Zusatz.")
        self.assertEqual(status, 1)
        self.assertIn("<stdin>:1:", stdout)
        self.assertEqual(stderr, "")

    def test_empty_stdin_is_a_valid_input(self):
        status, _, stderr = self.run_main(["-"])
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")

    def test_stdin_read_error_returns_two(self):
        for error in (
            OSError("stdin denied"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
        ):
            with self.subTest(error=error):
                stdin = mock.Mock()
                stdin.read.side_effect = error
                status, stdout, stderr = self.run_main(["-"], stdin)
                self.assertEqual(status, 2)
                self.assertNotIn("Keine", stdout)
                self.assertIn("stdin", stderr)

    def test_empty_or_unsupported_directory_returns_two(self):
        for name in (None, "image.png"):
            with self.subTest(name=name):
                if name:
                    self.write(name)
                status, stdout, stderr = self.run_main([self.root])
                self.assertEqual(status, 2)
                self.assertNotIn("Keine", stdout)
                self.assertIn("Keine unterstützten", stderr)

    def test_recursive_scan_filters_extensions_and_prunes_generated_directories(self):
        page = self.write("pages/page.MDX", "Mehr als nur Software.")
        self.write("pages/image.png", "Mehr als nur Software.")
        for directory in script.SKIP_DIRECTORIES:
            self.write(f"{directory}/ignored.md", "Mehr als nur Software.")
        status, stdout, stderr = self.run_main([self.root])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.count("KI-Floskel"), 1)
        self.assertIn(str(page), stdout)
        self.assertEqual(stderr, "")

    def test_file_discovery_is_sorted(self):
        self.write("z.md")
        self.write("a.md")
        self.assertEqual(
            list(script.iter_files([str(self.root)])),
            [self.root / "a.md", self.root / "z.md"],
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires named pipes")
    def test_named_pipe_is_rejected_without_reading_it(self):
        page = self.root / "pipe.md"
        os.mkfifo(page)
        with mock.patch.object(Path, "read_text") as read:
            status, stdout, stderr = self.run_main([self.root])
        self.assertEqual(status, 2)
        self.assertNotIn("Keine", stdout)
        self.assertIn("reguläre Datei", stderr)
        read.assert_not_called()

    def test_overlapping_paths_and_file_symlinks_are_checked_once(self):
        page = self.write("page.md", "Mehr als nur Software.")
        alias = self.root / "alias.md"
        alias.symlink_to(page)
        status, stdout, stderr = self.run_main([page, page, alias, self.root])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.count("KI-Floskel"), 1)
        self.assertEqual(stderr, "")

    def test_recursive_scan_does_not_follow_directory_symlinks(self):
        self.write("page.md")
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "page.md").write_text("Mehr als nur Software.", encoding="utf-8")
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
            status, _, stderr = self.run_main([self.root])
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")

    def test_explicit_files_accept_any_extension_and_excluded_directories(self):
        page = self.write("node_modules/COPY", "Mehr als nur Software.")
        status, stdout, stderr = self.run_main([page])
        self.assertEqual(status, 1)
        self.assertIn(str(page), stdout)
        self.assertEqual(stderr, "")

    def test_explicit_directory_root_bypasses_its_exclusion(self):
        page = self.write("build/page.md", "Mehr als nur Software.")
        status, stdout, stderr = self.run_main([page.parent])
        self.assertEqual(status, 1)
        self.assertIn(str(page), stdout)
        self.assertEqual(stderr, "")

    def test_script_runs_from_an_unrelated_working_directory(self):
        page = self.write("page.md")
        result = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts/check_copy.py"), str(page)],
            cwd=self.root, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
