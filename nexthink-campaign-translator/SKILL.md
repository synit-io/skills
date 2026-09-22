---
name: nexthink-campaign-translator
description: Add one or more requested language translations to Nexthink campaign export JSON while preserving export structure, rich HTML, links, and template placeholders. Use when a task translates a Nexthink campaign export, adds languages to one, or repairs INVALID translation overlays in one.
license: MIT
---

# Nexthink Campaign Translations

Use this skill when a user asks to translate a Nexthink campaign export or add languages to an existing export.

`<skill-dir>` below is the directory containing this SKILL.md. Read `<skill-dir>/references/schema.md` before editing an export. Use `<skill-dir>/scripts/add_translations.py` for structural changes and validation; let the model produce localized text, then pass that text to the script. Run the script from the user's working directory, by path; it finds its own `references/languages.json`. Keep campaign exports, bundles, and outputs outside `<skill-dir>`: exports contain sender names and embedded images.

Allowed languages come from `<skill-dir>/references/languages.json`. It is the single configurable allowlist. Add or disable entries there, or pass an alternate file with `--language-config`; keep export codes and aliases in that registry rather than hardcoding them in the workflow. The allowlist applies to requested targets only; languages already present in an export are kept as they are.

## Workflow

1. Locate the campaign export JSON. Process `.json` files only; ignore sidecars such as `:Zone.Identifier`. If the request names multiple input files, process each independently. For an explicit request, read one base language and one or more target languages. Normalize names through the configured allowlist (`French` becomes `fr`; an unknown regional code such as `pt-BR` falls back to `pt` when the registry defines no `pt-` variant, reported under `normalized_targets`) and reject disabled or unknown languages. Deduplicate targets while preserving request order. Skip the base language and report it.

   For a bare request such as `translate this campaign`, infer base language from `multiLanguageInfo.baseLanguage` and targets from `multiLanguageInfo.additionalLanguages`, followed by languages present in `campaign.translations`. Deduplicate, remove the base language, and use `--from-export-languages`. This uses only languages declared in the export. If both sources contain no target language, report that explicitly and do not guess.

2. Compare the requested base language with `campaign.multiLanguageInfo.baseLanguage`. If the export has a different base language, stop and ask which source to use. Keep `defaultLanguage` unchanged; it may differ from `baseLanguage` in existing exports.

3. Translate every base question's visible text and every base choice's visible text. Change text nodes only: the script requires every tag, its order, its attributes, and every attribute value (including `href` and inline styles) to be identical to the source, HTML entities and all. A literal `<` in translated text must be written as `&lt;`. Keep sender metadata, question metadata, choice behavior, links, and embedded images unchanged. An empty source string stays empty.

4. Protect every exact token matching `{{...}}` or `{...}` before translating. Put the exact token back in the same semantic location. This includes `{user}` and parameters inside HTML attributes such as `https://{{announcement_link}}`. Never translate, rename, or reformat protected tokens.

5. Write a translation bundle next to the input file, for example `<input-stem>.translations.json`, with this shape:

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

   Include one question entry per base question, keyed by base question `id`; when a base question has no `id`, key it by its `number` as a string instead (the script generates the ID). Each choice may also be written as `{"text": "..."}`. Keep choice order identical to the base question. Do not put translation metadata, labels, `next`, or `value` fields in this bundle; the script rejects languages that were not requested and question keys that match no base question. Delete the bundle after a successful run, or pass `--translations-file -` to feed it through standard input instead.

6. Run the helper from the user's working directory:

   ```bash
   python3 <skill-dir>/scripts/add_translations.py campaign.json \
     --base-language de \
     --target-language fr \
     --target-language pl \
     --translations-file campaign.translations.json \
     --output campaign.translated.json
   ```

   For a bare request, omit `--base-language` and replace repeated target options with `--from-export-languages`:

   ```bash
   python3 <skill-dir>/scripts/add_translations.py campaign.json \
     --from-export-languages \
     --translations-file campaign.translations.json \
     --output campaign.translated.json
   ```

   Use `--in-place` instead of `--output` only when the user explicitly requests modifying the source file. It replaces the real file behind a symlink, keeps the file mode, and first copies the original to `<input>.bak` unless `--no-backup` is given.

7. Review the helper output. Exit code 0 prints a JSON report to stdout; exit code 2 prints `error: ...` to stderr for any usage, encoding, or validation failure (nothing is written); exit code 1 with `error: unexpected ...` is an internal error worth reporting. The report contains `added`, `repaired_invalid`, `skipped_existing` (VALID overlays left untouched), `skipped_base_language`, `invalid_not_repaired` (INVALID overlays for languages that were not requested), `reconciled` (bookkeeping fixes made), `warnings`, `changed`, `output` (null when nothing changed and no file was written), `backup`, `target_languages`, `normalized_targets`, and `generated_ids`. The invariants (one translation question per base question, matching IDs, matching choice counts, identical HTML structure and attributes, preserved brace tokens, no duplicate languages, `status: "VALID"` on written overlays) are enforced by failing, not reported.

## Export rules

- New records use the observed translation shape: `{language, status, questions}`. Each translated question uses `{id, comment, text, choices}` with `comment: null`; each translated choice uses `{text}`.
- Append new language codes to `campaign.multiLanguageInfo.additionalLanguages` in requested order. Preserve existing order, `defaultLanguage`, `baseLanguage`, `exportedAt`, and all unrelated campaign fields. The script keeps `isMultiLanguageEnabled` true and lists every VALID overlay in `additionalLanguages`; the language spelling in an overlay and in `additionalLanguages` is always the same (a repaired overlay keeps its own spelling).
- Match translated questions by `id`, not array position. Translation question IDs repeat the corresponding base question ID in every provided export.
- Existing question IDs are 21-character NanoID-shaped URL-safe strings using letters, digits, `_`, and `-`. Preserve existing IDs. If a base question has no ID, generate one fresh ID with that shape, ensure document-wide uniqueness, and reuse it across all language translations.
- There is no translation-level ID in the observed format. Do not invent separate per-language question IDs. If user explicitly requires separate IDs, ask because that conflicts with all supplied exports.
- Parameters may be declared but unused. Do not invent parameter references or translate parameter names. Flag unused declarations in the final report.
- Preserve editor HTML as authored. Some samples contain loose `<li>` elements, empty containers, `font-style:undefined`, and empty anchors; do not silently normalize these while adding translations.
- The script reads UTF-8 (a byte-order mark is accepted and not written back) and writes UTF-8, keeping the input's indentation, ASCII escaping, and trailing newline. An unchanged export is not rewritten.

## Testing

Run regression tests after changing the script or `references/languages.json`:

```bash
cd <skill-dir>
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/add_translations.py
```

## Completion

Finish only after the helper exits 0 and, when `changed` is true, the output parses as JSON. Report output path (or that nothing changed), languages added, languages skipped because already present, repaired invalid languages, invalid overlays left unrepaired, reconciliations, generated IDs, and any unresolved translation or source-content issue.
