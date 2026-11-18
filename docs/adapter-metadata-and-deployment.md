# Adapter metadata integration and release proposal

Local implementation, November 4. This stage performs no real-source call,
server connection, dependency upgrade, grant, credential, schedule, Feishu write
or deployment. New source tests use injected clients; saved real aggregates are
replayed offline. It does not establish an inquiry timezone or causal effect.

## Implemented

- GA4 detects SDK capability from the actual protobuf schema. New truncation
  reason support, returned truncation, sampling, thresholding, other-row loss
  and restrictions are separate nullable fields. Absent/unsupported metadata
  never means false completeness. Raw reason messages are discarded. Exact
  request witnesses bind property, hosts, dates, case-sensitive pages and
  segments; unrelated/failed requests clear the witness. Lag stays an estimate.
- `GSCAdapter.availability_exact_result` declares
  `genemedi-exact-metadata/v1`. It makes a <=14-day date-only `dataState=all`,
  `type=web`, rowLimit100 request with the supplied exact page filter. It checks
  date grain/range, normalizes camel/snake incomplete-date aliases and rejects
  conflicts. Only approved metadata is returned, never query terms or raw bodies.
  Missing metadata does not become the maximum returned date.
- `analysis.adapter_metadata.capture_observation` validates actual request
  provenance before binding observations. Unsupported/mismatched/failed metadata
  is a bounded diagnostic, not a legacy CLI/raw-HTTP fallback. Authorization
  denial stops subsequent requests to that source, including extra case grain.
- The worker captures metadata immediately after each aggregate. Identical GSC
  metadata windows are cached per run. The case packet retains observations,
  source-registration hashes and sanitized diagnostics; all query views project
  them consistently without source access. Revisions append, never rewrite.
- Inquiry metadata reuses an already read aggregate. It adds no SQL. Database
  dates are not page-cohort coverage; stored DATE/writer calendar stays unknown,
  max record date is not a cutoff, and counts are not qualified leads.

## Explicit metadata dates

Both worker and registered-source case-refresh CLIs accept
`--metadata-windows <operator-owned JSON>`. Default requests use each exact
business week; no date extension is guessed. For example:

```json
{"2026-10-26":{"start":"2026-10-26","end":"2026-11-03"}}
```

Keys name existing worker weeks; ranges contain those weeks, are <=14 days and
cannot exceed the source's current calendar date. Validate before source
factories. Saved-packet replay rejects this fetch option. HTTP agents cannot
supply paths, windows, endpoints or source configuration. The private run-log
scope digest binds explicit windows as well as the business request.

For the original four pages, separate lowercase variant and two weeks, a native
full case uses 14 logical GA4 calls, 5 GSC final-data calls, up to 4 GSC metadata
calls, and 6 existing inquiry aggregate reads (each uses two aggregate SELECTs
plus the configured database guard). Metadata adds no GA4/inquiry requests.
Identical metadata windows can reduce GSC calls. A server pilot must use finite
timeouts and a one-attempt policy; these are not new production schedules.

## Actual-server gates

The last verified server has Python3.12.3, query identity analyticsquery, source
identity websiteanalytics and GA4 SDK0.23.0. These are dated observations, not a
fresh preflight. SDK0.23.0 lacks the new truncation field: the pilot must expose
that unknown, not upgrade the old venv. Recheck config/release hashes, identities,
versions, source access and disk; freeze a matching independent dependency lock
including transitive packages/wheel hashes before execution.

Freeze audited runtime provenance too. The legacy runtime flag
`local_developer_edits_deployed` is not a production deployment attestation and
the existing validator rejects true. Do not flip it to bypass validation or
reuse an old release label to claim the source service was updated. A formal
worker release needs an explicitly reviewed provenance manifest/contract while
old imported records keep their original meanings.

Validate real GSC alias/aggregation/missing metadata and network/denial behavior,
Google standard refresh under existing scopes, inquiry fixed schema/hosts and
unknown day boundary, and Linux sandbox/ownership/atomic publication. No new
grant is expected. If existing authorization fails, stop; do not start OAuth.
Live source execution, Linux isolation and colleague/Windows access were not
validated in this stage. Query imports remain free of source-client factories.

## Minimal production objects — proposal only

First use a manual oneshot pilot, then an explicitly approved publication/query
switch. Do not enable a timer or add colleague access in this release.

