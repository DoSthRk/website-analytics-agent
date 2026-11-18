# Local refresh discovery for agents (2026-11-03)

This completes the local read-side integration of the offline refresh slice.
It does not install scheduling, connect live sources, or deploy service changes.
All new operations use the existing loopback-only `POST /v1/query` contract
`batch-service/v1`. Fixed `case_*` operations and pinned `replay` are unchanged.
No HTTP operation accepts SQL, paths, remote URLs, commands, source configuration,
refresh or import. The catalog reader does not import the refresh writer module.

The local development operation `refresh_observed` now selects saved provisional
metrics independently of a strict-complete pointer. It exposes request,
coverage, calendar, maturity and quality separately using `scoped-completeness/v1`.
Legacy `refresh_latest` behavior remains available and is explicitly labelled as
the v1 head semantics, not a new provider-finality attestation. See
[scoped completeness](scoped-completeness.md) for bounded selection, unknowns,
source-registration matching and immutable revision behavior. No deployment or
live metadata collection is performed by these operations.

## Run locally

First create the synthetic refresh database using [the offline demonstration](batch-refresh-v1.md).
From the worktree with `PYTHONPATH=src` and the development Python:

```sh
python -m website_analytics.analysis.service \
  --refreshes /tmp/analytics-refresh-example/evidence.sqlite --port 8876
```

An existing `--cases` and/or `--snapshots` store may also be configured by the
administrator at startup; callers cannot change these paths over HTTP. Default
binding remains `127.0.0.1`, and there is no public listener or new credential.
The catalog requires SQLite JSON functions (standard SQLite 3.38+); production
runtime/permissions and safe export or database sidecar access need approval
and verification before deployment. No schema migration is needed for a database
already produced by `RefreshStore`. Enabling the catalog on an old snapshot-only
database fails rather than creating or migrating tables from the reader.

## Two independent clients, discovery then query

Client A (existing CLI):

```sh
python -m website_analytics.analysis.client --port 8876 --operation refresh_jobs
python -m website_analytics.analysis.client --port 8876 --operation refresh_latest --job-id JOB_ID
python -m website_analytics.analysis.client --port 8876 --operation refresh_versions --job-id JOB_ID --limit 10
python -m website_analytics.analysis.client --port 8876 --operation refresh_snapshot --job-id JOB_ID --snapshot-id SNAPSHOT_ID
```

Replace IDs with values returned by discovery; they are hashes, not file paths.
Client B (independent HTTP implementation, no package or Google SDK needed):

```sh
curl --noproxy '*' -H 'Content-Type: application/json' \
  -d '{"contract":"batch-service/v1","operation":"refresh_jobs","limit":10}' \
  http://127.0.0.1:8876/v1/query
```

For latest, send `{"contract":"batch-service/v1","operation":"refresh_latest","job_id":"JOB_ID"}`.
For historical replay, use operation `refresh_snapshot` and add `snapshot_id`.
`refresh_versions` returns immutable run receipts, including rejected attempts;
one snapshot can occur under several run IDs. `refresh_snapshot` enforces job
membership, while legacy `replay` is still explicitly pinned by snapshot ID.

Discovery and versions default to 10 items, maximum 20. Use returned
`next_after_job` as `after_job` for jobs, or `next_before_sequence` as
`before_sequence` for versions. Null means no next page. Unknown jobs or
cross-job snapshot selection return 404; malformed/extra parameters return 400.
Catalog source scopes contain dates, segments, page count and scope digest;
exact page sets stay in pinned reports rather than being multiplied in listings.

## How agents must read results

- `refresh_latest` always chooses the last **complete** imported head, not the
  newest row. It returns its pinned report, source data-through, capture/import
  times, and a separate `latest_attempt` receipt. Selection and snapshot evidence
  are read in one SQLite read-only transaction to avoid mixed-version responses.
- A latest rejected partial/stale/error envelope returns outer `status=partial`
  and `serving_prior_complete_after_rejection=true` with the older complete
  report. It is not silently labelled a fresh successful run. CLI exit code is 3.
  A first partial attempt returns no complete report or snapshot ID.
- `latest_attempt_basis=imported_envelopes_only_not_worker_health`: an abrupt
  source exception before capture cannot be imported, and does not change the
  catalog. The local bounded worker now records this in its private RunLog (see
  [worker/export boundary](bounded-worker-publication.md)). This catalog
  cannot promise worker health or observe unrecorded failures.
- Every receipt retains per-source `fetched_at`, source day boundary, coverage,
  `complete_through`, cutoff basis, missing dates and truncation. Import time is
  not data-through. Values are supplied by imported evidence and have not been
  reverified against Google or the inquiry server during a query. No GA4 finality,
  inquiry timezone or freshness-SLA guarantee is invented here.
- `daily`/`on_demand` are invocation labels; no scheduler runs. Source fetching is
  separate from import, and neither is available from query HTTP.
- Comparisons remain observational. Ordinary traffic is not relabelled target
  visitors without explicit selection. Existing continuous September-edit
  reports remain provisional in the fixed-case catalog. They are not promoted
  into exact-date confirmed batches, nor automatically refreshed by this stage.

## Local acceptance and remaining deployment gate

The tests run synthetic capture -> import -> discovery -> last complete query ->
historical query from urllib, independent http.client, and a separate CLI process;
then restart the local service and compare responses. They also cover concurrent
refresh writes/reads, no-head cases, partial/stale/failed source handling, estimated
GA4 finality, unverified inquiry day boundary, corruption, pagination, denied
requests and coexistence with provisional continuous-case evidence. Source
fixtures contain no customers; production is never contacted.

The bounded worker, source metadata bridge, private failure log, provisional
continuous-case builder and validated export/publish are now implemented locally;
see [their acceptance and limitations](bounded-worker-publication.md).
Live source compatibility/finality observations, actual daily scheduling,
reviewed launcher/handoff integration, Linux permission/sandbox validation,
cross-machine access and production acceptance remain unvalidated. Before any deployment, review a
concrete manifest for a separate source worker and read-only evidence publication
path. Keep the existing service/release and fixed-case database available for
rollback. Do not grant the query identity source credentials or write access,
or modify the old sync timers as an incidental step.
