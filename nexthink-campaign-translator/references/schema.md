# Nexthink campaign export schema

The supplied exports are JSON objects with exactly two top-level fields:

```json
{
  "exportedAt": "YYYY-MM-DD HH:MM:SS",
  "campaign": {}
}
```

`campaign` contains export metadata and localized question content. The observed fields are:

```text
name: string
description: string
nqlId: string
triggers: string[]
senderTitle: string
senderName: string
senderImage: data:image/jpeg;base64,...
investigationQuery: null
isImageGenerated: boolean
priority: string
isMandatory: boolean
questions: Question[]
isMultiLanguageEnabled: boolean
multiLanguageInfo: LanguageInfo
translations: Translation[]
quietPeriodDuration: null
isParametric: boolean
parameters: Parameter[] | null
```

Base question records contain:

```json
{
  "id": "21-character-id",
  "nqlId": "announcement",
  "type": "SINGLE_ANSWER",
  "number": 1,
  "text": "<rich HTML>",
  "choices": [
    {
      "label": "ok",
      "text": "Ok",
      "value": null,
      "next": ""
    }
  ],
  "comment": "",
  "defaultNext": null
}
```

Translation records are language overlays rather than full question copies:

```json
{
  "language": "en",
  "status": "VALID",
  "questions": [
    {
      "id": "same-as-base-question-id",
      "comment": null,
      "text": "<translated rich HTML>",
      "choices": [
        {"text": "Ok"}
      ]
    }
  ]
}
```

Translation questions omit base `nqlId`, `type`, `number`, and choice behavior fields. Preserve those fields from the base question by leaving the base record untouched. Translation choice order maps to base choice order.

`multiLanguageInfo` has this observed shape:

```json
{
  "defaultLanguage": "en",
  "additionalLanguages": ["en", "fr"],
  "baseLanguage": "de"
}
```

`additionalLanguages` matches translation language codes in the supplied exports, including `defaultLanguage` when it is `en`. Do not rewrite that convention. Add missing target codes once and preserve existing order. The script reconciles this bookkeeping on every run: each `VALID` overlay language is listed in `additionalLanguages` and `isMultiLanguageEnabled` is `true` whenever `translations` is non-empty; each reconciliation is named in the report.

Language spelling rule: a language is spelled the same way in its overlay and in `additionalLanguages`. A repaired overlay keeps its existing `language` string (`zh-CN` stays `zh-CN`, never becomes `zh`) and `additionalLanguages` follows it; a new overlay reuses the spelling already in `additionalLanguages`, else the registry code. Languages already present in an export are matched case-insensitively (`_` equals `-`, aliases resolve to their code) but are never checked against the allowlist; a language that is not on the allowlist can therefore stay in an export but cannot be added or repaired. `INVALID` overlays for languages that were not requested are left in place and reported as `invalid_not_repaired`; overlays kept as they are must contain exactly the base question IDs, in any order.

The supported-language allowlist lives in `languages.json` beside this file (`"version": 1`; other versions are rejected). Each entry defines the export `code`, display `name`, optional input `aliases`, and `enabled` flag. The bundled registry contains Afrikaans, Arabic, Bulgarian, Chinese (simplified), Chinese (traditional), Croatian, Czech, Danish, Dutch, English, Filipino, Finnish, French, German, Greek, Hindi, Hungarian, Indonesian, Irish, Italian, Japanese, Kashmiri, Korean, Norwegian, Persian, Polish, Portuguese, Romanian, Russian, Serbian, Slovak, Spanish, Swedish, Thai, Turkish, Ukrainian, and Vietnamese. Add entries or set `enabled: false` there to change allowed languages. Chinese (simplified) uses the observed legacy export code `zh` and accepts `zh-Hans`/`zh-CN` as aliases; Chinese (traditional) uses `zh-Hant`; Filipino uses `fil`; Norwegian (`no`) accepts `nb` and `nn`. Lookups ignore case, Unicode normalization form, and `_` versus `-`. A requested regional code such as `pt-BR` or `en-US` falls back to its primary subtag when that subtag is enabled and the registry defines no regional variant of it; the fallback is reported under `normalized_targets`.

The translation bundle keys each question by base `id`. When a base question has no `id`, use its `number` as a string (`"1"`); the script generates the ID and reuses it in every overlay. A base question with neither `id` nor `number` is an error. A choice may be a string or a `{"text": "..."}` object. Bundle keys that match no base question, and bundle languages that were not requested, are errors.

Parametric campaigns use parameter records with `id`, `name`, and `description`. In the samples, `announcement_body` and `service_name` are declared but not referenced by text. Treat that as source data; do not create references during translation.

Text values are editor-generated HTML strings. They may contain inline styles, HTML entities, non-breaking spaces, links, empty elements, or loose list items. Dynamic values observed in text are `{user}` and double-braced parameters such as `{{announcement_title_de}}`; preserve exact spelling and braces. Everything except text nodes is non-translatable structure: the script parses source and translation and requires the same sequence of tags (start, end, and self-closing forms), the same attributes, and the same attribute values after HTML-unescaping, so `href` values, inline styles, and comments must match exactly and no tag or attribute may be added, dropped, renamed, or reordered. A literal `<` that could open a tag in translated text is rejected; write `&lt;`.

Observed IDs in `#translation_test` are also 21 characters and fit the URL-safe NanoID alphabet `[A-Za-z0-9_-]`. Existing base question IDs are reused by every translation for that question, including invalid translations. Generate a new ID only for a missing base question ID, then reuse it for all translations. `isMultiLanguageEnabled: true` does not itself name targets: bare translation requests read `additionalLanguages` and then translation record languages. If both are empty, no target can be inferred.

Observed campaign families:

- `announcement_information`: `API`, mandatory, parametric, one valid English translation.
- `announcement_problem`: `API`, optional, parametric, one `INVALID` English translation whose text and choice are empty.
- `didyouknow_chatgpt`: `WORKFLOW`, mandatory, valid `en`, `fr`, `it`, and `pl` translations.
- `didyouknow_deepl`: `WORKFLOW`, mandatory, valid `en`, `fr`, `it`, and `pl` translations with five language links and a KBA link.
- `driver_updates_available`: `REMEDIATION`, optional, valid English translation with two choices.
- `translation_test`: `MANUAL`, mandatory, English base, two questions, and declared `fr`, `de`, and `pl` overlays whose question IDs match base IDs while text and choices are empty with `INVALID` status.
