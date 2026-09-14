import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import add_translations as script


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
    def export(cls, translations=None, additional_languages=None):
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
                "isMultiLanguageEnabled": bool(translations),
            },
        }

    @staticmethod
    def bundle(text=None, choices=None):
        return {
            "fr": {
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

    def test_language_registry_normalizes_names_and_aliases(self):
        self.assertEqual(self.registry.normalize(" Français "), "fr")
        self.assertEqual(self.registry.normalize("ZH-CN"), "zh")
        self.assertEqual(self.registry.normalize("Tagalog"), "fil")

        with self.assertRaisesRegex(script.TranslationError, "not allowed"):
            self.registry.normalize("Esperanto")

    def test_language_registry_rejects_disabled_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "languages.json"
            path.write_text(
                json.dumps(
                    {
                        "languages": [
                            {
                                "code": "xx",
                                "name": "Example",
                                "aliases": [],
                                "enabled": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = script.load_language_registry(path)

        with self.assertRaisesRegex(script.TranslationError, "disabled"):
            registry.normalize("xx")

    def test_load_language_registry_rejects_empty_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "languages.json"
            path.write_text('{"languages": []}', encoding="utf-8")

            with self.assertRaisesRegex(script.TranslationError, "at least one language"):
                script.load_language_registry(path)

    def test_validate_localized_string_preserves_tokens_and_hrefs(self):
        source = '<p>Hello {user}. <a href="https://example.test/{{link}}">Read</a></p>'
        target = '<p>Bonjour {user}. <a href="https://example.test/{{link}}">Lire</a></p>'

        self.assertEqual(script.validate_localized_string(source, target, "fr.text"), target)

        with self.assertRaisesRegex(script.TranslationError, "protected brace tokens"):
            script.validate_localized_string(source, "<p>Bonjour</p>", "fr.text")
        with self.assertRaisesRegex(script.TranslationError, "href values"):
            script.validate_localized_string(
                source,
                '<p>Bonjour {user}. <a href="https://other.test/{{link}}">Lire</a></p>',
                "fr.text",
            )
        with self.assertRaisesRegex(script.TranslationError, "must not be empty"):
            script.validate_localized_string(source, "   ", "fr.text")

    def test_discover_target_languages_deduplicates_and_excludes_base(self):
        data = self.export(
            translations=[self.translation_record("it", text="Ciao", choices=["Sì", "No"])],
            additional_languages=["de", "fr", "fr"],
        )

        self.assertEqual(script.discover_target_languages(data, "de", self.registry), ["fr", "it"])

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

    def test_update_export_adds_translation_and_updates_metadata(self):
        data = self.export(additional_languages=["en"])

        report = script.update_export(
            data,
            "de",
            ["fr"],
            self.bundle(),
            self.registry,
        )

        self.assertEqual(report, {
            "added": ["fr"],
            "repaired_invalid": [],
            "skipped_existing": [],
            "skipped_base_language": [],
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

        report = script.update_export(
            data,
            "de",
            ["de", "en", "fr"],
            self.bundle(),
            self.registry,
        )

        self.assertEqual(report["skipped_base_language"], ["de"])
        self.assertEqual(report["skipped_existing"], ["en"])
        self.assertEqual(report["repaired_invalid"], ["fr"])
        self.assertEqual(data["campaign"]["translations"][1]["status"], "VALID")
        self.assertEqual(
            data["campaign"]["translations"][1]["questions"][0]["text"],
            self.bundle()["fr"]["questions"]["question-1"]["text"],
        )

    def test_update_export_rejects_unrequested_or_missing_bundle_languages(self):
        data = self.export()
        with self.assertRaisesRegex(script.TranslationError, "unrequested languages"):
            script.update_export(
                data,
                "de",
                ["fr"],
                {**self.bundle(), "it": self.bundle()["fr"]},
                self.registry,
            )

        with self.assertRaisesRegex(script.TranslationError, "Bundle missing language 'fr'"):
            script.update_export(data, "de", ["fr"], {}, self.registry)

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

    def test_write_json_writes_valid_json_and_requires_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            script.write_json({"ok": True}, output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"ok": True})

            with self.assertRaisesRegex(script.TranslationError, "Output directory does not exist"):
                script.write_json({"ok": False}, Path(directory) / "missing" / "output.json")

    def test_main_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = directory / "input.json"
            bundle_path = directory / "bundle.json"
            output_path = directory / "output.json"
            input_path.write_text(json.dumps(self.export()), encoding="utf-8")
            bundle_path.write_text(json.dumps(self.bundle()), encoding="utf-8")

            argv = [
                "add_translations.py",
                str(input_path),
                "--base-language",
                "de",
                "--target-language",
                "fr",
                "--translations-file",
                str(bundle_path),
                "--output",
                str(output_path),
            ]
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                result = script.main()

            self.assertEqual(result, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["added"], ["fr"])
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8"))["campaign"]["translations"][0]["language"],
                "fr",
            )

    def test_main_infers_base_and_targets_from_export(self):
        data = self.export(additional_languages=["fr"])
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = directory / "input.json"
            bundle_path = directory / "bundle.json"
            output_path = directory / "output.json"
            input_path.write_text(json.dumps(data), encoding="utf-8")
            bundle_path.write_text(json.dumps(self.bundle()), encoding="utf-8")

            argv = [
                "add_translations.py",
                str(input_path),
                "--from-export-languages",
                "--translations-file",
                str(bundle_path),
                "--output",
                str(output_path),
            ]
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                result = script.main()

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue())["target_languages"], ["fr"])

    def test_main_reports_invalid_output_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = directory / "input.json"
            bundle_path = directory / "bundle.json"
            input_path.write_text(json.dumps(self.export()), encoding="utf-8")
            bundle_path.write_text(json.dumps(self.bundle()), encoding="utf-8")

            argv = [
                "add_translations.py",
                str(input_path),
                "--base-language",
                "de",
                "--target-language",
                "fr",
                "--translations-file",
                str(bundle_path),
                "--output",
                str(input_path),
            ]
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(stderr):
                result = script.main()

            self.assertEqual(result, 2)
            self.assertIn("Use --in-place", stderr.getvalue())


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

        report = script.update_export(data, "de", ["fr", "zh"], bundle, self.registry)

        self.assertEqual(report["added"], ["fr", "zh"])
        self.assertEqual(
            data["campaign"]["multiLanguageInfo"]["additionalLanguages"],
            ["zh-CN", "en", "fr"],
        )

    def test_main_generates_ids_and_repairs_overlay_without_ids(self):
        invalid = self.translation_record("fr", status="INVALID")
        invalid["questions"][0]["id"] = None
        data = self.export(translations=[invalid], additional_languages=["fr"])
        data["campaign"]["questions"][0].pop("id")
        bundle = {"fr": {"questions": {"1": self.bundle()["fr"]["questions"]["question-1"]}}}
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = directory / "input.json"
            bundle_path = directory / "bundle.json"
            output_path = directory / "output.json"
            input_path.write_text(json.dumps(data), encoding="utf-8")
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

            argv = [
                "add_translations.py",
                str(input_path),
                "--from-export-languages",
                "--translations-file",
                str(bundle_path),
                "--output",
                str(output_path),
            ]
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                result = script.main()

            self.assertEqual(result, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["repaired_invalid"], ["fr"])
            self.assertEqual(len(report["generated_ids"]), 1)
            written = json.loads(output_path.read_text(encoding="utf-8"))
            base_id = written["campaign"]["questions"][0]["id"]
            self.assertEqual(base_id, report["generated_ids"][0])
            self.assertEqual(written["campaign"]["translations"][0]["questions"][0]["id"], base_id)
            self.assertEqual(written["campaign"]["translations"][0]["status"], "VALID")


if __name__ == "__main__":
    unittest.main()
