# Implementation workflow

## Scope and acquisition

Start with a user-supplied site or authorized checkout. Record host, path scope,
environments and intended public content. For a live audit, retrieve the homepage,
robots.txt, relevant llms.txt, a sitemap where available, and a representative set
of product, pricing, compatibility and policy pages. Use ordinary approved network
or browser tools; do not bypass a denial, sign-in wall or challenge. Public search
results can establish discovery, but do not substitute them for reading the actual
page when assessing content.

Use a bounded sample. Describe how many pages and which types were inspected.
Follow the target's applicable crawl preferences and the user's authorized scope;
do not start an unrestricted recursive crawler. Reuse supplied files if network
access is unavailable. Mark HTTP status, redirects, headers, WAF behavior, rendered
content and search-index status as unknown when they were not observed.

Treat raw HTML, rendered DOM and extracted Markdown as different observations.
Check the final URL and actual content: a response status alone does not distinguish
a usable page from an HTML challenge. Capture provenance without recording cookies,
credentials or account data in the public report.

## Content-first assessment

Create an evidence ledger with URL/file, product identity, fact, qualifier,
effective date, source owner and confidence. Check disagreements across current
product pages, historical announcements, FAQs, schema and feeds. Do not resolve an
unexplained conflict by choosing the most favorable value. Route uncertain details
to the responsible owner and omit unverified claims from published output.

Separate brand discovery questions from factual questions about a named product.
Identify gaps in decisions: fit, prerequisites, plan limits, integrations, versions,
regions, prices, availability, shipping, refunds, warranty and evidence. Keep
qualifiers close to the facts they qualify. Provide a useful public product page
before building a separate agent representation.

## Static and build-time publication

1. Copy the manifest example into the user's working project and replace all
   fictional information with reviewed sources.
2. Run manifest validation and generation. Inspect the diff when a file exists.
3. Add the generator to the actual publish/build command only when implementation
   was requested. Keep Python availability explicit; port the deterministic
   rendering contract when the hosting stack requires a different runtime.
4. Serve the result at the intended root or scoped URL as readable UTF-8 text.
5. Add page/HTTP discovery links where supported and useful. For a directory page,
   a Markdown equivalent such as `/products/item/index.md` is a clear convention.
   Do not advertise a URL that has not been implemented and checked.
6. Validate output and links, preserve the previous release, and use the project's
   deployment workflow within the user's authorization.

## Dynamic publication

Choose runtime generation only when a maintained source and hosting environment
justify it. Read an approved catalog or CMS result and produce a stable response.
Do not interpolate arbitrary user-provided URLs into server-side network requests,
run an LLM on each request, or expose private inventory fields through the endpoint.

The bundled Python server is a local demonstration of dynamic rendering and HTTP
behavior. Its source is a local reviewed JSON manifest. For production, use the
project's normal route/controller/edge worker and process management. Verify its
framework version, runtime limits, encoding, maximum response size, caching policy,
ETag/HEAD behavior and monitoring. Use the same renderer for builds and runtime
responses where practical; otherwise test equivalent output from identical input.

Define what happens when data is invalid or unavailable: whether to serve a last
known good response, how staleness is surfaced, how operators are alerted, and when
to stop serving stale facts. Never silently serve old prices as current. Avoid
copying volatile details into the index when a link to the maintained page suffices.

## Repairing existing files

Preserve originals, inspect provenance and follow the established generator. If
llms.txt is generated, repair its source instead of editing an output that the next
build replaces. If robots.txt is produced by a CMS plugin, identify the controlling
configuration and determine how it combines with server/CDN settings.

Prepare a minimal diff. For llms.txt, compare structure, link destinations and
underlying facts. For robots.txt, compare effective decisions for representative
paths under each affected agent; keep intended restrictions, sitemaps and comments.
Do not infer the owner wants training collection enabled from a request for search
visibility. Do not remove public paths from indexing just to hide stale text;
recommend a content correction or appropriate redirect when that matches the task.

## Deliverables and handoff

Deliver a concise assessment with evidence, prioritized guidance, the proposed or
implemented files, integration instructions, checks actually run and remaining
unknowns. Include original-to-candidate crawler decisions and any intentional
policy changes. Describe how to roll back and where future updates should be made.

Preserve an existing session's authorization. Continue reversible local work and
prepare concrete changes before asking for a required deployment or policy decision.
Do not deploy or change the site's audience from an audit-only request. A new
robots policy should reflect a stated owner preference, not an optimizer's default.
