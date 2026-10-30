# Providers and evidence

Reference snapshot: 2026-10-30. Treat this file as a research map, not a permanent
provider contract. Verify the affected primary source during each policy change or
time-sensitive recommendation. Record the actual verification date and any failed
retrieval. Do not convert a historical statement into a current fact without checking.

## Evidence levels

- **Observed:** reproducible result from the supplied file, response, repository or
  authorized report. State coverage and raw/rendered limitations.
- **Documented:** a current primary source explicitly supports the mechanism.
- **Recommendation:** an implementation judgment supported by the actual content
  and a clear purpose; do not present it as a secret ranking signal.
- **Experiment:** a plausible improvement with a defined test and no promised lift.
- **Unknown:** data or current documentation unavailable. Complete unaffected work.

## Source registry

| Topic | Primary source | What to verify |
| --- | --- | --- |
| llms.txt format | https://llmstxt.org/ | Current proposal, file structure, scope and consumption guidance |
| Proposal changes | https://llmstxt.org/changes.html | Link relations, subpath scope, Markdown URL conventions |
| Robots protocol | https://www.rfc-editor.org/rfc/rfc9309.html | Product tokens, grouping, matching, encoded octets, HTTP behavior |
| Google interpretation | https://developers.google.com/crawling/docs/robots-txt | Provider-specific matching, supported fields and delivery behavior; follow redirects |
| OpenAI crawlers | https://developers.openai.com/api/docs/bots | OAI-SearchBot, GPTBot, ChatGPT-User, published identity verification and independent controls |
| Anthropic crawlers | https://support.claude.com/en/articles/8896518-does-anthropic-crawl-data-from-the-web-and-how-can-site-owners-block-the-crawler | Claude-SearchBot, Claude-User, ClaudeBot and robots compliance |
| Google crawlers | https://developers.google.com/crawling/docs/crawlers-fetchers/google-common-crawlers | Googlebot and Google-Extended purposes, grounding scope and request identities |
| Perplexity crawlers | https://docs.perplexity.ai/docs/resources/perplexity-crawlers | Search/user fetch differences and current IP verification |
| Google AI visibility | https://developers.google.com/search/docs/fundamentals/ai-optimization-guide | Current Search eligibility, llms.txt stance, inclusion settings and measurement |
| Search technical eligibility | https://developers.google.com/search/docs/appearance/ai-features | Indexing, snippet controls, accessible text and visible-data consistency |
| Product data | https://developers.google.com/search/docs/appearance/structured-data/product | Product/variant data, merchant feeds and actual eligibility |
| OpenAI shopping | https://help.openai.com/en/articles/11128490-shopping-with-chatgpt-search | Catalog sources, direct-feed availability and current merchant integrations |
| OpenAI product feeds | https://developers.openai.com/commerce/guides/best-practices | Onboarding eligibility, supported fields and variant requirements |
| IndexNow | https://www.indexnow.org/faq | Participating engines, meaningful updates and lack of indexing guarantee |
| Connected data | https://modelcontextprotocol.io/introduction | MCP host/client integration and supported tool/data access |
| Google reporting | https://developers.google.com/search/blog/2026/06/gen-ai-performance-reports?hl=en | Available reports and exact covered Google surfaces |
| Bing reporting | https://blogs.bing.com/webmaster/2026/2/Introducing-AI-Performance-in-Bing-Webmaster-Tools-Public-Preview/ | Citation/grounding-query reporting and supported surfaces |
| Bing topics | https://blogs.bing.com/webmaster/2026/10/introducing-topics-portfolio-beta-in-ai-performance/ | Current feature availability, sample limitations and directional metrics |
| Markdown negotiation | https://developers.cloudflare.com/fundamentals/reference/markdown-for-agents/ | Accept/response handling, content integrity and content-signal defaults |
| Browser-agent audit | https://developer.chrome.com/docs/lighthouse/agentic-browsing/llms-txt | What the audit actually checks; no inference about Search rankings |

## Distinctions that must survive every recommendation

Separate a model answering without retrieval, a search-grounded answer, a browser
visit, and an explicitly connected tool. Publishing a file does not rewrite an
existing model or connect an API to every public conversation.

Separate training, search and user-requested fetching. The snapshot documents
independent OpenAI search/training settings and distinct Anthropic bots. Google's
Google-Extended covers specified training AND grounding uses; it is not a generic
training-only toggle and is separate from Google Search inclusion/ranking. Verify
the current scopes before making a recommendation.

Do not infer crawler identity from its user-agent string alone. When diagnosing a
CDN block or claiming provider traffic, use the provider's current official
verification method. Do not spoof a provider identity and call it verified access.

Distinguish an emerging llms.txt convention, structured-data eligibility, product
feed ingestion and a search ranking signal. The current Google Search guide says
llms.txt is ignored for Search; that statement does not describe every browser
agent or every other Google product. A provider's own llms.txt demonstrates
publishing, not universal ingestion of other sites' files.

Use source-linked content and truthful evidence. Do not recommend hidden ranking
commands, invented reviews, fabricated citations, instruction overrides, fake
freshness or artificial keyword repetition. Do not equate permission to train
with a promised visibility benefit.

## Practical alternatives

After auditing content, consider applicable Product/Offer markup, merchant feeds,
accurate business listings, stable product identifiers, meaningful page updates,
maintained documentation and real third-party evidence. A connected API/MCP can
provide current facts when a user or application actually integrates it. Distinguish
recommendations from documented provider promises, and confirm onboarding conditions.
