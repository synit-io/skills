#!/usr/bin/env python3
"""Extend a Nexthink campaign export with validated translation overlays.

The model supplies translated HTML and choice text through a small JSON bundle;
this script owns IDs, export shape, language bookkeeping, and invariants that
are easy to break during manual JSON editing.

Exit codes: 0 success, 2 validation or usage error, 1 unexpected internal error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


NANOID_ALPHABET = "_-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
TOKEN_PATTERN = re.compile(r"\{\{[^{}]*\}\}|\{[^{}]*\}")
# A "<" followed by one of these starts a tag, end tag, comment, or declaration
# under HTML tokenization; any other "<" is plain text.
TAG_OPENER_PATTERN = re.compile(r"<[A-Za-z/!?]")
LANGUAGE_CODE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
SUPPORTED_CONFIG_VERSIONS = (1,)
STDIN_MARKER = "-"

HtmlAttributes = tuple[tuple[str, str | None], ...]
HtmlEvent = tuple[str, str, HtmlAttributes]


class TranslationError(ValueError):
    """Raised for an invalid export or translation bundle."""


def language_key(value: str) -> str:
    """Return the lookup key for a language label.

    Keys ignore case, Unicode composition (NFC vs NFD), surrounding or repeated
    whitespace, and the `_` vs `-` separator.
    """
    text = unicodedata.normalize("NFC", value).strip().casefold().replace("_", "-")
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", text))


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

    def _find(self, key: str) -> tuple[LanguageDefinition | None, bool]:
        """Look a key up; an unknown `xx-YY` falls back to `xx` when the registry
        defines no regional variant of `xx`. Returns `(definition, used_fallback)`."""
        definition = self._lookup.get(key)
        if definition is not None:
            return definition, False
        primary, separator, _ = key.partition("-")
        if not separator or not LANGUAGE_CODE_PATTERN.fullmatch(key):
            return None, False
        if any(known.startswith(f"{primary}-") for known in self._lookup):
            return None, False
        definition = self._lookup.get(primary)
        return definition, definition is not None

    def resolve(self, value: str) -> tuple[str, bool]:
        """Return `(code, used_regional_fallback)` for a requested language.

        Requested languages must be on the allowlist and enabled.
        """
        if not isinstance(value, str):
            raise TranslationError(f"Language must be a string, got {type(value).__name__}")
        definition, used_fallback = self._find(language_key(value))
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
        return definition.code, used_fallback

    def normalize(self, value: str) -> str:
        return self.resolve(value)[0]

    def match_key(self, value: str) -> str:
        """Return a comparison key for a language that already exists in an export.

        Existing codes are never rejected: a known label maps to its export code
        (enabled or not) and anything else to its normalized spelling.
        """
        key = language_key(value)
        definition, _ = self._find(key)
        return definition.code if definition is not None else key


def load_language_registry(path: Path) -> LanguageRegistry:
    raw = read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("languages"), list):
        raise TranslationError(f"{path} must contain a languages array")
    version = raw.get("version", SUPPORTED_CONFIG_VERSIONS[0])
    if type(version) is not int or version not in SUPPORTED_CONFIG_VERSIONS:
        raise TranslationError(
            f"{path}: unsupported version {version!r}; "
            f"supported: {', '.join(map(str, SUPPORTED_CONFIG_VERSIONS))}"
        )
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


@dataclass(frozen=True)
class JsonStyle:
    """Serialization style; defaults apply when the input style is not detectable."""

    indent: str | None = "  "
    item_separator: str = ","
    key_separator: str = ": "
    ensure_ascii: bool = False
    trailing_newline: bool = True
    newline: str = "\n"


def detect_json_style(text: str) -> JsonStyle:
    """Detect indentation, separators, ASCII escaping, and newline conventions."""
    indent_match = re.match(r"\s*[\[{]\r?\n([ \t]*)", text)
    indent = indent_match.group(1) if indent_match else None
    key_match = re.match(r'\s*\{\s*"(?:[^"\\]|\\.)*"\s*:( ?)', text)
    spaced = key_match is None or key_match.group(1) == " "
    return JsonStyle(
        indent=indent,
        item_separator="," if indent is not None or not spaced else ", ",
        key_separator=": " if spaced else ":",
        ensure_ascii=text.isascii() and re.search(r"\\u[0-9a-fA-F]{4}", text) is not None,
        trailing_newline=text.endswith("\n"),
        newline="\r\n" if "\r\n" in text else "\n",
    )


def decode_json(raw: bytes, label: str) -> tuple[Any, str]:
    """Decode UTF-8 JSON bytes; a leading BOM is accepted and dropped."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise TranslationError(f"{label} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text), text
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Invalid JSON in {label}: {exc}") from exc


