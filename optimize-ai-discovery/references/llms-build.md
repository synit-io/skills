# Build and maintain llms.txt

## Contents

- Choose static, build-time, or dynamic delivery
- Curate the source manifest
- Run the tools
- Understand lint results
- Integrate with a production application
- Discoverability and Markdown equivalents
- Validate changes and rollback
- Sources

## Choose static, build-time, or dynamic delivery

| Pattern | Choose it when | Update mechanism |
| --- | --- | --- |
| Hand-maintained static file | A small site has few stable authoritative links | Review and replace the text with the site release |
| Generated static file | A CMS or repository already owns the content | Export approved fields and run the deterministic build during publication |
| Dynamic endpoint | A large or frequently updated catalog needs runtime routing | Read a curated data source, render deterministically, and cache by content version |

Prefer generated static delivery unless runtime behavior has a clear purpose. Keep current prices, stock, and detailed specifications on their maintained destinations. A dynamic index does not make crawlers refresh on demand, change trained model knowledge, or guarantee citations.

Keep HTML, Markdown, structured product data, and merchant feeds consistent by generating them from the same reviewed product records where practical. Do not run an LLM on each public request. Use an LLM during drafting to organize known facts, then validate the resulting source data.

## Curate the source manifest

Use `assets/site-manifest.example.json` as a fictional demonstration, and `assets/site-manifest.schema.json` as the builder's data contract. This schema belongs to this toolkit; it is not an industry standard for llms.txt.

Required fields:

- `schema_version`: integer `1`.
- `name`: the site or project name as one line of plain text.

Optional fields:

- `summary`: one paragraph identifying the business and subject matter.
- `guidance`: an array of one-line paragraphs about source selection, interpretation, currency, version, limitations, or attribution.
- `sections`: an array of `{title, links}` objects. Each section has a distinct title and at least one `{title, url, description?}` link.
- `reviewed_on`: a real `YYYY-MM-DD` index review date supplied by the reviewer. Never automatically advance it on every build.

Use existing absolute HTTP(S) destinations. Approved external sources are permitted. Validate external authority as part of content analysis. Do not invent `.md` versions of pages that are not actually served. Prefer public sources that answer real customer questions. Product facts do not belong in an index until their supporting pages exist and are accessible.

The Python validator rejects unknown keys, duplicate destinations or section names, credentials in URLs, unsafe schemes, malformed URLs, line/control-character injection in text fields, duplicate JSON keys, and obvious unresolved template markers. Text is treated as plain text and escaped for Markdown. The default also rejects reserved example/test domains; `--allow-example` deliberately permits them for demonstrations. Validation does not establish factual accuracy or fetch any URL.

The tool reads at most 4 MiB of manifest input. This is an operational limit of the reference implementation, not a constraint of the llms.txt proposal. The JSON Schema covers structure; the Python validator performs additional semantic checks.

## Run the tools

Run these commands from the installed or extracted skill directory. Use Python 3.10 or later; no third-party Python packages are required.

Validate the included demonstration:

```bash
python3 scripts/llms_toolkit.py validate-manifest --manifest assets/site-manifest.example.json --allow-example --json
```

Generate a demonstration file:

```bash
python3 scripts/llms_toolkit.py build --manifest assets/site-manifest.example.json --allow-example --output output/llms.txt
python3 scripts/llms_toolkit.py lint --file output/llms.txt --json
```

The example index is fictional and unsuitable for deployment. Create a site-specific reviewed manifest and omit `--allow-example` for a real build:

```bash
python3 scripts/llms_toolkit.py validate-manifest --manifest path/to/site-manifest.json --json
python3 scripts/llms_toolkit.py build --manifest path/to/site-manifest.json --output path/to/public/llms.txt
```

