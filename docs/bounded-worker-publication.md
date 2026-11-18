# Bounded worker, provisional refresh and evidence publication — local stage

Implemented locally on 2026-11-03. No new server code, live source query, grant,
credential write, service/timer/access change, Feishu write or deployment was run.
All source responses used for tests are synthetic stand-ins. They are not user
confirmations or business-effect evidence, including where legacy real-case
schema requires `sample=false` to test its importer.

## Local components and contracts

- `analysis.worker.query`: fixed `genemedi-net` boundary, 1–20 original HTTPS
  URLs plus up to five separate variants, at most two completed Monday–Sunday
  windows. Country/channel defaults are empty; traffic is not named target
  visitors. Calendar completeness does not prove source finality. No arbitrary
  SQL, source endpoint, database or credentials in requests.
- Preflight requires an `AdapterReader(exact_paths=True)`, declared
  `genemedi-exact/v1` interfaces and compatible method signatures for all sources.
  Old production `aggregate/query_result` methods are not fallback routes.
  Existing GA4/GSC adapters now have separate exact-case methods; their legacy
  methods retain their original behavior. Hostname scope remains registered;
  exact path case is preserved, with existing optional trailing-slash/query
  matching conventions. Union UV comes from a single interval cohort query,
  never addition of page or weekly users.
- `window_metadata` accepts bounded source observations, not a success flag as
  finality. GA4 response timezone and available sampling/threshold/data-loss
  fields flow through the exact reader. Absent fields stay unknown/partial.
  Independent cutoff evidence is explicit; configured lag remains an estimate.
  GSC incomplete-date evidence needs matching-scope verification. The collector
  does not fabricate availability when this observation is absent. Inquiry
  server calendar/dedicated scope remains unverified in provisional reports.
- `case_refresh.collect/run` composes the existing strict weekly packet using
  injected adapters and preserves worker window assessments in a versioned
  optional metadata field. Old packet/report bytes and hashes are unchanged.
  Actual page/date grain is requested separately; unavailable daily data is an
  error envelope, never synthetic day rows. Original URL case, variant separation,
  approximate September start, unknown end/stage dates and observational meaning
  are preserved. CaseStore retains each version; stale/future/scope-changing
  packets fail. The private store must already contain historical evidence.
- `RunLog`: private 0600 SQLite events with run ID, request digest, stage,
  status and timestamp only. No exception text, DSNs, form fields or credentials.
  Preflight/source failure cannot appear as a successful capture. A process
  killed before a closing event leaves an unfinished `started` event; external
  service monitoring is still needed. The query catalog remains explicitly an
  imported-evidence catalog, not a worker-health oracle.

## Explicit local entries

With the development Python and `PYTHONPATH=src`:

```sh
python -m website_analytics.analysis.worker --help
python -m website_analytics.analysis.case_refresh --help
python -m website_analytics.analysis.publication --help
python -m pytest tests/test_analysis_worker.py tests/test_analysis_publication.py -q
```

Worker fixture mode requires `--request`, `--sample-evidence`, `--run-log`,
`--run-id`, `--output` and optionally a fixed `--as-of`. The strict fixture is
`worker-evidence/v1`, `sample=true`, exact scope/SourceEvidence records. CLI output
creation is exclusive, with exit 3 for partial evidence. No client is initialized.

`--registered-config` is the explicit existing-access alternative, not a default.
It is implemented but **not exercised with real credentials**. Request and site
bounds are checked first. Existing config/auth factories supply clients; unknown
or missing verified inquiry configuration fails rather than being filled with
`dedicated_database`. There is no interactive authorization or credential save.
Production compatibility, source timing and permissions still need verification.

Case fixture mode uses `--saved-packet`, `--previous-store`, `--case-id`,
`--previous-evidence-id`, `--request`, `--output-store`, `--run-id` and `--run-log`.
The output store must be pre-seeded with history. The explicit registered-source
alternative also requires a reviewed public `--runtime-manifest`; it never runs
the old synchronization CLI. A case version being recorded is not a claim that
its sources are independently finalized or its optimization effect is confirmed.

## Export and atomic publish

Two separate operator commands/APIs, neither available from query HTTP:

```sh
python -m website_analytics.analysis.publication export \
  --source PRIVATE.sqlite --output NEW-CANDIDATE.sqlite --kind cases
python -m website_analytics.analysis.publication publish \
  --candidate NEW-CANDIDATE.sqlite --directory CONTROLLED-QUERY-DIRECTORY \
  --kind cases --candidate-sha REVIEWED-SHA --previous-sha EXPECTED-OLD-SHA
```