| Object | Proposed identity/path/permission |
| --- | --- |
| Worker release/venv | `/opt/website-analytics-refresh/releases/<approved-sha>/`, root-owned independent environment; leave old current/venv untouched |
| Fixed public configuration | `/etc/website-analytics-refresh/{sites.yaml,request.json,metadata-windows.json,runtime-manifest.json}`, root:websiteanalytics0640, directory0750; no credentials |
| Source identity | Existing websiteanalytics; existing readonly scopes and column-SELECT database account, no new grant/login/sudo |
| Existing ADC | Reference `/var/lib/website-analytics/.config/gcloud/application_default_credentials.json`; no copy, replacement or new authorization |
| Credential loading | Reviewed fixed launcher reads only the named existing inquiry key and proxy settings into memory; no whole-file EnvironmentFile, secrets in argv/logs, or query-agent control |
| Private state/logs | `/var/lib/website-analytics-refresh/worker/`, websiteanalytics0700; cases/run-log SQLite files0600; validated seed preserves all public history |
| Candidate | `/var/lib/website-analytics-refresh/outbox/cases.sqlite`, websiteanalytics0400, parent0700; export excludes run logs and records SHA |
| Worker unit | New `website-analytics-refresh.service`, oneshot websiteanalytics, UMask0077, no capabilities; only private worker/outbox writable; no enable/timer |
| Publisher unit | New fixed administrator-owned `website-analytics-refresh-publish.service`, oneshot, no network/source credentials; reads only reviewed candidate and writes new live evidence directory |
| Live evidence | `/var/lib/website-analytics-query/live/cases.sqlite`, root:analyticsquery0440, parent0750; original fixed cases.sqlite remains untouched |
| Query code | `/opt/website-analytics-query/releases/<approved-query-sha>/`, independent YAML-only venv; existing analyticsquery/sandbox, only127.0.0.1:8765 |
| Query switch | Approved `website-analytics-query.service.d/20-scoped-evidence.conf` selects reviewed code/live evidence; restart query service only |

The fixed env-whitelist launcher, exact dependency lock and Linux unit/drop-in
content still require review before execution. This table does not claim those
objects exist. The independent source config must truthfully bind
v2_genemedi_net.contacts and allowed GeneMedi hosts; old production sites.yaml
currently lacks this new contract and must not be edited or labelled dedicated.
Existing websiteanalytics permissions are reused, not narrowed by a new folder;
do not claim it cannot read every secret that identity already could read.
analyticsquery remains isolated from source credentials. Stronger credential
isolation would be a separate, explicit change.

## Ordered approval/acceptance

1. Freeze file/dependency manifests and baseline old units/config/ADC metadata,
   query release and actual public evidence versions/hash. Do not assume local
   newer case IDs have been published. Reject collisions.
2. Install only new worker/private files. Seed all old public versions plus any
   specifically reviewed aggregate evidence. Old query and sync remain running.
3. With execution approval run the fixed oneshot once for the original URLs and
   two approved weeks. Explicit recent metadata dates must be included in that
   approval. Set one-attempt retry policies, per-call/whole-run limits. No OAuth,
   original sync runner, Feishu or website operation is called.
4. Validate real scopes, quality capability/unknowns, time order, exact-case
   variants, aggregate UV grain, reconciliation and immutable history. Access
   denial or scope/schema mismatch stops publication. Missing provider finality
   is acceptable only as explicit provisional/partial evidence.
5. With publication approval validate/export and compare-and-swap the **new**
   live file by expected hash, preserving history. If replacement is uncertain,
   inspect the target before retry. Do not synthesize a confirmed refresh head.
6. With query-switch approval select the new release/live path. Verify source
   credential denial, loopback/read-only behavior, rejected arbitrary requests,
   two clients/restart and old baselines. No scheduling or colleague access.

These conditional steps can be presented together as one concrete approval
scope; failure never authorizes skipping an acceptance gate. Future daily/on-
demand automation is a later proposal, not installed by these steps.

## Rollback

Before publication/switch, stop only the new oneshot if required and retain
private logs/candidates; old services were untouched. After a query switch,
restore the recorded prior query release/ExecStart evidence path and restart
only that query service. Original fixed evidence was never overwritten. Retain
new/private evidence for review; do not purge history. Do not alter ADC, restart
old sync, restore timers or change Google/Feishu settings because none changed.

## Frozen local release candidate

The fixed launcher, runtime-provenance/v2, SHA256 dependency locks, offline
Linux wheels and tested publication/rollback path are now prepared locally.
See [the RC runbook](../deploy/refresh-rc/README.md) for the superseding concrete
file/identity/command list. Public inputs now use pilot.json/publish.json; runtime
provenance is generated by the verified fixed launcher, not a hand-labelled
runtime-manifest.json. This does not claim server installation or validation.
