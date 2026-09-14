#!/usr/bin/env python3
"""Extend a Nexthink campaign export with validated translation overlays.

The model supplies translated HTML and choice text through a small JSON bundle;
this script owns IDs, export shape, language bookkeeping, and invariants that
are easy to break during manual JSON editing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NANOID_ALPHABET = "_-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
TOKEN_PATTERN = re.compile(r"\{\{[^{}]*\}\}|\{[^{}]*\}")
HREF_PATTERN = re.compile(r"(?is)\bhref\s*=\s*([\"'])(.*?)\1")
LANGUAGE_CODE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class TranslationError(ValueError):
    """Raised for an invalid export or translation bundle."""


def language_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold().replace("_", "-"))


@dataclass(frozen=True)
class LanguageDefinition:
    code: str
    name: str
    aliases: tuple[str, ...]
    enabled: bool


class LanguageRegistry:
    """Configurable allowlist mapping language names and aliases to export codes."""

    def __init__(self, definitions: list[LanguageDefinition], source: Path):
        self.source = source
        self.definitions = definitions
        self._lookup: dict[str, LanguageDefinition] = {}
        for definition in definitions:
            if not LANGUAGE_CODE_PATTERN.fullmatch(definition.code):
                raise TranslationError(
                    f"Invalid language code {definition.code!r} in {source}"
                )
            for label in (definition.code, definition.name, *definition.aliases):
                key = language_key(label)
                previous = self._lookup.get(key)
                if previous is not None and previous.code != definition.code:
                    raise TranslationError(
                        f"Language alias {label!r} maps to both "
                        f"{previous.code!r} and {definition.code!r} in {source}"
                    )
                self._lookup[key] = definition

    def normalize(self, value: str) -> str:
        if not isinstance(value, str):
            raise TranslationError(f"Language must be a string, got {type(value).__name__}")
        definition = self._lookup.get(language_key(value))
        if definition is None:
            supported = ", ".join(
                f"{item.name} ({item.code})"
                for item in self.definitions
                if item.enabled
            )
            raise TranslationError(
                f"Language {value!r} is not allowed by {self.source}. "
                f"Supported languages: {supported}"
            )
        if not definition.enabled:
            raise TranslationError(
                f"Language {value!r} ({definition.code}) is disabled in {self.source}"
            )
        return definition.code


def load_language_registry(path: Path) -> LanguageRegistry:
    raw = read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("languages"), list):
        raise TranslationError(f"{path} must contain a languages array")
    definitions: list[LanguageDefinition] = []
    seen_codes: set[str] = set()
    for index, item in enumerate(raw["languages"]):
        if not isinstance(item, dict):
            raise TranslationError(f"{path}: languages[{index}] must be an object")
        code = item.get("code")
        name = item.get("name")
        aliases = item.get("aliases", [])
        enabled = item.get("enabled", True)
        if not isinstance(code, str) or not code.strip():
            raise TranslationError(f"{path}: languages[{index}].code must be a string")
        if not isinstance(name, str) or not name.strip():
            raise TranslationError(f"{path}: languages[{index}].name must be a string")
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise TranslationError(f"{path}: languages[{index}].aliases must be a string array")
        if not isinstance(enabled, bool):
            raise TranslationError(f"{path}: languages[{index}].enabled must be boolean")
        canonical_code = code.strip()
        if canonical_code in seen_codes:
            raise TranslationError(f"Duplicate language code {canonical_code!r} in {path}")
        seen_codes.add(canonical_code)
        definitions.append(
            LanguageDefinition(
                code=canonical_code,
                name=name.strip(),
                aliases=tuple(alias.strip() for alias in aliases),
                enabled=enabled,
            )
        )
    if not definitions:
        raise TranslationError(f"{path} must define at least one language")
    return LanguageRegistry(definitions, path)


def default_language_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "references" / "languages.json"


def deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise TranslationError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Invalid JSON in {path}: {exc}") from exc


def collect_ids(data: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    campaign = data.get("campaign")
    if not isinstance(campaign, dict):
        return ids
    for question in campaign.get("questions", []):
        if isinstance(question, dict) and isinstance(question.get("id"), str):
            ids.add(question["id"])
    for translation in campaign.get("translations", []):
        if not isinstance(translation, dict):
            continue
        for question in translation.get("questions", []):
            if isinstance(question, dict) and isinstance(question.get("id"), str):
                ids.add(question["id"])
    return ids


def generate_id(used_ids: set[str]) -> str:
    while True:
        candidate = "".join(secrets.choice(NANOID_ALPHABET) for _ in range(21))
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate


def propagate_question_id(
    translations: list[Any], position: int, question_id: str
) -> None:
    """Reuse a freshly generated base ID in every existing translation overlay.

    Translation questions map to base questions by array position, so a base
    question without an ID leaves the matching overlay entries without one as
    well. Entries that already carry a different ID are left alone; the later
    ID-order validation reports that mismatch instead of silently overwriting.
    """
    for translation in translations:
        if not isinstance(translation, dict):
            continue
        questions = translation.get("questions")
        if not isinstance(questions, list) or position >= len(questions):
            continue
        question = questions[position]
        if not isinstance(question, dict):
            continue
        existing = question.get("id")
        if not isinstance(existing, str) or not existing:
            question["id"] = question_id


def ensure_base_question_ids(data: dict[str, Any]) -> list[str]:
    campaign = data["campaign"]
    used_ids = collect_ids(data)
    translations = campaign.get("translations", [])
    if not isinstance(translations, list):
        translations = []
    generated: list[str] = []
    seen: set[str] = set()
    for index, question in enumerate(campaign["questions"]):
        if not isinstance(question, dict):
            raise TranslationError(f"campaign.questions[{index}] must be an object")
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id:
            question_id = generate_id(used_ids)
            question["id"] = question_id
            generated.append(question_id)
            propagate_question_id(translations, index, question_id)
        if question_id in seen:
            raise TranslationError(f"Duplicate base question id: {question_id}")
        seen.add(question_id)
    return generated


def tokens(value: str) -> Counter[str]:
    return Counter(TOKEN_PATTERN.findall(value))


def hrefs(value: str) -> Counter[str]:
    return Counter(match.group(2) for match in HREF_PATTERN.finditer(value))


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise TranslationError(f"{location} must be a string")
    return value


def validate_localized_string(source: Any, target: Any, location: str) -> str:
    source_text = require_string(source, f"source {location}")
    target_text = require_string(target, location)
    if not target_text.strip():
        raise TranslationError(f"{location} must not be empty")
    source_tokens = tokens(source_text)
    target_tokens = tokens(target_text)
    if source_tokens != target_tokens:
        raise TranslationError(
            f"{location} changes protected brace tokens: "
            f"expected {dict(source_tokens)!r}, got {dict(target_tokens)!r}"
        )
    source_hrefs = hrefs(source_text)
    target_hrefs = hrefs(target_text)
    if source_hrefs != target_hrefs:
        raise TranslationError(
            f"{location} changes href values: expected {list(source_hrefs.elements())!r}, "
            f"got {list(target_hrefs.elements())!r}"
        )
    return target_text


def validate_source(
    data: Any, base_language: str, registry: LanguageRegistry
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TranslationError("Export root must be an object")
    campaign = data.get("campaign")
    if not isinstance(campaign, dict):
        raise TranslationError("Export must contain campaign object")
    questions = campaign.get("questions")
    if not isinstance(questions, list) or not questions:
        raise TranslationError("campaign.questions must be a non-empty array")
    info = campaign.get("multiLanguageInfo")
    if not isinstance(info, dict):
        raise TranslationError("campaign.multiLanguageInfo must be an object")
    stored_base = info.get("baseLanguage")
    if stored_base is not None:
        stored_base = registry.normalize(
            require_string(stored_base, "multiLanguageInfo.baseLanguage")
        )
        if stored_base != base_language:
            raise TranslationError(
                f"Base language mismatch: export is {stored_base!r}, request is {base_language!r}"
            )
    additional = info.get("additionalLanguages")
    if not isinstance(additional, list) or any(not isinstance(item, str) for item in additional):
        raise TranslationError("campaign.multiLanguageInfo.additionalLanguages must be a string array")
    translations = campaign.get("translations")
    if not isinstance(translations, list):
        raise TranslationError("campaign.translations must be an array")
    seen_languages: set[str] = set()
    for index, translation in enumerate(translations):
        if not isinstance(translation, dict):
            raise TranslationError(f"campaign.translations[{index}] must be an object")
        language = registry.normalize(
            require_string(translation.get("language"), f"translations[{index}].language")
        )
        if language in seen_languages:
            raise TranslationError(f"Duplicate translation language: {language}")
        seen_languages.add(language)
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise TranslationError(f"campaign.questions[{index}] must be an object")
        require_string(question.get("text"), f"questions[{index}].text")
        choices = question.get("choices")
        if not isinstance(choices, list):
            raise TranslationError(f"questions[{index}].choices must be an array")
        for choice_index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                raise TranslationError(f"questions[{index}].choices[{choice_index}] must be an object")
            require_string(choice.get("text"), f"questions[{index}].choices[{choice_index}].text")
    base_ids = [question.get("id") for question in questions]
    if all(isinstance(question_id, str) and question_id for question_id in base_ids):
        for index, translation in enumerate(translations):
            translated_questions = translation.get("questions")
            if not isinstance(translated_questions, list):
                raise TranslationError(f"translations[{index}].questions must be an array")
            if any(not isinstance(question, dict) for question in translated_questions):
                raise TranslationError(
                    f"translations[{index}].questions entries must be objects"
                )
            translated_ids = [question.get("id") for question in translated_questions]
            if translated_ids != base_ids:
                language = registry.normalize(translation["language"])
                raise TranslationError(
                    f"Translation {language!r} question IDs must match base question IDs "
                    f"in order; expected {base_ids!r}, got {translated_ids!r}"
                )
    return {
        "campaign": campaign,
        "translations_by_language": {
            registry.normalize(translation["language"]): translation
            for translation in translations
        },
    }


def discover_target_languages(
    data: Any, base_language: str, registry: LanguageRegistry
) -> list[str]:
    """Read target languages from export bookkeeping for a bare translate request."""
    source = validate_source(data, base_language, registry)
    campaign = source["campaign"]
    info = campaign["multiLanguageInfo"]
    candidates = list(info["additionalLanguages"])
    candidates.extend(
        translation["language"] for translation in campaign["translations"]
    )
    targets = deduplicate([registry.normalize(candidate) for candidate in candidates])
    targets = [language for language in targets if language != base_language]
    if not targets:
        raise TranslationError(
            "Export contains no target languages in multiLanguageInfo.additionalLanguages "
            "or campaign.translations; specify --target-language"
        )
    return targets


def read_bundle(path: Path, registry: LanguageRegistry) -> dict[str, Any]:
    bundle = read_json(path)
    if not isinstance(bundle, dict):
        raise TranslationError("Translation bundle root must be an object")
    normalized: dict[str, Any] = {}
    for raw_language, payload in bundle.items():
        if not isinstance(raw_language, str):
            raise TranslationError("Translation bundle language keys must be strings")
        language = registry.normalize(raw_language)
        if language in normalized:
            raise TranslationError(f"Duplicate translation bundle language: {language}")
        if not isinstance(payload, dict) or not isinstance(payload.get("questions"), dict):
            raise TranslationError(
                f"Translation bundle entry {language!r} must contain questions object"
            )
        normalized[language] = payload
    return normalized


def question_payload(
    question_map: dict[str, Any], question: dict[str, Any], index: int
) -> dict[str, Any]:
    question_id = question["id"]
    payload = question_map.get(question_id)
    if payload is None:
        payload = question_map.get(str(question.get("number", index + 1)))
    if not isinstance(payload, dict):
        raise TranslationError(
            f"Missing translation bundle entry for question {question_id!r}"
        )
    return payload


def build_translation(
    language: str, questions: list[dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    question_map = payload["questions"]
    translated_questions: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        source_text = question["text"]
        question_id = question["id"]
        localized = question_payload(question_map, question, index)
        translated_text = validate_localized_string(
            source_text, localized.get("text"), f"{language}.questions[{question_id}].text"
        )
        source_choices = question["choices"]
        translated_choices = localized.get("choices")
        if not isinstance(translated_choices, list):
            raise TranslationError(
                f"{language}.questions[{question_id}].choices must be an array"
            )
        if len(translated_choices) != len(source_choices):
            raise TranslationError(
                f"{language}.questions[{question_id}] choice count mismatch: "
                f"expected {len(source_choices)}, got {len(translated_choices)}"
            )
        choice_records: list[dict[str, str]] = []
        for choice_index, (source_choice, translated_choice) in enumerate(
            zip(source_choices, translated_choices)
        ):
            if isinstance(translated_choice, dict):
                translated_choice = translated_choice.get("text")
            choice_text = validate_localized_string(
                source_choice["text"],
                translated_choice,
                f"{language}.questions[{question_id}].choices[{choice_index}].text",
            )
            choice_records.append({"text": choice_text})
        translated_questions.append(
            {
                "id": question_id,
                "comment": None,
                "text": translated_text,
                "choices": choice_records,
            }
        )
    return {"language": language, "status": "VALID", "questions": translated_questions}


def update_export(
    data: dict[str, Any],
    base_language: str,
    target_languages: list[str],
    bundle: dict[str, Any],
    registry: LanguageRegistry,
) -> dict[str, Any]:
    source = validate_source(data, base_language, registry)
    campaign = source["campaign"]
    questions = campaign["questions"]
    translations = campaign["translations"]
    translations_by_language = source["translations_by_language"]
    info = campaign["multiLanguageInfo"]

    unexpected_languages = sorted(set(bundle) - set(target_languages))
    if unexpected_languages:
        raise TranslationError(
            "Translation bundle contains unrequested languages: "
            + ", ".join(unexpected_languages)
        )

    added: list[str] = []
    repaired: list[str] = []
    skipped_existing: list[str] = []
    skipped_base: list[str] = []
    existing_language_positions = {
        registry.normalize(translation["language"]): index
        for index, translation in enumerate(translations)
    }

    for language in target_languages:
        if language == base_language:
            skipped_base.append(language)
            continue
        existing = translations_by_language.get(language)
        if existing is not None and existing.get("status") == "VALID":
            skipped_existing.append(language)
            continue
        if existing is not None and existing.get("status") != "INVALID":
            raise TranslationError(
                f"Existing translation {language!r} has unsupported status "
                f"{existing.get('status')!r}"
            )
        if language not in bundle:
            action = "repair" if existing is not None else "add"
            raise TranslationError(f"Bundle missing language {language!r} required to {action} it")
        translation = build_translation(language, questions, bundle[language])
        if existing is not None:
            translations[existing_language_positions[language]] = translation
            translations_by_language[language] = translation
            repaired.append(language)
        else:
            translations.append(translation)
            translations_by_language[language] = translation
            added.append(language)

    # Keep existing entries verbatim (order and spelling); compare by
    # normalized code so "zh-CN" and "zh" count as the same language.
    additional = info["additionalLanguages"]
    known_codes = {registry.normalize(item) for item in additional}
    for language in added + repaired:
        if language not in known_codes:
            additional.append(language)
            known_codes.add(language)
    if added or repaired:
        campaign["isMultiLanguageEnabled"] = True

    return {
        "added": added,
        "repaired_invalid": repaired,
        "skipped_existing": skipped_existing,
        "skipped_base_language": skipped_base,
    }


def write_json(data: dict[str, Any], path: Path) -> None:
    parent = path.parent
    if not parent.exists():
        raise TranslationError(f"Output directory does not exist: {parent}")
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Nexthink campaign export JSON")
    parser.add_argument("--base-language", help="Base language code or name")
    target_languages = parser.add_mutually_exclusive_group(required=True)
    target_languages.add_argument(
        "--target-language",
        action="append",
        dest="target_languages",
        help="Target language code or name; repeat for multiple languages",
    )
    target_languages.add_argument(
        "--from-export-languages",
        action="store_true",
        help="Use languages declared in additionalLanguages and translations",
    )
    parser.add_argument(
        "--language-config",
        type=Path,
        default=default_language_config_path(),
        help="JSON language allowlist (default: bundled references/languages.json)",
    )
    parser.add_argument(
        "--translations-file",
        type=Path,
        required=True,
        help="JSON bundle containing translated question text and choices",
    )
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output", type=Path, help="Write extended export to this path")
    output.add_argument("--in-place", action="store_true", help="Replace input export atomically")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        registry = load_language_registry(args.language_config)
        data = read_json(args.input)
        if args.base_language is None:
            if not args.from_export_languages:
                raise TranslationError(
                    "--base-language is required with explicit target languages"
                )
            campaign = data.get("campaign") if isinstance(data, dict) else None
            info = campaign.get("multiLanguageInfo") if isinstance(campaign, dict) else None
            stored_base = info.get("baseLanguage") if isinstance(info, dict) else None
            base_language = registry.normalize(
                require_string(stored_base, "multiLanguageInfo.baseLanguage")
            )
        else:
            base_language = registry.normalize(args.base_language)
        if args.from_export_languages:
            target_languages = discover_target_languages(data, base_language, registry)
        else:
            assert args.target_languages is not None
            target_languages = deduplicate(
                [registry.normalize(language) for language in args.target_languages]
            )
        validate_source(data, base_language, registry)
        generated_ids = ensure_base_question_ids(data)
        bundle = read_bundle(args.translations_file, registry)
        report = update_export(data, base_language, target_languages, bundle, registry)
        output_path = args.input if args.in_place else args.output
        assert output_path is not None
        if not args.in_place and output_path.resolve() == args.input.resolve():
            raise TranslationError("Use --in-place when output path equals input path")
        write_json(data, output_path)
        report.update(
            {
                "output": str(output_path),
                "target_languages": target_languages,
                "generated_ids": generated_ids,
            }
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, TranslationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