`--expect-absent` replaces `--previous-sha` only for a first publication. `kind`
is cases or refreshes, fixing the target basename. Paths are operator startup
configuration, not caller/agent request values. A privileged publisher can use
the reviewed analyticsquery GID with `--read-gid`; the code creates no account
and grants no persistent access itself.

Export takes a bounded consistent private SQLite backup, validates schema,
payloads and provenance, then rebuilds only known tables. Views, triggers,
extra tables/fields, credential/personal-looking text and oversized data fail.
Private run logs are separate and cannot be exported as evidence. The candidate
is new and 0400; no source code, customer records or raw database details are
copied into the query database.

Publish verifies the candidate SHA from a no-follow FD, copies into an exclusive
0600 temporary file, validates again, checks expected target SHA and preservation
of every historical row/cursor, refuses head/source regression, retains a prior
hash-named file, and atomically replaces only the selected SQLite file at 0440.
It fsyncs the file and directory. Interruption before replacement leaves the old
database intact. After-replacement failure returns `PublicationUncertain` / CLI
exit 4: inspect the target hash before retry; do not blindly repeat publication.
This is an integrity boundary, not a cryptographic signature or protection from
a malicious administrator. Controlled directory ownership is a deployment gate.

The read-only service is not given write access or source clients. New catalog
queries use one read transaction. Existing fixed/provisional history remains
queryable after publication. No automatic deletion or retention policy exists.
Current bounds: 32 case versions per case, 512 rows per exported table, 32 MiB
database and 10,000 log events. Pilot capacity/archival policy must be reviewed
before unattended scheduling; exceeding a budget fails, never purges evidence.

## Concrete proposed deployment objects — not created

| Object | Proposed identity/path/permission |
| --- | --- |
| Source code/runtime | `/opt/website-analytics-refresh/releases/<reviewed-sha>/`, root-owned independent venv; never replace old sync current/venv |
| Source worker | existing `websiteanalytics`; original readonly Google scopes and registered database connection; no new grant or guessed scope |
| Private working/history DB and logs | `/var/lib/website-analytics-refresh/worker/`, websiteanalytics 0700, logs 0600; seed all old case history by validated export before first run |
| Private candidate exports | `/var/lib/website-analytics-refresh/outbox/`, websiteanalytics 0700, candidates 0400; fixed reviewed paths and SHA receipt |
| Publisher | fixed administrator-owned oneshot, no network, no source SDK/grant/env; sandbox hides old credentials; may read only reviewed candidate location and write new query live directory |
| New public evidence directory | `/var/lib/website-analytics-query/live/`, root:analyticsquery 0750; `cases.sqlite` / optional initialized `refreshes.sqlite`, root:analyticsquery 0440 |
| Query service | existing analyticsquery, credential-free, loopback8765, existing sandbox; reviewed release/drop-in selects live evidence only after acceptance |
| Candidate units | `website-analytics-refresh.service`, `website-analytics-refresh-publish.service`, later `website-analytics-refresh.timer` |

The current real case remains provisional: do not create a fake confirmed batch
or synthetic production refresh head just to populate the new refresh catalog.
Use its case versions API; enable generic refresh discovery only when a genuine
matching store exists. Keep original `/var/lib/website-analytics-query/cases.sqlite`
and the prior query release untouched as rollback assets.

Remaining deployment gates: freeze source/public runtime manifest and dependency
versions, verify existing config/inquiry isolation evidence without changing it,
test live bounded responses/finality unknowns under the original identity, test
Linux ownership/sandbox and atomic replacement, and review a fixed launcher and
candidate-manifest handoff. No root command/parameter channel is exposed to agents.
First propose one shadow run without a timer. A later daily candidate is 06:30
Asia/Shanghai with an independent lock and a finite pilot; on-demand means an
administrator starts the same fixed oneshot. These schedules/services do not
exist yet; no old timer is modified. Long-term Google authorization remains a
separate issue, not solved or renewed by this code.

Rollback: stop only new worker/publisher/schedule, preserve private failure logs,
restore the prior query release and old evidence path/hash. Publication never
deletes historical data. Any rollback replacing a published file must itself be
an explicit administrator operation; normal monotonic publication rejects it.