def read_json_text(path: Path) -> tuple[Any, str]:
    try:
        if str(path) == STDIN_MARKER:
            return decode_json(sys.stdin.buffer.read(), "standard input")
        return decode_json(path.read_bytes(), str(path))
    except OSError as exc:
        raise TranslationError(f"Cannot read {path}: {exc}") from exc


def read_json(path: Path) -> Any:
    return read_json_text(path)[0]


def read_document(path: Path) -> tuple[Any, JsonStyle]:
    data, text = read_json_text(path)
    return data, detect_json_style(text)


def collect_ids(data: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    campaign = data.get("campaign")
    if not isinstance(campaign, dict):
        return ids
    records = [campaign]
    translations = campaign.get("translations")
    if isinstance(translations, list):
        records.extend(item for item in translations if isinstance(item, dict))
    for record in records:
        questions = record.get("questions")
        if not isinstance(questions, list):
            continue
        for question in questions:
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

    A base question without an ID leaves the matching overlay entries without
    one as well, so array position is the only available mapping. Entries that
    already carry a different ID are left alone; the later overlay ID check
    reports that mismatch instead of silently overwriting.
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
            if question.get("number") is None:
                raise TranslationError(
                    f"campaign.questions[{index}] has neither id nor number; "
                    "the translation bundle cannot address it"
                )
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


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise TranslationError(f"{location} must be a string")
    return value


class _StructureParser(HTMLParser):
    """Record everything in an HTML fragment except its translatable text nodes.

    Character references are left unconverted so that a literal `<` in text can
    be told apart from `&lt;`; attribute values are unescaped by the parser.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.events: list[HtmlEvent] = []
        # Raw text between structural events, one string per gap.
        self.text_segments: list[str] = [""]

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> HtmlAttributes:
        return tuple(
            sorted(attrs, key=lambda item: (item[0], item[1] is not None, item[1] or ""))
        )

    def _event(self, event: HtmlEvent) -> None:
        self.events.append(event)
        self.text_segments.append("")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._event(("start", tag, self._attributes(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._event(("startend", tag, self._attributes(attrs)))

    def handle_endtag(self, tag: str) -> None:
        self._event(("end", tag, ()))

    def handle_data(self, data: str) -> None:
        if getattr(self, "cdata_elem", None):
            # Script and style bodies are code, not translatable text.
            if self.events and self.events[-1][0] == "raw text":
                self.events[-1] = ("raw text", self.events[-1][1] + data, ())
            else:
                self._event(("raw text", data, ()))
        else:
            self.text_segments[-1] += data

    def handle_comment(self, data: str) -> None:
        self._event(("comment", data, ()))

    def handle_decl(self, decl: str) -> None:
        self._event(("declaration", decl, ()))

    def unknown_decl(self, data: str) -> None:
        self._event(("declaration", data, ()))

    def handle_pi(self, data: str) -> None:
        self._event(("processing instruction", data, ()))


def parse_html(value: str) -> _StructureParser:
    parser = _StructureParser()
    parser.feed(value)
    parser.close()
    return parser


def html_structure(value: str) -> list[HtmlEvent]:
    return parse_html(value).events


def describe_html_event(event: HtmlEvent) -> str:
    kind, name, _ = event
    if kind == "start":
        return f"<{name}>"
    if kind == "end":
        return f"</{name}>"
    if kind == "startend":
        return f"<{name}/>"
    return f"{kind} {name!r}"


def describe_attribute_difference(expected: HtmlAttributes, actual: HtmlAttributes) -> str:
    expected_values = dict(expected)
    actual_values = dict(actual)
    for name in expected_values:
        if name not in actual_values:
            return f"missing attribute {name!r}"
    for name in actual_values:
        if name not in expected_values:
            return f"unexpected attribute {name!r}"
    for name, value in expected_values.items():
        if actual_values[name] != value:
            return f"attribute {name!r} expected {value!r}, got {actual_values[name]!r}"
    return f"expected attributes {list(expected)!r}, got {list(actual)!r}"


def validate_html_structure(source_text: str, target_text: str, location: str) -> None:
    """Require identical tags, tag order, attributes, and attribute values.

    Only text nodes may differ between a source string and its translation.
    """
    source_events = html_structure(source_text)
    target = parse_html(target_text)
    target_events = target.events
    for position, (expected, actual) in enumerate(zip(source_events, target_events), start=1):
        if expected == actual:
            continue
        if expected[:2] == actual[:2]:
            raise TranslationError(
                f"{location} changes attributes of {describe_html_event(expected)} "
                f"(tag {position}): {describe_attribute_difference(expected[2], actual[2])}"
            )
        raise TranslationError(
            f"{location} changes HTML structure at tag {position}: "
            f"expected {describe_html_event(expected)}, got {describe_html_event(actual)}"
        )
    if len(target_events) > len(source_events):
        extra = target_events[len(source_events)]
        raise TranslationError(
            f"{location} adds HTML that the source does not have: "
            f"unexpected {describe_html_event(extra)} at tag {len(source_events) + 1}"
        )
    if len(source_events) > len(target_events):
        missing = source_events[len(target_events)]
        raise TranslationError(
            f"{location} drops source HTML: "
            f"missing {describe_html_event(missing)} at tag {len(target_events) + 1}"
        )
    # An unterminated "<tag ..." is parsed as text here but can still open a
    # tag once the string is embedded in a page.
    for segment in target.text_segments:
        match = TAG_OPENER_PATTERN.search(segment)
        if match:
            raise TranslationError(
                f"{location} contains a raw {match.group(0)!r} in text that could "
                "open a tag; write '<' as '&lt;'"
            )


def validate_localized_string(source: Any, target: Any, location: str) -> str:
    source_text = require_string(source, f"source {location}")
    target_text = require_string(target, location)
    if not source_text.strip():
        # Nothing to translate: the overlay mirrors the empty source.
        if target_text.strip():
            raise TranslationError(f"{location} must be empty because the source is empty")
        return target_text
    if not target_text.strip():
        raise TranslationError(f"{location} must not be empty")
    source_tokens = tokens(source_text)
    target_tokens = tokens(target_text)
    if source_tokens != target_tokens:
        raise TranslationError(
            f"{location} changes protected brace tokens: "
            f"expected {dict(source_tokens)!r}, got {dict(target_tokens)!r}"
        )
    validate_html_structure(source_text, target_text, location)
    return target_text


@dataclass
class Source:
    """Validated view of an export, built once and shared by the later steps."""

    data: dict[str, Any]
    campaign: dict[str, Any]
    questions: list[dict[str, Any]]
    translations: list[dict[str, Any]]
    info: dict[str, Any]
    # Registry match key -> position in campaign.translations.
    overlays: dict[str, int]


def validate_source(data: Any, registry: LanguageRegistry) -> Source:
    """Check the export shape.

    Languages already present in the export are matched leniently and never
    checked against the allowlist; only requested targets are.
    """
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
    if info.get("baseLanguage") is not None:
        require_string(info["baseLanguage"], "multiLanguageInfo.baseLanguage")
    additional = info.get("additionalLanguages")
    if not isinstance(additional, list) or any(not isinstance(item, str) for item in additional):
        raise TranslationError("campaign.multiLanguageInfo.additionalLanguages must be a string array")
    translations = campaign.get("translations")
    if not isinstance(translations, list):
        raise TranslationError("campaign.translations must be an array")
    overlays: dict[str, int] = {}
    for index, translation in enumerate(translations):
        if not isinstance(translation, dict):
            raise TranslationError(f"campaign.translations[{index}] must be an object")
        language = require_string(translation.get("language"), f"translations[{index}].language")
        key = registry.match_key(language)
        if key in overlays:
            raise TranslationError(f"Duplicate translation language: {language}")
        overlays[key] = index
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
    return Source(data, campaign, questions, translations, info, overlays)


def check_kept_overlays(source: Source) -> None:
    """Require every overlay that stays in the export to cover the base questions.

    INVALID overlays are skipped: they are rebuilt when targeted and reported as
    `invalid_not_repaired` otherwise, so a stale one never blocks a run. IDs are
    compared as a set because overlays map to base questions by ID, not position.
    """
    base_ids = sorted(str(question.get("id")) for question in source.questions)
    for index, translation in enumerate(source.translations):
        if translation.get("status") == "INVALID":
            continue
        translated_questions = translation.get("questions")
        if not isinstance(translated_questions, list):
            raise TranslationError(f"translations[{index}].questions must be an array")
        if any(not isinstance(question, dict) for question in translated_questions):
            raise TranslationError(f"translations[{index}].questions entries must be objects")
        translated_ids = [question.get("id") for question in translated_questions]
        if sorted(map(str, translated_ids)) != base_ids:
            raise TranslationError(
                f"Translation {translation['language']!r} question IDs must match base "
                f"question IDs; expected {base_ids!r}, got {translated_ids!r}. "
                "Set its status to INVALID and target the language to rebuild it"
            )


def resolve_base_language(
    requested: str | None, source: Source, registry: LanguageRegistry
) -> str:
    stored = source.info.get("baseLanguage")
    if requested is None:
        return registry.match_key(
            require_string(stored, "multiLanguageInfo.baseLanguage")
        )
    if stored is None:
        return registry.normalize(requested)
    stored_key = registry.match_key(stored)
    if language_key(requested) == language_key(stored):
        return stored_key
    try:
        requested_key = registry.normalize(requested)
    except TranslationError:
        requested_key = registry.match_key(requested)
    if requested_key != stored_key:
        raise TranslationError(
            f"Base language mismatch: export is {stored!r}, request is {requested!r}"
        )
    return stored_key


def resolve_target_languages(
    requested: list[str], registry: LanguageRegistry
) -> tuple[list[str], dict[str, str]]:
    """Normalize requested targets; also return regional fallbacks such as pt-BR -> pt."""
    targets: list[str] = []
    fallbacks: dict[str, str] = {}
    for label in requested:
        code, used_fallback = registry.resolve(label)
        if used_fallback:
            fallbacks[label] = code
        targets.append(code)
    return list(dict.fromkeys(targets)), fallbacks


def discover_target_languages(
    source: Source, base_language: str, registry: LanguageRegistry
) -> list[str]:
    """Read target languages from export bookkeeping for a bare translate request."""
    candidates = list(source.info["additionalLanguages"])
    candidates.extend(translation["language"] for translation in source.translations)
    targets = list(dict.fromkeys(registry.match_key(candidate) for candidate in candidates))
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
    question_map: dict[str, Any], question: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Return `(bundle key, entry)` for a base question.

    Entries are keyed by question `id`; the question `number` is the fallback
    for questions whose ID was only generated during this run.
    """
    question_id = question["id"]
    number = question.get("number")
    keys = [question_id] if number is None else [question_id, str(number)]
    for key in keys:
        payload = question_map.get(key)
        if payload is not None:
            if not isinstance(payload, dict):
                raise TranslationError(f"Translation bundle entry {key!r} must be an object")
            return key, payload
    raise TranslationError(
        f"Missing translation bundle entry for question {question_id!r}"
        + ("" if number is None else f" (number {number})")
    )


def build_translation(
    language: str,
    questions: list[dict[str, Any]],
    payload: dict[str, Any],
    spelling: str | None = None,
) -> dict[str, Any]:
    """Build a VALID overlay; `spelling` overrides the language string written."""
    question_map = payload["questions"]
    consumed: set[str] = set()
    translated_questions: list[dict[str, Any]] = []
    for question in questions:
        source_text = question["text"]
        question_id = question["id"]
        key, localized = question_payload(question_map, question)
        consumed.add(key)
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
    unused = sorted(set(question_map) - consumed)
    if unused:
        valid = ", ".join(
            repr(question["id"])
            + ("" if question.get("number") is None else f" (or {str(question['number'])!r})")
            for question in questions
        )
        raise TranslationError(
            f"Translation bundle entry {language!r} has question keys that match no "
            f"base question: {', '.join(map(repr, unused))}. Valid keys: {valid}"
        )
    return {
        "language": spelling or language,
        "status": "VALID",
        "questions": translated_questions,
    }


def update_export(
    source: Source,
    base_language: str,
    target_languages: list[str],
    bundle: dict[str, Any],
    registry: LanguageRegistry,
) -> dict[str, Any]:
    campaign = source.campaign
    translations = source.translations
    additional = source.info["additionalLanguages"]

    unexpected_languages = sorted(set(bundle) - set(target_languages))
    if unexpected_languages:
        raise TranslationError(
            "Translation bundle contains unrequested languages: "
            + ", ".join(unexpected_languages)
        )
    check_kept_overlays(source)

    had_translations = bool(translations)
    additional_positions: dict[str, int] = {}
    for position, item in enumerate(additional):
        additional_positions.setdefault(registry.match_key(item), position)

    added: list[str] = []
    repaired: list[str] = []
    skipped_existing: list[str] = []
    skipped_base: list[str] = []

    for language in target_languages:
        if language == base_language:
            skipped_base.append(language)
            continue
        position = source.overlays.get(language)
        existing = translations[position] if position is not None else None
        if existing is not None and existing.get("status") == "VALID":
            skipped_existing.append(language)
            continue
        if existing is not None and existing.get("status") != "INVALID":
            raise TranslationError(
                f"Existing translation {language!r} has unsupported status "
                f"{existing.get('status')!r}"
            )
        # Anything written must be an enabled allowlist code, including targets
        # discovered from the export itself.
        code = registry.normalize(language)
        if code not in bundle:
            action = "repair" if existing is not None else "add"
            raise TranslationError(f"Bundle missing language {code!r} required to {action} it")
        # One spelling per language: keep the export's own spelling when it has
        # one (the overlay first, then additionalLanguages), else the registry code.
        if existing is not None:
            spelling = existing["language"]
        elif code in additional_positions:
            spelling = additional[additional_positions[code]]
        else:
            spelling = code
        translation = build_translation(code, source.questions, bundle[code], spelling)
        if position is not None:
            translations[position] = translation
            repaired.append(code)
        else:
            source.overlays[code] = len(translations)
            translations.append(translation)
            added.append(code)

    # Reconcile bookkeeping: every VALID overlay is listed in additionalLanguages
    # under the same spelling, and the multi-language flag is set.
    touched = set(added) | set(repaired)
    reconciled: list[str] = []
    warnings: list[str] = []
    invalid_not_repaired: list[str] = []
    for translation in translations:
        spelling = translation["language"]
        status = translation.get("status")
        if status == "INVALID":
            invalid_not_repaired.append(spelling)
        if status != "VALID":
            continue
        key = registry.match_key(spelling)
        position = additional_positions.get(key)
        if position is None:
            additional_positions[key] = len(additional)
            additional.append(spelling)
            if key not in touched:
                reconciled.append(
                    f"added {spelling!r} to multiLanguageInfo.additionalLanguages"
                )
        elif additional[position] != spelling:
            if key in touched:
                reconciled.append(
                    f"respelled additionalLanguages entry {additional[position]!r} "
                    f"as {spelling!r} to match its translation"
                )
                additional[position] = spelling
            else:
                warnings.append(
                    f"additionalLanguages spells {additional[position]!r} but its "
                    f"translation uses {spelling!r}; left unchanged"
                )
    if translations and campaign.get("isMultiLanguageEnabled") is not True:
        campaign["isMultiLanguageEnabled"] = True
        if had_translations:
            reconciled.append("set isMultiLanguageEnabled to true")

    return {
        "added": added,
        "repaired_invalid": repaired,
        "skipped_existing": skipped_existing,
        "skipped_base_language": skipped_base,
        "invalid_not_repaired": invalid_not_repaired,
        "reconciled": reconciled,
        "warnings": warnings,
    }


def serialize_json(data: Any, style: JsonStyle) -> bytes:
    text = json.dumps(
        data,
        ensure_ascii=style.ensure_ascii,
        indent=style.indent,
        separators=(style.item_separator, style.key_separator),
    )
    if style.trailing_newline:
        text += "\n"
    if style.newline != "\n":
        text = text.replace("\n", style.newline)
    try:
        return text.encode("utf-8")
    except UnicodeError as exc:
        raise TranslationError(
            f"Output cannot be encoded as UTF-8 (lone surrogate in the data?): {exc}"
        ) from exc


def current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def write_json(
    data: Any, path: Path, style: JsonStyle = JsonStyle(), *, backup: bool = False
) -> Path | None:
    """Atomically write `data` as UTF-8 without a BOM; return the backup path, if any.

    Symlinks are followed so the real file is replaced, an existing file keeps
    its permission bits, and `backup` first copies it to `<name>.bak`.
    """
    payload = serialize_json(data, style)
    target = path.resolve()
    parent = target.parent
    if not parent.is_dir():
        raise TranslationError(f"Output directory does not exist: {parent}")
    backup_path: Path | None = None
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            shutil.copymode(target, temporary_path)
            if backup:
                backup_path = target.with_name(target.name + ".bak")
                shutil.copy2(target, backup_path)
        else:
            os.chmod(temporary_path, 0o666 & ~current_umask())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    return backup_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        help="JSON bundle containing translated question text and choices; "
        f"'{STDIN_MARKER}' reads it from standard input",
    )
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output", type=Path, help="Write extended export to this path")
    output.add_argument(
        "--in-place",
        action="store_true",
        help="Replace input export atomically, keeping the original as <input>.bak",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="With --in-place, do not keep the <input>.bak copy",
    )
    args = parser.parse_args()
    if args.no_backup and not args.in_place:
        parser.error("--no-backup requires --in-place")
    return args


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.base_language is None and not args.from_export_languages:
        raise TranslationError("--base-language is required with explicit target languages")
    if not args.in_place and args.output.resolve() == args.input.resolve():
        raise TranslationError("Use --in-place when output path equals input path")
    registry = load_language_registry(args.language_config)
    data, style = read_document(args.input)
    source = validate_source(data, registry)
    base_language = resolve_base_language(args.base_language, source, registry)
    normalized_targets: dict[str, str] = {}
    if args.from_export_languages:
        target_languages = discover_target_languages(source, base_language, registry)
    else:
        if not args.target_languages:
            raise TranslationError("Specify --target-language or --from-export-languages")
        target_languages, normalized_targets = resolve_target_languages(
            args.target_languages, registry
        )
    generated_ids = ensure_base_question_ids(data)
    bundle = read_bundle(args.translations_file, registry)
    report = update_export(source, base_language, target_languages, bundle, registry)

    # An unchanged export is not rewritten, so a no-op run leaves the input
    # byte-identical and creates neither an output file nor a backup.
    changed = bool(
        report["added"] or report["repaired_invalid"] or report["reconciled"] or generated_ids
    )
    output_path: Path | None = None
    backup_path: Path | None = None
    if changed:
        output_path = args.input if args.in_place else args.output
        backup_path = write_json(
            data, output_path, style, backup=args.in_place and not args.no_backup
        )
    report.update(
        {
            "changed": changed,
            "output": None if output_path is None else str(output_path),
            "backup": None if backup_path is None else str(backup_path),
            "target_languages": target_languages,
            "normalized_targets": normalized_targets,
            "generated_ids": generated_ids,
        }
    )
    return report


def main() -> int:
    args = parse_args()
    try:
        report = run(args)
    except (OSError, UnicodeError, TranslationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Last resort: never show a traceback to the caller.
        print(f"error: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
