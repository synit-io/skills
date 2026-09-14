---
name: nexthink-campaign-translator
description: Add one or more requested language translations to Nexthink campaign export JSON while preserving export structure, rich HTML, links, and template placeholders. Use when a task translates a Nexthink campaign export, adds languages to one, or repairs INVALID translation overlays in one.
---

# Nexthink Campaign Translations

Use this skill when a user asks to translate a Nexthink campaign export or add languages to an existing export.

Read `references/schema.md` before editing an export. Use `scripts/add_translations.py` for structural changes and validation; let the model produce localized text, then pass that text to the script.

Allowed languages come from `references/languages.json`. It is the single configurable allowlist. Add or disable entries there, or pass an alternate file with `--language-config`; keep export codes and aliases in that registry rather than hardcoding them in the workflow.

## Workflow

1. Locate the campaign export JSON. Process `.json` files only; ignore sidecars such as `:Zone.Identifier`. If the request names multiple input files, process each independently. For an explicit request, read one base language and one or more target languages. Normalize names through the configured allowlist (`French` becomes `fr`) and reject disabled or unknown languages. Deduplicate targets while preserving request order. Skip the base language and report it.

   For a bare request such as `translate this campaign`, infer base language from `multiLanguageInfo.baseLanguage` and targets from `multiLanguageInfo.additionalLanguages`, followed by languages present in `campaign.translations`. Deduplicate, remove the base language, and use `--from-export-languages`. This uses only languages declared in the export. If both sources contain no target language, report that explicitly and do not guess.

2. Compare the requested base language with `campaign.multiLanguageInfo.baseLanguage`. If the export has a different base language, stop and ask which source to use. Keep `defaultLanguage` unchanged; it may differ from `baseLanguage` in existing exports.

3. Translate every base question's visible text and every base choice's visible text. Preserve rich HTML tags, attributes, inline styles, `href` values, whitespace that affects rendering, and HTML entities. Translate text nodes and choice labels only. Keep sender metadata, question metadata, choice behavior, links, and embedded images unchanged.

4. Protect every exact token matching `{{...}}` or `{...}` before translating. Put the exact token back in the same semantic location. This includes `{user}` and parameters inside HTML attributes such as `https://{{announcement_link}}`. Never translate, rename, or reformat protected tokens.

5. Build a temporary translation bundle with this shape:

   ```json
   {
     "fr": {
       "questions": {
         "BASE_QUESTION_ID": {
           "text": "<translated rich HTML>",
           "choices": ["Translated choice 1", "Translated choice 2"]
         }
       }
     }
   }
   ```

   Include one question entry per base question, keyed by base question `id`. Keep choice order identical to the base question. Do not put translation metadata, labels, `next`, or `value` fields in this bundle.

6. Run the helper from this skill directory:

   ```bash
   python3 scripts/add_translations.py INPUT.json \
     --base-language de \
     --target-language fr \
     --target-language pl \
     --translations-file /tmp/nexthink-translations.json \
     --output OUTPUT.json
   ```

   For a bare request, omit `--base-language` and replace repeated target options with `--from-export-languages`:

   ```bash
   python3 scripts/add_translations.py INPUT.json \
     --from-export-languages \
     --translations-file /tmp/nexthink-translations.json \
     --output OUTPUT.json
   ```

   Use `--in-place` instead of `--output` only when user explicitly requests modifying the source file.

7. Review helper output. It must report valid JSON, one translation question for every base question, matching question IDs, matching choice counts, preserved brace tokens, preserved `href` values, no duplicate languages, and `status: "VALID"` for added translations. Existing `VALID` translations stay untouched. An existing `INVALID` translation is replaced only when a valid bundle for that language is supplied.

## Export rules

- New records use the observed translation shape: `{language, status, questions}`. Each translated question uses `{id, comment, text, choices}` with `comment: null`; each translated choice uses `{text}`.
- Append new language codes to `campaign.multiLanguageInfo.additionalLanguages` in requested order. Preserve existing order, `defaultLanguage`, `baseLanguage`, `exportedAt`, and all unrelated campaign fields. Keep `isMultiLanguageEnabled` true when translations exist.
- Match translated questions by `id`, not only array position. Translation question IDs repeat the corresponding base question ID in every provided export.
- Existing question IDs are 21-character NanoID-shaped URL-safe strings using letters, digits, `_`, and `-`. Preserve existing IDs. If a base question has no ID, generate one fresh ID with that shape, ensure document-wide uniqueness, and reuse it across all language translations.
- There is no translation-level ID in the observed format. Do not invent separate per-language question IDs. If user explicitly requires separate IDs, ask because that conflicts with all supplied exports.
- Parameters may be declared but unused. Do not invent parameter references or translate parameter names. Flag unused declarations in the final report.
- Preserve editor HTML as authored. Some samples contain loose `<li>` elements, empty containers, `font-style:undefined`, and empty anchors; do not silently normalize these while adding translations.

## Testing

Run regression tests after changing the script or `references/languages.json`:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/add_translations.py
```

## Completion

Finish only after output parses as JSON and helper validation passes. Report output path, languages added, languages skipped because already present, repaired invalid languages, generated IDs, and any unresolved translation or source-content issue.
