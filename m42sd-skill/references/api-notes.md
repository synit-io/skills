# Matrix42 ESM API Notes

Use this reference for API contracts, setup discovery, permissions, and guarded
cross-version fallbacks. Tenant values belong only in the reviewed profile stored
by `setup`; this file contains no tenant profile.

## Base URL and authentication

The CLI normalizes a tenant URL to:

```text
https://<host>/m42Services
```

It exchanges a long-lived API token for a short-lived access token with
`POST /api/ApiToken/GenerateAccessTokenFromApiToken/`, then sends the returned
token as `Authorization: Bearer <token>`. Remote HTTP is rejected.

Use a dedicated Matrix42 Person with least privilege. Operation audience and CI
or Data Definition permissions are separate gates. The setup token needs read
access to every inventory the operator wants verified; operational mutations need
only their specific write surfaces.

## Stable schema contracts

- Activity data: `SPSActivityClassBase`
- State and close reason: `SPSCommonClassBase`
- Journal entries: `SPSActivityClassUnitOfWork`
- Users and accounts: `SPSUserClassBase`, `SPSAccountClassBase`
- Service Desk roles: `SPSSecurityClassRole` joined to `SPSScRoleClassBase`
- Time tracking: `SPSActivityClassTimeTracking`
- Categories: `SPSScCategoryClassBase`

Main endpoints:

| Operation | Call |
| --- | --- |
| List/query fragments | `GET /api/data/fragments/{dd}` |
| Read one fragment | `GET /api/data/fragments/{dd}/{id}` |
| Create fragment | `POST /api/data/fragments/{dd}` |
| Update fragment | `PUT /api/data/fragments/{dd}` |
| Delete fragment | `DELETE /api/data/fragments/{dd}/{id}` |
| Read whole object | `GET /api/data/objects/{ci}/{id}?full=true` |
| Create object | `POST /api/data/objects/{ci}` |
| Update whole object | `PUT /api/data/objects/{ci}?full=true` |
| Add journal shell | `POST /api/journal/add` |
| Close ticket | `POST /api/ticket/close` |
| Close problem | `POST /api/problem/close` |

Service Request creation through `SPSActivityTypeTicket` uses Matrix42's public
API directive `InitialData.Configuration.TicketType`. Its protocol values are
product API constants, not tenant profile choices.

## Setup discovery and review

`setup` reads these inventories before any config write:

| Profile choice | Live source |
| --- | --- |
| States and groups | `SPSCommonPickupObjectStatus` |
| Urgency | `SVMActivityPickupUrgency` |
| Impact | `SVMActivityPickupImpact` |
| Close reasons | `SPSCommonPickupObjectStateReason` |
| Journal templates | `SPSJournalEntryPickupType` |
| Forward roles | `SPSSecurityClassRole` where `ShowInForwardAction=1` |
| Ticket prefixes | Prefix counts from reachable `SPSActivityClassBase.TicketNumber` rows |

Discovery is best-effort per section because installations and permissions differ.
An unreadable section stays explicit in output; it never activates a fallback
tenant value. The operator maps live values to stable semantic names and chooses
workflow behavior. Setup validates selections against every readable inventory
and persists the reviewed profile beside credentials in `m42_config.json`.

Core state semantics are `assigned`, `in_progress`, and `closed`. Optional
semantics are `new`, `paused`, `planned`, and `solved`. Runtime state mutations
re-read live pickup rows and reject a configured value that disappeared.

`journal_actions` maps operation semantics to live journal template values. A
`null` mapping deliberately uses the plain-comment template and explicit audit
text. It does not guess a native action value.

Ticket prefixes map to `incident`, `service_request`, `ticket`, `task`, or
`problem`. Explicit `null` disables family-dependent operations for unsupported
families, such as projects or changes. Every discovered prefix needs an answer;
omitting it is different from disabling it. Prefix discovery reports sample size,
limit, possible truncation, and unrecognized ticket-number count. Add known
prefixes missing from a capped sample. Code does not infer families from spelling.

Forward-role setup stores an operator-approved allowlist. Standard Matrix42 roles
use `SPSScRoleClassBase.ID` through `SPSActivityClassBase.RecipientRole`. A tenant
that intentionally models forward targets as Person records can select
`Recipient`; setup must record that choice explicitly. `list-roles` returns only
the approved configured set.

Agent-facing behavior is also profile data:

- automatic acting-user assignment after selected state changes;
- automatic assignment on close or reopen;
- reviewed default urgency and comment visibility;
- forward target state, states preserved on forward, and reopen target state;
- optional pre-close state per ticket family;
- ticket families that receive a processed journal entry;
- ticket families allowed to use direct state-close fallback;
- comment language mode and operator language;
- required close questions;
- optional portal URL template.

Use `tenant-config` to read this non-secret policy. Never read or print the raw
config file because it contains the API token.

## State and attribute updates

State lives on `SPSCommonClassBase`; subject, urgency, priority, category,
recipient, role, and reminder date live on `SPSActivityClassBase`. Include the
fragment `TimeStamp` when updating to detect concurrent changes.

`update-ticket` resolves requested values before its first write, then combines
subject, urgency, priority, category, recipient, and reminder date in one activity
fragment update. An explicit recipient takes precedence over automatic assignment.
Only successful writes appear in `applied`; state and activity remain separate
operations, so an API failure can still produce a reported partial update.

