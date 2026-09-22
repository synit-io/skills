import contextlib
import copy
import io
import json
import os
import stat
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import add_translations as script


def write_registry(directory, languages, version=1):
    path = Path(directory) / "languages.json"
    document = {"languages": languages}
    if version is not None:
        document["version"] = version
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class AddTranslationsTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        cls.registry = script.load_language_registry(cls.root / "references" / "languages.json")

    @staticmethod
    def source_question():
        return {
            "id": "question-1",
            "number": 1,
            "text": (
                '<p>Hello {user}. <a href="https://example.test/{{link}}">Read</a></p>'
            ),
            "choices": [{"text": "Yes"}, {"text": "No {user}"}],
        }

    @classmethod
    def export(cls, translations=None, additional_languages=None, multi_language=None):
        return {
            "exportedAt": "2026-09-14 08:00:00",
            "campaign": {
                "questions": [cls.source_question()],
                "translations": list(translations or []),
                "multiLanguageInfo": {
                    "defaultLanguage": "de",
                    "additionalLanguages": list(additional_languages or []),
                    "baseLanguage": "de",
                },
                "isMultiLanguageEnabled": (
                    bool(translations) if multi_language is None else multi_language
                ),
            },
        }

    @staticmethod
    def bundle(text=None, choices=None, language="fr"):
        return {
            language: {
                "questions": {
                    "question-1": {
                        "text": text
                        or '<p>Bonjour {user}. <a href="https://example.test/{{link}}">Lire</a></p>',
                        "choices": choices or ["Oui", "Non {user}"],
                    }
                }
            }
        }

    @staticmethod
    def translation_record(language, status="VALID", text="", choices=None):
        return {
            "language": language,
            "status": status,
            "questions": [
                {
                    "id": "question-1",
                    "comment": None,
                    "text": text,
                    "choices": [{"text": value} for value in (choices or [""])],
                }
            ],
        }

    @classmethod
    def valid_record(cls, language):
        return cls.translation_record(
            language, text=cls.source_question()["text"], choices=["Yes", "No {user}"]
        )

    def update(self, data, base_language, targets, bundle):
        source = script.validate_source(data, self.registry)
        return script.update_export(source, base_language, targets, bundle, self.registry)

    @staticmethod
    def run_main(argv, stdin=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        patches = [mock.patch.object(sys, "argv", ["add_translations.py", *argv])]
        if stdin is not None:
            patches.append(mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin))))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            result = script.main()
        report = json.loads(stdout.getvalue()) if result == 0 else None
        return result, report, stderr.getvalue()

    def write_case(self, directory, data=None, bundle=None):
        directory = Path(directory)
        input_path = directory / "input.json"
        bundle_path = directory / "bundle.json"
        input_path.write_text(json.dumps(data or self.export()), encoding="utf-8")
        bundle_path.write_text(json.dumps(bundle or self.bundle()), encoding="utf-8")
        return input_path, bundle_path

    # Language registry

    def test_language_registry_normalizes_names_and_aliases(self):
        self.assertEqual(self.registry.normalize(" Français "), "fr")
        self.assertEqual(self.registry.normalize("ZH-CN"), "zh")
        self.assertEqual(self.registry.normalize("Tagalog"), "fil")
        self.assertEqual(self.registry.normalize("nb"), "no")
        self.assertEqual(self.registry.normalize("nn"), "no")

        with self.assertRaisesRegex(script.TranslationError, "not allowed"):
            self.registry.normalize("Esperanto")

    def test_language_registry_ignores_unicode_form_and_separator(self):
        decomposed = unicodedata.normalize("NFD", "Français")
        self.assertNotEqual(decomposed, "Français")
        self.assertEqual(self.registry.normalize(decomposed), "fr")
        self.assertEqual(self.registry.normalize("zh_cn"), "zh")
        self.assertEqual(script.language_key("  Simplified\tChinese "), "simplified chinese")

    def test_language_registry_falls_back_to_primary_subtag(self):
        self.assertEqual(self.registry.resolve("pt-BR"), ("pt", True))
        self.assertEqual(self.registry.resolve("pt_br"), ("pt", True))
        self.assertEqual(self.registry.resolve("en-US"), ("en", True))
        self.assertEqual(self.registry.resolve("fr"), ("fr", False))
        # Chinese defines regional variants, so an unknown one is not collapsed.
        with self.assertRaisesRegex(script.TranslationError, "not allowed"):
            self.registry.normalize("zh-SG")
        with self.assertRaisesRegex(script.TranslationError, "not allowed"):
            self.registry.normalize("xx-YY")

    def test_language_registry_match_key_never_rejects(self):
        self.assertEqual(self.registry.match_key("Deutsch"), "de")
        self.assertEqual(self.registry.match_key("pt-BR"), "pt")
        self.assertEqual(self.registry.match_key("he"), "he")
        self.assertEqual(self.registry.match_key(" HE "), "he")

    def test_language_registry_rejects_disabled_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_registry(
                directory, [{"code": "xx", "name": "Example", "aliases": [], "enabled": False}]
            )
            registry = script.load_language_registry(path)

        with self.assertRaisesRegex(script.TranslationError, "disabled"):
            registry.normalize("xx")
        self.assertEqual(registry.match_key("Example"), "xx")

    def test_load_language_registry_rejects_bad_configurations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_registry(directory, [])
            with self.assertRaisesRegex(script.TranslationError, "at least one language"):
                script.load_language_registry(path)

            path = write_registry(
                directory,
                [
                    {"code": "aa", "name": "First", "aliases": ["shared"]},
                    {"code": "bb", "name": "Second", "aliases": ["Shared"]},
                ],
            )
            with self.assertRaisesRegex(script.TranslationError, "maps to both 'aa' and 'bb'"):
                script.load_language_registry(path)

            path = write_registry(directory, [{"code": "not a code", "name": "Bad"}])
            with self.assertRaisesRegex(script.TranslationError, "Invalid language code"):
                script.load_language_registry(path)

            path = write_registry(directory, [{"code": "aa", "name": "First"}], version=2)
            with self.assertRaisesRegex(script.TranslationError, "unsupported version 2"):
                script.load_language_registry(path)

            path = write_registry(directory, [{"code": "aa", "name": "First"}], version=None)
            self.assertEqual(script.load_language_registry(path).normalize("aa"), "aa")

    # Localized string validation

    def test_validate_localized_string_preserves_tokens_and_hrefs(self):
        source = '<p>Hello {user}. <a href="https://example.test/{{link}}">Read</a></p>'
        target = '<p>Bonjour {user}. <a href="https://example.test/{{link}}">Lire</a></p>'

        self.assertEqual(script.validate_localized_string(source, target, "fr.text"), target)

        with self.assertRaisesRegex(script.TranslationError, "protected brace tokens"):
            script.validate_localized_string(source, "<p>Bonjour</p>", "fr.text")
        with self.assertRaisesRegex(script.TranslationError, "attribute 'href' expected"):
            script.validate_localized_string(
                source,
                '<p>Bonjour {user}. <a href="https://other.test/{{link}}">Lire</a></p>',
                "fr.text",
            )
        with self.assertRaisesRegex(script.TranslationError, "must not be empty"):
            script.validate_localized_string(source, "   ", "fr.text")

    def test_validate_localized_string_rejects_structural_changes(self):
        source = (
            '<p>Hi {user}. <a href="https://a.test/{{link}}">A</a> '
            '<a href="https://b.test">B</a></p>'
        )
        cases = {
            "added script": (
                source + "<script>alert(1)</script>",
                "adds HTML that the source does not have: unexpected <script>",
            ),
            "added image": (
                source.replace("</p>", "<img src=x onerror=alert(1)></p>"),
                "changes HTML structure at tag 6: expected </p>, got <img>",
            ),
            "added event handler": (
                source.replace('<a href="https://b.test">', '<a href="https://b.test" onclick="x()">'),
                "changes attributes of <a> \\(tag 4\\): unexpected attribute 'onclick'",
            ),
            "unquoted javascript href": (
                source.replace('href="https://b.test"', "href=javascript:alert(1)"),
                "attribute 'href' expected 'https://b.test', got 'javascript:alert\\(1\\)'",
            ),
            "swapped hrefs": (
                source.replace("https://a.test/{{link}}", "TMP")
                .replace("https://b.test", "https://a.test/{{link}}")
                .replace("TMP", "https://b.test"),
                "attribute 'href' expected 'https://a.test/{{link}}', got 'https://b.test'",
            ),
            "token moved into attribute": (
                source.replace("Hi {user}.", "Salut.").replace(
                    "https://b.test", "https://b.test/{user}"
                ),
                "attribute 'href' expected 'https://b.test', got 'https://b.test/{user}'",
            ),
            "tag renamed": (
                source.replace("<p>", "<div>").replace("</p>", "</div>"),
                "changes HTML structure at tag 1: expected <p>, got <div>",
            ),
            "tag dropped": (
                source.replace("</p>", ""),
                "drops source HTML: missing </p> at tag 6",
            ),
            "self-closing form changed": (
                source.replace("</a></p>", "</a><br/></p>"),
                "expected </p>, got <br/>",
            ),
            "raw tag opener in text": (
                source.replace("Hi {user}.", "Hi {user} <b 1."),
                "changes HTML structure at tag 2",
            ),
            "unterminated tag at end": (
                source + "<img src=x onerror=alert(1)",
                "raw '<i' in text that could open a tag",
            ),
            "unterminated comment at end": (
                source + "<!-- x",
                "raw '<!' in text",
            ),
        }
        for name, (target, message) in cases.items():
            with self.subTest(name):
                with self.assertRaisesRegex(script.TranslationError, message):
                    script.validate_localized_string(source, target, "fr.text")

    def test_validate_localized_string_accepts_text_only_changes(self):
        source = '<p>Hi {user}. <a href="https://a.test/{{link}}">A</a> 1 &lt; 2</p>'
        accepted = [
            '<p>Salut {user}. <a href="https://a.test/{{link}}">A</a> 1 &lt; 2</p>',
            '<p>Salut {user}. <a href="https://a.test/{{link}}">A</a> 2 &gt; 1 &amp; 3 &#60; 4</p>',
            '<p>{user}, salut. <a href="https://a.test/{{link}}">A</a> 1 < 2 <3</p>',
            '<P>Salut {user}. <A HREF="https://a.test/{{link}}">A</A> 1 &lt; 2</P>',
        ]
        for target in accepted:
            with self.subTest(target):
                self.assertEqual(script.validate_localized_string(source, target, "l"), target)
        # Attribute values compare after HTML-unescaping, regardless of quoting.
        self.assertEqual(
            script.validate_localized_string(
                '<a href="a&amp;b" title=\'t\'>x</a>', "<a title=\"t\" href='a&amp;b'>y</a>", "l"
            ),
            "<a title=\"t\" href='a&amp;b'>y</a>",
        )
        with self.assertRaisesRegex(script.TranslationError, "attribute 'href' expected 'a&b'"):
            script.validate_localized_string('<a href="a&amp;b">x</a>', '<a href="a&b&c">y</a>', "l")

    def test_validate_localized_string_plain_text_and_empty_source(self):
        self.assertEqual(script.validate_localized_string("Yes", "Oui", "l"), "Oui")
        self.assertEqual(script.validate_localized_string("", "", "l"), "")
        self.assertEqual(script.validate_localized_string("  ", "", "l"), "")
        with self.assertRaisesRegex(script.TranslationError, "unexpected <b>"):
            script.validate_localized_string("Yes", "<b>Oui</b>", "l")
        with self.assertRaisesRegex(script.TranslationError, "must be empty because the source"):
            script.validate_localized_string("", "Oui", "l")
        with self.assertRaisesRegex(script.TranslationError, "raw '<b' in text"):
            script.validate_localized_string("a < b", "a <b", "l")

    # Source validation and language resolution

    def test_discover_target_languages_deduplicates_and_excludes_base(self):
        data = self.export(
            translations=[self.translation_record("it", text="Ciao", choices=["Sì", "No"])],
            additional_languages=["de", "fr", "fr"],
        )
        source = script.validate_source(data, self.registry)

        self.assertEqual(script.discover_target_languages(source, "de", self.registry), ["fr", "it"])

        source = script.validate_source(self.export(), self.registry)
        with self.assertRaisesRegex(script.TranslationError, "no target languages"):
            script.discover_target_languages(source, "de", self.registry)

    def test_validate_source_rejects_malformed_exports(self):
        duplicate = self.export(translations=[self.valid_record("fr"), self.valid_record("FR")])
        with self.assertRaisesRegex(script.TranslationError, "Duplicate translation language"):
            script.validate_source(duplicate, self.registry)

        bad_language = self.export(translations=[{"language": 3, "status": "VALID", "questions": []}])
        with self.assertRaisesRegex(script.TranslationError, "translations\\[0\\].language must be"):
            script.validate_source(bad_language, self.registry)

        bad_info = self.export()
        bad_info["campaign"]["multiLanguageInfo"]["additionalLanguages"] = "fr"
        with self.assertRaisesRegex(script.TranslationError, "additionalLanguages must be"):
            script.validate_source(bad_info, self.registry)

    def test_resolve_base_language_compares_with_export(self):
        source = script.validate_source(self.export(), self.registry)
        self.assertEqual(script.resolve_base_language(None, source, self.registry), "de")
        self.assertEqual(script.resolve_base_language("German", source, self.registry), "de")
        self.assertEqual(script.resolve_base_language("de-AT", source, self.registry), "de")
        with self.assertRaisesRegex(script.TranslationError, "Base language mismatch"):
            script.resolve_base_language("en", source, self.registry)

        hebrew = self.export()
        hebrew["campaign"]["multiLanguageInfo"]["baseLanguage"] = "he"
        source = script.validate_source(hebrew, self.registry)
        self.assertEqual(script.resolve_base_language(None, source, self.registry), "he")
        self.assertEqual(script.resolve_base_language("HE", source, self.registry), "he")

    def test_resolve_target_languages_reports_regional_fallbacks(self):
        targets, fallbacks = script.resolve_target_languages(
            ["pt-BR", "French", "pt", "en_US"], self.registry
        )
        self.assertEqual(targets, ["pt", "fr", "en"])
        self.assertEqual(fallbacks, {"pt-BR": "pt", "en_US": "en"})

    # Building translations

    def test_build_translation_accepts_question_number_and_choice_objects(self):
        question = {
            "id": "question-1",
            "number": 4,
            "text": "Hello {user}",
            "choices": [{"text": "Yes"}, {"text": "No"}],
        }
        payload = {
            "questions": {
                "4": {
                    "text": "Hallo {user}",
                    "choices": [{"text": "Ja"}, {"text": "Nein"}],
                }
            }
        }

        result = script.build_translation("de", [question], payload)

        self.assertEqual(result["language"], "de")
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["questions"][0]["choices"], [{"text": "Ja"}, {"text": "Nein"}])
        self.assertEqual(
            script.build_translation("de", [question], payload, spelling="de-DE")["language"],
            "de-DE",
        )

    def test_build_translation_rejects_unconsumed_bundle_keys(self):
        payload = self.bundle()["fr"]
        payload["questions"]["question-2"] = payload["questions"]["question-1"]

        with self.assertRaisesRegex(
            script.TranslationError,
            "match no base question: 'question-2'. Valid keys: 'question-1' \\(or '1'\\)",
        ):
            script.build_translation("fr", [self.source_question()], payload)

        with self.assertRaisesRegex(script.TranslationError, "Missing translation bundle entry"):
            script.build_translation("fr", [self.source_question()], {"questions": {}})

    def test_build_translation_rejects_choice_count_mismatch(self):
        with self.assertRaisesRegex(script.TranslationError, "choice count mismatch: expected 2, got 1"):
            script.build_translation("fr", [self.source_question()], self.bundle(choices=["Oui"])["fr"])

    def test_ensure_base_question_ids_generates_ids_and_rejects_duplicates(self):
        data = self.export()
        data["campaign"]["questions"][0].pop("id")

        generated = script.ensure_base_question_ids(data)

        self.assertEqual(len(generated), 1)
        self.assertRegex(generated[0], r"^[A-Za-z0-9_-]{21}$")
        self.assertEqual(data["campaign"]["questions"][0]["id"], generated[0])

        duplicate = self.export()
        duplicate["campaign"]["questions"].append(copy.deepcopy(self.source_question()))
        with self.assertRaisesRegex(script.TranslationError, "Duplicate base question id"):
            script.ensure_base_question_ids(duplicate)

        unaddressable = self.export()
        unaddressable["campaign"]["questions"][0].pop("id")
        unaddressable["campaign"]["questions"][0]["number"] = None
        with self.assertRaisesRegex(script.TranslationError, "has neither id nor number"):
            script.ensure_base_question_ids(unaddressable)

    def test_ensure_base_question_ids_propagates_generated_id_to_translations(self):
        invalid = self.translation_record("fr", status="INVALID")
        invalid["questions"][0]["id"] = None
        keeps_own = self.translation_record("it", text="Ciao", choices=["Sì", "No"])
        keeps_own["questions"][0]["id"] = "existing-translation-id"
        data = self.export(translations=[invalid, keeps_own])
        data["campaign"]["questions"][0].pop("id")

        generated = script.ensure_base_question_ids(data)

        self.assertEqual(len(generated), 1)
        self.assertEqual(data["campaign"]["questions"][0]["id"], generated[0])
        self.assertEqual(data["campaign"]["translations"][0]["questions"][0]["id"], generated[0])
        self.assertEqual(
            data["campaign"]["translations"][1]["questions"][0]["id"],
            "existing-translation-id",
        )

    # Updating the export

    def test_update_export_adds_translation_and_updates_metadata(self):
        data = self.export(additional_languages=["en"])

        report = self.update(data, "de", ["fr"], self.bundle())

        self.assertEqual(report, {
            "added": ["fr"],
            "repaired_invalid": [],
            "skipped_existing": [],
            "skipped_base_language": [],
            "invalid_not_repaired": [],
            "reconciled": [],
            "warnings": [],
        })
        self.assertEqual(data["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["en", "fr"])
        self.assertTrue(data["campaign"]["isMultiLanguageEnabled"])
        self.assertEqual(data["campaign"]["translations"][0]["language"], "fr")
        self.assertEqual(data["campaign"]["translations"][0]["status"], "VALID")

    def test_update_export_repairs_invalid_skips_valid_and_skips_base(self):
        valid = self.translation_record(
            "en",
            text="Hello {user}",
            choices=["Yes", "No {user}"],
        )
        invalid = self.translation_record("fr", status="INVALID")
        data = self.export(
            translations=[valid, invalid],
            additional_languages=["en", "fr"],
        )

        report = self.update(data, "de", ["de", "en", "fr"], self.bundle())

        self.assertEqual(report["skipped_base_language"], ["de"])
        self.assertEqual(report["skipped_existing"], ["en"])
        self.assertEqual(report["repaired_invalid"], ["fr"])
        self.assertEqual(data["campaign"]["translations"][1]["status"], "VALID")
        self.assertEqual(
            data["campaign"]["translations"][1]["questions"][0]["text"],
            self.bundle()["fr"]["questions"]["question-1"]["text"],
        )

    def test_update_export_rejects_unrequested_or_missing_bundle_languages(self):
        with self.assertRaisesRegex(script.TranslationError, "unrequested languages"):
            self.update(
                self.export(), "de", ["fr"], {**self.bundle(), "it": self.bundle()["fr"]}
            )

        with self.assertRaisesRegex(script.TranslationError, "Bundle missing language 'fr'"):
            self.update(self.export(), "de", ["fr"], {})

    def test_update_export_rejects_unsupported_status(self):
        data = self.export(translations=[self.translation_record("fr", status="PENDING")])
        with self.assertRaisesRegex(script.TranslationError, "unsupported status 'PENDING'"):
            self.update(data, "de", ["fr"], self.bundle())

    def test_update_export_stale_invalid_overlay_never_blocks(self):
        stale = {"language": "fr", "status": "INVALID", "questions": []}

        # A: repairing the stale overlay itself.
        data = self.export(translations=[copy.deepcopy(stale)], additional_languages=["fr"])
        report = self.update(data, "de", ["fr"], self.bundle())
        self.assertEqual(report["repaired_invalid"], ["fr"])
        self.assertEqual(report["invalid_not_repaired"], [])
        self.assertEqual(data["campaign"]["translations"][0]["questions"][0]["id"], "question-1")

        # B: adding an unrelated language while the stale overlay stays.
        data = self.export(translations=[copy.deepcopy(stale)], additional_languages=["fr"])
        report = self.update(data, "de", ["pl"], self.bundle(language="pl"))
        self.assertEqual(report["added"], ["pl"])
        self.assertEqual(report["invalid_not_repaired"], ["fr"])
        self.assertEqual(data["campaign"]["translations"][0], stale)
        self.assertEqual(data["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["fr", "pl"])

        # Overlay with questions: null is repairable when INVALID and targeted.
        data = self.export(translations=[{"language": "fr", "status": "INVALID", "questions": None}])
        self.assertEqual(self.update(data, "de", ["fr"], self.bundle())["repaired_invalid"], ["fr"])

    def test_update_export_matches_overlay_questions_by_id_not_position(self):
        second = copy.deepcopy(self.source_question())
        second.update({"id": "question-2", "number": 2, "text": "Bye", "choices": []})
        overlay = self.valid_record("it")
        overlay["questions"].insert(0, {"id": "question-2", "comment": None, "text": "Ciao", "choices": []})
        data = self.export(translations=[overlay])
        data["campaign"]["questions"].append(second)
        bundle = self.bundle()
        bundle["fr"]["questions"]["question-2"] = {"text": "Au revoir", "choices": []}

        report = self.update(data, "de", ["fr"], bundle)

        self.assertEqual(report["added"], ["fr"])
        self.assertEqual(report["reconciled"], ["added 'it' to multiLanguageInfo.additionalLanguages"])

        overlay["questions"][0]["id"] = "question-3"
        with self.assertRaisesRegex(script.TranslationError, "Translation 'it' question IDs must match"):
            self.update(self.export(translations=[overlay]), "de", ["fr"], self.bundle())

    def test_update_export_rejects_kept_overlay_without_question_list(self):
        data = self.export(translations=[{"language": "it", "status": "VALID", "questions": None}])
        with self.assertRaisesRegex(script.TranslationError, "translations\\[0\\].questions must be an array"):
            self.update(data, "de", ["fr"], self.bundle())

        data = self.export(translations=[{"language": "it", "status": "VALID", "questions": None}])
        data["campaign"]["questions"][0].pop("id")
        script.ensure_base_question_ids(data)
        with self.assertRaisesRegex(script.TranslationError, "translations\\[0\\].questions must be an array"):
            self.update(data, "de", ["fr"], self.bundle())

    def test_update_export_keeps_existing_languages_outside_allowlist(self):
        hebrew = self.valid_record("he")
        data = self.export(translations=[hebrew], additional_languages=["he", "pt-BR"])

        report = self.update(data, "de", ["fr"], self.bundle())

        self.assertEqual(report["added"], ["fr"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(data["campaign"]["translations"][0], hebrew)
        self.assertEqual(
            data["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["he", "pt-BR", "fr"]
        )
        with self.assertRaisesRegex(script.TranslationError, "Language 'he' is not allowed"):
            self.update(self.export(translations=[self.translation_record("he", status="INVALID")]),
                        "de", ["he"], {})

    def test_update_export_keeps_disabled_existing_language(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = script.load_language_registry(write_registry(directory, [
                {"code": "de", "name": "German"},
                {"code": "fr", "name": "French"},
                {"code": "it", "name": "Italian", "enabled": False},
            ]))
        data = self.export(translations=[self.valid_record("it")], additional_languages=["it"])
        source = script.validate_source(data, registry)

        report = script.update_export(source, "de", ["fr"], self.bundle(), registry)

        self.assertEqual(report["added"], ["fr"])
        self.assertEqual(data["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["it", "fr"])
        # A discovered target that is already VALID is skipped without any allowlist check.
        report = script.update_export(source, "de", ["it"], {}, registry)
        self.assertEqual(report["skipped_existing"], ["it"])

        # Writing a disabled language is refused, even to repair it.
        data["campaign"]["translations"][0]["status"] = "INVALID"
        with self.assertRaisesRegex(script.TranslationError, "disabled"):
            script.update_export(source, "de", ["it"], self.bundle(language="it"), registry)

    def test_update_export_preserves_existing_additional_language_entries(self):
        data = self.export(additional_languages=["zh-CN", "en"])
        bundle = {
            **self.bundle(),
            "zh": {
                "questions": {
                    "question-1": {
                        "text": '<p>你好 {user}. <a href="https://example.test/{{link}}">阅读</a></p>',
                        "choices": ["是", "否 {user}"],
                    }
                }
            },
        }

        report = self.update(data, "de", ["fr", "zh"], bundle)

        self.assertEqual(report["added"], ["fr", "zh"])
        self.assertEqual(
            data["campaign"]["multiLanguageInfo"]["additionalLanguages"],
            ["zh-CN", "en", "fr"],
        )
        # One spelling per language: the new overlay reuses the bookkeeping spelling.
        self.assertEqual(
            [item["language"] for item in data["campaign"]["translations"]], ["fr", "zh-CN"]
        )

    def test_update_export_repair_keeps_overlay_spelling(self):
        invalid = self.translation_record("zh-CN", status="INVALID")
        data = self.export(translations=[invalid], additional_languages=["zh-CN"])
        bundle = {"zh": self.bundle()["fr"]}
        bundle["zh"]["questions"]["question-1"]["text"] = (
            '<p>你好 {user}. <a href="https://example.test/{{link}}">阅读</a></p>'
        )

        report = self.update(data, "de", ["zh"], bundle)

        self.assertEqual(report["repaired_invalid"], ["zh"])
        self.assertEqual(data["campaign"]["translations"][0]["language"], "zh-CN")
        self.assertEqual(data["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["zh-CN"])

        # Bookkeeping that spells the repaired language differently follows the overlay.
        data = self.export(translations=[copy.deepcopy(invalid)], additional_languages=["zh"])
        report = self.update(data, "de", ["zh"], copy.deepcopy(bundle))
        self.assertEqual(data["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["zh-CN"])
        self.assertEqual(report["reconciled"], ["respelled additionalLanguages entry 'zh' as 'zh-CN' to match its translation"])

    def test_update_export_reconciles_bookkeeping_for_valid_overlays(self):
        data = self.export(translations=[self.valid_record("fr")], additional_languages=[], multi_language=False)

        report = self.update(data, "de", ["fr"], {})

        self.assertEqual(report["skipped_existing"], ["fr"])
        self.assertEqual(
            report["reconciled"],
            [
                "added 'fr' to multiLanguageInfo.additionalLanguages",
                "set isMultiLanguageEnabled to true",
            ],
        )
        self.assertEqual(data["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["fr"])
        self.assertTrue(data["campaign"]["isMultiLanguageEnabled"])

        mismatch = self.export(translations=[self.valid_record("zh-CN")], additional_languages=["zh"])
        report = self.update(mismatch, "de", ["zh"], {})
        self.assertEqual(report["reconciled"], [])
        self.assertEqual(report["warnings"], ["additionalLanguages spells 'zh' but its translation uses 'zh-CN'; left unchanged"])

    # Serialization

    def test_detect_json_style(self):
        compact = script.detect_json_style('{"a":1,"b":[1,2]}')
        self.assertEqual((compact.indent, compact.item_separator, compact.key_separator), (None, ",", ":"))
        self.assertFalse(compact.trailing_newline)
        self.assertFalse(compact.ensure_ascii)

        spaced = script.detect_json_style('{"a": 1, "b": "\\u00e9"}\n')
        self.assertEqual((spaced.indent, spaced.item_separator, spaced.key_separator), (None, ", ", ": "))
        self.assertTrue(spaced.trailing_newline)
        self.assertTrue(spaced.ensure_ascii)

        indented = script.detect_json_style('{\r\n    "a": "é"\r\n}\r\n')
        self.assertEqual((indented.indent, indented.newline), ("    ", "\r\n"))
        self.assertFalse(indented.ensure_ascii)

    def test_serialize_json_round_trips_input_style(self):
        for text in ['{"a":1,"b":["é",{"c":null}]}', '{"a": 1, "b": "\\u00e9"}\n', '{\n  "a": [\n    1\n  ]\n}\n']:
            with self.subTest(text):
                data, _ = script.decode_json(text.encode("utf-8"), "test")
                self.assertEqual(script.serialize_json(data, script.detect_json_style(text)), text.encode("utf-8"))

        with self.assertRaisesRegex(script.TranslationError, "cannot be encoded as UTF-8"):
            script.serialize_json({"a": "\ud83d"}, script.JsonStyle())

    def test_decode_json_accepts_bom_and_rejects_other_encodings(self):
        data, text = script.decode_json(b'\xef\xbb\xbf{"a": 1}', "test")
        self.assertEqual(data, {"a": 1})
        self.assertFalse(text.startswith("\ufeff"))
        with self.assertRaisesRegex(script.TranslationError, "not valid UTF-8"):
            script.decode_json('{"a": 1}'.encode("utf-16"), "test")
        with self.assertRaisesRegex(script.TranslationError, "Invalid JSON in test"):
            script.decode_json(b"{", "test")

    def test_write_json_writes_valid_json_and_requires_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            self.assertIsNone(script.write_json({"ok": True}, output))
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o666 & ~script.current_umask())

            with self.assertRaisesRegex(script.TranslationError, "Output directory does not exist"):
                script.write_json({"ok": False}, Path(directory) / "missing" / "output.json")

    def test_write_json_keeps_mode_follows_symlinks_and_backs_up(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            real = directory / "real.json"
            real.write_bytes(b'{"old": true}')
            os.chmod(real, 0o640)
            link = directory / "link.json"
            link.symlink_to(real)

            backup = script.write_json({"new": True}, link, backup=True)

            self.assertTrue(link.is_symlink())
            self.assertEqual(json.loads(real.read_text(encoding="utf-8")), {"new": True})
            self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o640)
            self.assertEqual(backup, directory.resolve() / "real.json.bak")
            self.assertEqual(backup.read_bytes(), b'{"old": true}')
            self.assertEqual(sorted(path.name for path in directory.iterdir()),
                             ["link.json", "real.json", "real.json.bak"])

    # Command line

    def test_main_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path, bundle_path = self.write_case(directory)
            output_path = Path(directory) / "output.json"

            result, report, _ = self.run_main([
                str(input_path), "--base-language", "de", "--target-language", "fr",
                "--translations-file", str(bundle_path), "--output", str(output_path),
            ])

            self.assertEqual(result, 0)
            self.assertEqual(report["added"], ["fr"])
            self.assertTrue(report["changed"])
            self.assertEqual(report["output"], str(output_path))
            self.assertIsNone(report["backup"])
            self.assertEqual(report["normalized_targets"], {})
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8"))["campaign"]["translations"][0]["language"],
                "fr",
            )

    def test_main_infers_base_and_targets_from_export(self):
        data = self.export(additional_languages=["fr"])
        with tempfile.TemporaryDirectory() as directory:
            input_path, bundle_path = self.write_case(directory, data)
            output_path = Path(directory) / "output.json"

            result, report, _ = self.run_main([
                str(input_path), "--from-export-languages",
                "--translations-file", str(bundle_path), "--output", str(output_path),
            ])

            self.assertEqual(result, 0)
            self.assertEqual(report["target_languages"], ["fr"])

            result, _, stderr = self.run_main([
                str(input_path), "--from-export-languages",
                "--translations-file", str(bundle_path), "--output", str(output_path),
            ])
            self.assertEqual(result, 0)

            input_path.write_text(json.dumps(self.export()), encoding="utf-8")
            result, _, stderr = self.run_main([
                str(input_path), "--from-export-languages",
                "--translations-file", str(bundle_path), "--output", str(output_path),
            ])
            self.assertEqual(result, 2)
            self.assertIn("no target languages", stderr)

    def test_main_reports_invalid_output_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path, bundle_path = self.write_case(directory)

            result, _, stderr = self.run_main([
                str(input_path), "--base-language", "de", "--target-language", "fr",
                "--translations-file", str(bundle_path), "--output", str(input_path),
            ])

            self.assertEqual(result, 2)
            self.assertIn("Use --in-place", stderr)

    def test_main_reports_base_language_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path, bundle_path = self.write_case(directory)
            result, _, stderr = self.run_main([
                str(input_path), "--base-language", "en", "--target-language", "fr",
                "--translations-file", str(bundle_path), "--output", str(Path(directory) / "o.json"),
            ])
            self.assertEqual(result, 2)
            self.assertIn("Base language mismatch: export is 'de', request is 'en'", stderr)

    def test_main_reports_regional_fallback_and_reads_bundle_from_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path, _ = self.write_case(directory)
            output_path = Path(directory) / "output.json"
            bundle = json.dumps({"pt": self.bundle()["fr"]}).encode("utf-8")

            result, report, _ = self.run_main([
                str(input_path), "--base-language", "de", "--target-language", "pt-BR",
                "--translations-file", "-", "--output", str(output_path),
            ], stdin=bundle)

            self.assertEqual(result, 0)
            self.assertEqual(report["target_languages"], ["pt"])
            self.assertEqual(report["normalized_targets"], {"pt-BR": "pt"})
            written = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(written["campaign"]["translations"][0]["language"], "pt")
            self.assertEqual(written["campaign"]["multiLanguageInfo"]["additionalLanguages"], ["pt"])

    def test_main_generates_ids_and_repairs_overlay_without_ids(self):
        invalid = self.translation_record("fr", status="INVALID")
        invalid["questions"][0]["id"] = None
        data = self.export(translations=[invalid], additional_languages=["fr"])
        data["campaign"]["questions"][0].pop("id")
        bundle = {"fr": {"questions": {"1": self.bundle()["fr"]["questions"]["question-1"]}}}
        with tempfile.TemporaryDirectory() as directory:
            input_path, bundle_path = self.write_case(directory, data, bundle)
            output_path = Path(directory) / "output.json"

            result, report, _ = self.run_main([
                str(input_path), "--from-export-languages",
                "--translations-file", str(bundle_path), "--output", str(output_path),
            ])

            self.assertEqual(result, 0)
            self.assertEqual(report["repaired_invalid"], ["fr"])
            self.assertEqual(len(report["generated_ids"]), 1)
            written = json.loads(output_path.read_text(encoding="utf-8"))
            base_id = written["campaign"]["questions"][0]["id"]
            self.assertEqual(base_id, report["generated_ids"][0])
            self.assertEqual(written["campaign"]["translations"][0]["questions"][0]["id"], base_id)
            self.assertEqual(written["campaign"]["translations"][0]["status"], "VALID")

    def test_main_noop_run_does_not_write_and_real_run_is_idempotent(self):
        data = self.export(translations=[self.valid_record("fr")], additional_languages=["fr"])
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path, bundle_path = self.write_case(directory, data, {})
            output_path = directory / "output.json"
            original = input_path.read_bytes()
            common = ["--base-language", "de", "--target-language", "fr", "--translations-file", str(bundle_path)]

            result, report, _ = self.run_main([str(input_path), *common, "--output", str(output_path)])
            self.assertEqual(result, 0)
            self.assertFalse(report["changed"])
            self.assertIsNone(report["output"])
            self.assertEqual(report["skipped_existing"], ["fr"])
            self.assertFalse(output_path.exists())

            result, report, _ = self.run_main([str(input_path), *common, "--in-place"])
            self.assertEqual(result, 0)
            self.assertFalse(report["changed"])
            self.assertEqual(input_path.read_bytes(), original)
            self.assertFalse(input_path.with_name("input.json.bak").exists())

            # A real change, then the same request again: byte-identical output.
            self.write_case(directory, self.export(), self.bundle())
            result, report, _ = self.run_main([str(input_path), *common, "--output", str(output_path)])
            self.assertTrue(report["changed"])
            first = output_path.read_bytes()
            result, report, _ = self.run_main([str(output_path), *common, "--in-place"])
            self.assertEqual(result, 0)
            self.assertFalse(report["changed"])
            self.assertEqual(output_path.read_bytes(), first)

    def test_main_preserves_input_style(self):
        data = self.export()
        data["campaign"]["name"] = "Größe"
        escaped = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        self.assertNotIn("\n", escaped)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = directory / "input.json"
            input_path.write_bytes(b"\xef\xbb\xbf" + escaped.encode("ascii"))
            bundle_path = directory / "bundle.json"
            bundle_path.write_text(json.dumps(self.bundle()), encoding="utf-8")
            output_path = directory / "output.json"

            result, report, _ = self.run_main([
                str(input_path), "--base-language", "de", "--target-language", "fr",
                "--translations-file", str(bundle_path), "--output", str(output_path),
            ])

            self.assertEqual(result, 0)
            written = output_path.read_bytes()
            self.assertFalse(written.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(written.isascii())
            self.assertIn(b"Gr\\u00f6\\u00dfe", written)
            self.assertNotIn(b"\n", written)
            self.assertIn(b'"language":"fr"', written)
            self.assertEqual(json.loads(written)["campaign"]["translations"][0]["language"], "fr")

    def test_main_in_place_backs_up_and_honors_no_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path, bundle_path = self.write_case(directory)
            original = input_path.read_bytes()
            common = ["--base-language", "de", "--target-language", "fr", "--translations-file", str(bundle_path)]

            result, report, _ = self.run_main([str(input_path), *common, "--in-place"])

            self.assertEqual(result, 0)
            backup = directory / "input.json.bak"
            self.assertEqual(report["backup"], str(backup.resolve()))
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(json.loads(input_path.read_text(encoding="utf-8"))["campaign"]["translations"][0]["language"], "fr")

            backup.unlink()
            self.write_case(directory)
            result, report, _ = self.run_main([str(input_path), *common, "--in-place", "--no-backup"])
            self.assertEqual(result, 0)
            self.assertIsNone(report["backup"])
            self.assertFalse(backup.exists())

    def test_main_reports_encoding_problems_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path, bundle_path = self.write_case(directory)
            output_path = directory / "output.json"
            common = ["--base-language", "de", "--target-language", "fr",
                      "--translations-file", str(bundle_path), "--output", str(output_path)]

            input_path.write_bytes(json.dumps(self.export()).encode("utf-16"))
            result, _, stderr = self.run_main([str(input_path), *common])
            self.assertEqual(result, 2)
            self.assertIn("not valid UTF-8", stderr)

            input_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(self.export()).encode("utf-8"))
            result, report, _ = self.run_main([str(input_path), *common])
            self.assertEqual(result, 0)
            self.assertFalse(output_path.read_bytes().startswith(b"\xef\xbb\xbf"))

            # A lone surrogate escape in an ASCII-styled file round-trips as an escape.
            data = self.export()
            data["campaign"]["name"] = "\ud83d"
            input_path.write_text(json.dumps(data), encoding="utf-8")
            result, report, _ = self.run_main([str(input_path), *common])
            self.assertEqual(result, 0)
            self.assertIn(b"\\ud83d", output_path.read_bytes())

            # In a file written with raw UTF-8 it cannot be encoded back: clean exit 2.
            data["campaign"]["name"] = "\ud83d café"
            text = json.dumps(data, ensure_ascii=False).replace("\ud83d", "\\ud83d")
            input_path.write_text(text, encoding="utf-8")
            result, _, stderr = self.run_main([str(input_path), *common])
            self.assertEqual(result, 2)
            self.assertIn("cannot be encoded as UTF-8", stderr)

    def test_main_reports_unconsumed_bundle_keys(self):
        bundle = self.bundle()
        bundle["fr"]["questions"]["questoin-1"] = bundle["fr"]["questions"]["question-1"]
        with tempfile.TemporaryDirectory() as directory:
            input_path, bundle_path = self.write_case(directory, bundle=bundle)
            result, _, stderr = self.run_main([
                str(input_path), "--base-language", "de", "--target-language", "fr",
                "--translations-file", str(bundle_path), "--output", str(Path(directory) / "o.json"),
            ])
            self.assertEqual(result, 2)
            self.assertIn("'questoin-1'", stderr)
            self.assertIn("Valid keys: 'question-1'", stderr)

    def test_main_handles_unexpected_errors_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path, bundle_path = self.write_case(directory)
            with mock.patch.object(script, "run", side_effect=RuntimeError("boom")):
                result, _, stderr = self.run_main([
                    str(input_path), "--base-language", "de", "--target-language", "fr",
                    "--translations-file", str(bundle_path), "--output", str(Path(directory) / "o.json"),
                ])
            self.assertEqual(result, 1)
            self.assertEqual(stderr, "error: unexpected RuntimeError: boom\n")


if __name__ == "__main__":
    unittest.main()