The builder does not overwrite an existing output by default. Use `--force` only after reviewing the existing content and intended diff. An output that resolves to the input manifest, or is a hard-link alias of it, is rejected even with `--force`. Writes use a same-directory temporary file and an atomic create or replacement. The filesystem must support hard links for atomic no-clobber creation. It creates a missing output parent directory when explicitly given that output path.

Start a local dynamic demonstration where local sockets are permitted:

```bash
python3 scripts/llms_toolkit.py serve --manifest assets/site-manifest.example.json --allow-example --host 127.0.0.1 --port 8765
```

Visit `http://127.0.0.1:8765/llms.txt`. For a scoped endpoint, add `--path /docs/llms.txt`. Edit the manifest and request the endpoint again to see deterministic updates. Stop with Ctrl-C.

The reference server:

- Defaults to an IPv4 loopback listener; it does not bind publicly without an explicit host choice.
- Serves only the selected index path and ignores a query string for that resource.
- Supports GET and HEAD; uses `text/plain; charset=utf-8`, byte-accurate content length, and `nosniff`.
- Computes a SHA-256 content ETag and supports conditional requests with `If-None-Match`, including weak comparison for GET/HEAD.
- Sends `Cache-Control: no-cache`, requiring revalidation instead of assuming a fixed freshness interval.
- Reads and validates the local manifest per request; it does not contact a CMS, fetch links, or execute instructions in content.
- Refuses to start with invalid source data. If a subsequent edit is invalid or unavailable, serves the last valid index with `X-LLMS-Stale: true` and a local diagnostic. Recovery sets the header back to `false`.
- Keeps the same ETag when rendered content is unchanged. It does not fabricate publication dates or expose local errors to visitors.

This is a local reference server, not a production application server. Decide explicitly whether a production endpoint should serve a last-known-good index or fail with a monitored 503 when freshness is critical. A last-known-good response can be stale even when its syntax is valid.

### Exit codes and reports

| Code | Meaning |
| --- | --- |
| `0` | Validation/lint passed; warnings or information may remain, or the server stopped normally |
| `1` | Content errors: invalid manifest data or an index failing the local lint checks |
| `2` | CLI, file access, output-conflict, or listener/startup operation error |

`validate-manifest`, `build`, and `lint` accept `--json`. Reports contain `valid`, `errors`, `warnings`, `info`, and coded issues with locations where available. A successful build also reports output bytes and SHA-256. Human-readable output is the default. Do not convert a warning into a proven defect without checking its context.

## Understand lint results

The linter accepts an H1 alone. It recognizes common ATX and Setext headings, introductory prose or lists, fenced examples before file-list sections, inline links (including angle destinations and optional quoted titles), and single-line reference definitions with full, collapsed, or shortcut reference links. Relative URLs receive a resolution warning rather than an automatic invalidity claim.

It reports HTML error pages, missing or multiple H1 headings, incorrect heading levels, malformed or unresolved file-list links, unsafe URL forms, and unrelated top-level prose after link sections. Duplicate destinations, empty sections, example domains, complex Markdown, and oversized indexes produce advisory warnings. Fenced material inside file-list sections needs manual review. Multiline reference definitions, complex nested block structures, HTML-based links, and other Markdown extensions are not comprehensively parsed.

Treat this as a practical compatibility lint, not a complete CommonMark parser or proof of compliance for all consumers. For an existing file, inspect warnings and rendered meaning before changing it. The linter does not check reachability, redirects, robots permissions, MIME headers, factual consistency, freshness, ranking, or actual provider use. Perform those checks through the skill's separate audit workflow.

### Repair a long-form guide

An existing llms.txt may be useful prose while failing the proposal's file-list
structure. Group repeated diagnostics by code with counts and representative
locations; hundreds of line errors can share one structural cause. Distinguish
format compatibility from factual accuracy and measured discovery.