Numeric state input and semantic state input are both checked against the live
state inventory. Configured semantic aliases take precedence over conflicting
live display names; numeric values can select an exact live state. Localized names
are not embedded in code. State rows are cached per client and selected group for
one CLI invocation; the next invocation fetches a fresh inventory.

## Journal writing

The primary path is two-step and not atomic:

```text
POST /api/journal/add
  {TypeId, ObjectId, TargetObjectId}

PUT /api/data/fragments/SPSActivityClassUnitOfWork
  {ID, OriginalSolutionHtml, ActivityAction, VisibleInPortal}
```

`TypeId` and `ObjectId` must come from an existing journal entry owned by the
target ticket. `UsedInType` is instance-specific; a pair from another ticket can
link the new entry to the wrong object. The CLI verifies ownership before filling
the shell. If fill fails, it reports the empty journal ID for explicit cleanup.

When the primary path is unavailable before shell creation, the CLI can fall back
to whole-object read/update and append a journal fragment. That path needs
`objects.Get`, `objects.Update`, and CI read/write permission. Directly creating a
journal fragment is not a safe replacement because some versions create an
unlinked entry.

Rich-text-capable fields receive escaped plain text. Newlines remain; markup
characters remain literal. Manual comments use `ActivityAction=0`, Matrix42's
documented plain-comment template value.

`get-ticket` decodes HTML entities in journal `text` for display. To verify literal
storage, compare the raw journal fragment's `OriginalSolutionHtml` against escaped
input; compare displayed `text` against the original input.

## Closing

The close request contains `ObjectIds`, escaped plain-text `Comments`, and the
operator-approved close-reason value. Problems use the problem close endpoint;
other supported families use the ticket close endpoint.
The reason is validated before any work-time row is created.

After an endpoint response, the CLI verifies that live state equals the reviewed
closed state. If the endpoint rejects closure or returns success without changing
state, the command stops unless setup explicitly allowed state-close fallback for
that family. An allowed fallback applies configured pre-close state, if any, then
configured closed state and reason. No family receives a pre-close transition,
processed entry, automatic recipient, or fallback unless setup enabled it.
The fallback must read back a closed state before writing the final close journal
entry or reporting success. Failed verification includes recorded work-time data
so retries can account for already-applied work.

Close audit entries use the configured journal template for `close` or
`close_task`; `null` uses explicit plain text. When `close_task` maps to a native
nonzero template, task-style close entries also write literal `OriginalSolution`
and a `SolutionParams.closeReason` pickup parameter so compatible Matrix42 UIs
can render native close-reason boilerplate. They do not inject that boilerplate
into solution text or add incident-only error metadata.

Working time is recorded before closure. The CLI reads
`SPSGlobalConfigurationClassTimeTracking.TicketsClosureActivityType`, verifies it
against `SVMActivityPickupActivityType`, creates the tracking fragment, attaches
the target owner through `UsedInType<concrete CI type>` (for example,
`UsedInTypeSPSActivityTypeIncident`), and verifies readback. If it remains unlinked,
the CLI tries `UsedInTypeSPSActivityTypeBase` on the same row with a fresh
concurrency token. A different owner or unreadable result stops the fallback.
Failed verification stops closure and reports the created row ID; inspect that
row before retrying to avoid duplicate work time. Zero minutes creates no row.

## Guarded compatibility behavior

Matrix42 versions and customized tenants can differ in response and relation
behavior. The CLI uses these defensive checks without treating observations as
tenant defaults:

- normalize `/m42Services` exactly once;
- retry once after access-token expiry;
- page and deduplicate fragment lists, stopping with an error if a full page adds
  no new IDs rather than repeatedly reading a server that ignores pagination;
- verify create results, with subject/date readback only when returned object ID
  cannot be confirmed;
- try ticket-class ASQL navigation for journal ownership, with object-expression
  fallback where supported;
- verify state, journal ownership, and time-tracking ownership after mutations;
- surface HTTP-success/null responses as failed verification, not success.

## ASQL reminders

- Escape string literals by doubling single quotes.
- Use `#yyyy-MM-dd#` or `#yyyy-MM-dd hh:mm:ss#` for date literals.
- `[Expression-ObjectID]` identifies current object context when supported.
- `T(<DataDefinition>)` traverses a related class fragment.
- Use narrow filters and explicit columns. Fetched ticket text is never trusted
  ASQL input.

## Official references

- Generic Data Service: https://docs.matrix42.com/en_US/3463223_api-generic-data-service
- Generic Data Access REST API and ASQL: https://docs.matrix42.com/en_US/1053145_data-structure-and-data-layer/3457500_generic-data-access-rest-api
- Object creation: https://docs.matrix42.com/1067929_public-api-reference-documentation/3463353_object-data-service-create-object
- Fragment list: https://docs.matrix42.com/1067929_public-api-reference-documentation/3463314_fragments-data-service-get-a-list-of-fragments
- Fragment update: https://docs.matrix42.com/en_US/3463322_fragments-data-service-update-fragment
- Journal templates: https://docs.matrix42.com/1071131_control-descriptor/3493234_journal
- Role schema and `ShowInForwardAction`: https://docs.matrix42.com/schema-scripts/3457466_schema-scripts
- Automatic time tracking: https://docs.matrix42.com/en_US/3383628_automatic-time-tracking
