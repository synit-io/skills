# Evaluation and experiments

## Define success before changes

Measure four outcomes separately: discovery, answer accuracy, appropriate
attribution, and useful customer action. Do not treat a crawl, citation, endorsement
and conversion as interchangeable events.

Build a small representative prompt set from actual customer questions. Include
branded factual prompts, unbranded category discovery, product comparisons, current
pricing/availability, compatibility, and situations where the product is unsuitable.
Record an evidence-backed expected answer or scoring rubric. Do not award discovery
credit simply because a prompt names the product.

## Controlled usefulness

Use a configurable test agent with the same model/settings and questions. Compare
access to the ordinary pages with access to the proposed index or representation.
Hold underlying facts constant when testing the index itself. Log source requests,
selected URLs, extracted facts, qualifiers, unsupported claims and citations.

This measures usefulness conditional on ingestion. It does not prove an external
service naturally discovers the file. Do not create a false result by asking one
arm to favor the brand or giving only one arm more accurate facts.

## Natural discovery

Ask neutral questions in the relevant public services without supplying the domain
or telling them to read llms.txt. Use fresh conversations; record service, model if
shown, search mode, locale, date, exact prompt, answer and cited URLs. Repeat over
time to observe variation. Avoid presenting a single output as a stable ranking.

Where practical, change one major factor at a time or use comparable cohorts.
Model/index changes, competitor changes, personalization and simultaneous SEO work
can confound before/after comparisons. Report uncertainty instead of attributing
all movement to llms.txt.

## Metrics

| Metric | Interpretation |
| --- | --- |
| Correct product/plan/version identification | Whether the answer describes the intended entity |
| Correct material facts / checked facts | Accuracy against the reviewed source ledger |
| Unsupported feature or price claims | A concrete hallucination/error measure |
| Preserved qualifications | Whether billing, region, limitations and prerequisites remain attached |
| Appropriate current-source citations | Useful attribution, not automatic endorsement |
| Relevant unbranded inclusion | Observed discovery within a defined prompt sample |
| Qualified visits and conversions | Business value, subject to attribution gaps |
| Verified fetches | Access evidence only; no proof of reading or influence |

Use current official Search Console/Bing reports when authorized and available.
Record the exact supported surfaces; do not call Search reports Gemini-app
analytics or represent a limited partner report as all ChatGPT/Claude usage.

## Candidate experiments

Treat these as hypotheses, not secret ranking factors:

1. Replace vague index labels with descriptions of the questions each link answers.
2. Add product aliases, current names and clear model/plan/version boundaries.
3. Keep currency, billing units, region and commitment beside price facts.
4. Add explicit compatibility exclusions and release status.
5. Route current questions away from historical announcements and label archives.
6. Preserve public canonical attribution URLs alongside Markdown representations.
7. Derive HTML, Markdown, structured data and feeds from the same reviewed fields.
8. Test scoped indexes on large sites with actual product/language boundaries.

Select experiments from observed failures. Define the metric, scope and duration,
then report results and limitations. Lower unsuitable-recommendation rates can be
an improvement even if raw mention counts fall.
