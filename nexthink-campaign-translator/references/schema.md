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

`additionalLanguages` matches translation language codes in the supplied exports, including `defaultLanguage` when it is `en`. Do not rewrite that convention. Add missing target codes once and preserve existing order.

The supported-language allowlist lives in `languages.json` beside this file. Each entry defines the export `code`, display `name`, optional input `aliases`, and `enabled` flag. The bundled registry contains Afrikaans, Arabic, Bulgarian, Chinese (simplified), Chinese (traditional), Croatian, Czech, Danish, Dutch, English, Filipino, Finnish, French, German, Greek, Hindi, Hungarian, Indonesian, Irish, Italian, Japanese, Kashmiri, Korean, Norwegian, Persian, Polish, Portuguese, Romanian, Russian, Serbian, Slovak, Spanish, Swedish, Thai, Turkish, Ukrainian, and Vietnamese. Add entries or set `enabled: false` there to change allowed languages. Chinese (simplified) uses the observed legacy export code `zh` and accepts `zh-Hans`/`zh-CN` as aliases; Chinese (traditional) uses `zh-Hant`; Filipino uses `fil`.

Parametric campaigns use parameter records with `id`, `name`, and `description`. In the samples, `announcement_body` and `service_name` are declared but not referenced by text. Treat that as source data; do not create references during translation.

Text values are editor-generated HTML strings. They may contain inline styles, HTML entities, non-breaking spaces, links, empty elements, or loose list items. Dynamic values observed in text are `{user}` and double-braced parameters such as `{{announcement_title_de}}`; preserve exact spelling and braces. `href` values are part of the non-translatable structure, including URLs containing dynamic parameters.

Observed IDs in `#translation_test` are also 21 characters and fit the URL-safe NanoID alphabet `[A-Za-z0-9_-]`. Existing base question IDs are reused by every translation for that question, including invalid translations. Generate a new ID only for a missing base question ID, then reuse it for all translations. `isMultiLanguageEnabled: true` does not itself name targets: bare translation requests read `additionalLanguages` and then translation record languages. If both are empty, no target can be inferred.

Observed campaign families:

- `announcement_information`: `API`, mandatory, parametric, one valid English translation.
- `announcement_problem`: `API`, optional, parametric, one `INVALID` English translation whose text and choice are empty.
- `didyouknow_chatgpt`: `WORKFLOW`, mandatory, valid `en`, `fr`, `it`, and `pl` translations.
- `didyouknow_deepl`: `WORKFLOW`, mandatory, valid `en`, `fr`, `it`, and `pl` translations with five language links and a KBA link.
- `driver_updates_available`: `REMEDIATION`, optional, valid English translation with two choices.
- `translation_test`: `MANUAL`, mandatory, English base, two questions, and declared `fr`, `de`, and `pl` overlays whose question IDs match base IDs while text and choices are empty with `INVALID` status.