Preserve the original bytes and review unique claims before shortening. Propose
a concise index of verified, annotated source links. Keep useful long-form content
in the maintained source pages or a separately reviewed companion document rather
than silently discarding it. A companion filename has no guaranteed discovery
semantics; advertise it only after its public endpoint exists. Avoid copying facts
found only in the old index into a new public guide without verifying their source.

For audit-only work, deliver a local candidate and the preservation plan. Choose
build-time delivery only after inspecting the publishing pipeline; otherwise supply
a reviewed static candidate. Keep crawler policy unchanged unless a policy change
is requested.

## Integrate with a production application

Adapt the renderer and validated data contract to the site's existing framework. Preserve its established routing and deployment conventions.

1. Read the approved catalog/CMS snapshot or export; do not trust request parameters as source URLs or file paths.
2. Validate and render a candidate index off the request path where practical.
3. Publish or cache only a complete valid result. Retain a rollback version.
4. Serve the existing `/llms.txt` or scoped route with plain text, correct encoding, and a content validator such as ETag.
5. Select a cache/revalidation policy consistent with the update frequency. An index refresh should not imply that all linked pages changed.
6. Monitor invalid source updates, stale fallback responses, size, response failures, and broken destinations.
7. Check the deployed endpoint after an authorized release, using the actual final headers and response body.

For a Python framework, import `load_manifest` and `render_manifest` from `scripts/llms_toolkit.py` in the site's build or application code after adapting paths to that project. For a Node/edge framework, port the same small deterministic transformation or serve a generated static output. Do not add a request-time Python dependency merely to publish a short text file. Preserve the schema checks in any port; a JSON Schema-only pass does not cover all semantic checks listed above.

For an existing hand-written index, lint and repair it directly when that is the smallest maintainable change. A manifest migration is optional. Preserve accurate links, factual qualifications, applicable scope, and the owner's content preferences.

## Discoverability and Markdown equivalents

Where the current proposal and client support warrant it, advertise the applicable index and an existing Markdown equivalent from an HTML page:

```html
<link rel="describedby" href="https://example.com/llms.txt">
<link rel="alternate" type="text/markdown" href="https://example.com/products/flow/index.md">
```

Equivalent HTTP Link headers can advertise these relations for other resource types. Replace example URLs with real endpoints. Compare Markdown and HTML for meaningful parity: price qualifications, variant labels, tabular limits, and effective dates must survive conversion. Put the human-readable source URL in the Markdown page when useful for attribution; such a preference is advisory.

Keep scoped indexes consistent with the URL scope they describe. The v2 proposal uses the most specific applicable path. The `Optional` section remains a useful convention but has no special mechanical omission semantics in v2.

## Validate changes and rollback

Run the offline contract suite from the skill directory:

```bash
python3 -m unittest discover -s scripts -p 'test_llms_toolkit.py' -v
```

The normal tests do not open network sockets. They exercise the actual HTTP handler with in-memory request and response streams, including GET/HEAD, ETag revalidation, changes, invalid-source fallback, and scope errors. A separate opt-in transport test exists for environments that permit local sockets:

```bash
LLMS_TOOLKIT_LIVE_HTTP=1 python3 -m unittest discover -s scripts -p 'test_llms_toolkit.py' -v
```

Do not report real network serving as verified unless that transport test or a real endpoint check succeeds in the target environment. Review the proposed output diff, run syntax checks, verify linked sources, and preserve the previous file or application version before release. After an authorized deployment, check actual GET and HEAD behavior, conditional responses, route scope, MIME headers, crawler access, and content parity. Revert the known file/version if those checks fail.

## Sources

Recheck provider behavior and the proposal before relying on time-sensitive details. The examples above implement the proposal reviewed on 2026-10-30; they do not establish provider adoption or ranking effects.

- [The llms.txt proposal](https://llmstxt.org/)
- [Proposal v2 changes](https://llmstxt.org/changes.html)
- [HTTP conditional requests, RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-if-none-match)
- [Python http.server documentation](https://docs.python.org/3/library/http.server.html)
