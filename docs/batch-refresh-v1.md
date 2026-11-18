# Local aggregate refresh slice (2026-11-03)

This stage adds offline orchestration, not a production refresh deployment.
The original checkout, existing source files, service, credentials and Feishu are unchanged.
No scheduler is installed, and no real batch or intervention date is confirmed by this demo.

## Boundary and contract

`analysis.refresh.prepare` calls existing snapshot `capture`, using an injected
`FixtureReader` or `AdapterReader`. The latter already wraps the existing GA4,
GSC and inquiry adapters; it does not construct credential clients. GA4 cohort
users remain interval-deduplicated. Import consumes only the aggregate envelope;
it cannot fetch a source. HTTP exposes neither prepare nor import.

The `batch-refresh/v1` envelope has exactly `contract`, `job_id`, `run_id`,
`mode`, `snapshot`. The snapshot is the existing validated `batch-snapshot/v1`.
`job_id` hashes the confirmed batch/revision, exact query/windows/segments and
sample flag. A changed window is a different job. `run_id` is an immutable retry
key within that job. `daily` and `on_demand` label invocation intent, not finality.
Daily callers can use an ISO-day run ID; on-demand callers use a unique explicit ID.
An exact retry returns the original receipt. Different evidence with the same
run key is rejected; corrected fetches need a new run key.

No global “latest date” is invented. Receipts retain each source/window's
`fetched_at`, source day boundary, coverage, missing dates, `complete_through`,
cutoff basis and truncation. Capture time and import time are separate from
data-through. `configured_lag` is an estimate and cannot establish observed
source finality; the production runner's conservative cutoff is not proof of
Google's actual availability. This stage does not impose a business freshness
SLA: an old but complete source remains visibly old.

## Promotion and failure behavior

- `promoted`: all comparison sources pass the existing evidence/metric checks.
- `rejected_partial`: errors, unavailable sources, missing dates, truncation,
  missing metrics, unverified day boundaries or estimated finality remain visible;
  missing values are not turned into zeros. A first partial attempt has no head.
- `rejected_stale`: an older/equal capture, regressing source fetch timestamp or
  regressing per-scope data-through cannot replace the last complete snapshot.
- `unchanged`: the already-selected snapshot ID is imported under another run ID.
- Invalid/oversize/wrong-version/cross-site/extra-field evidence fails validation
  before a store is created. Thrown reader errors do not produce a capture or
  touch the store; the CLI returns a safe error/exit 2. The bounded worker now
  retains preflight/capture failures in its private RunLog, separately from the
  query catalog; it does not pretend the old head was refreshed.

Snapshots, immutable run receipts and the head are written in one SQLite
transaction. Failed transactions roll back all three. Valid partial/stale
evidence is retained for inspection, but the last complete snapshot is preserved.
Concurrent retries serialize; receipts are immutable historical facts. Use
`status` for the current head rather than treating an old receipt as current.
There is no retention/deletion worker in this slice.

`RefreshStore.status` is read-only and shows both the latest imported attempt
and last complete receipt. Existing `SnapshotStore` and `service --snapshots`
can read the database without new write operations, credentials or schema migration.
Queries remain pinned by snapshot ID. The service cannot choose a local path
or refresh sources from an HTTP request.

## Reproducible synthetic demonstration

From the worktree, using the existing development Python and `PYTHONPATH=src`:

```sh
python scripts/demo_batch_analysis.py --output-dir /tmp/analytics-refresh-example
python -m website_analytics.analysis.refresh prepare-sample \
  --request examples/batch-analysis/query.json \
  --batches /tmp/analytics-refresh-example/synthetic-batches.sqlite \
  --sample-evidence examples/batch-analysis/evidence.json \
  --run-id daily-2026-08-21 --mode daily --as-of 2026-08-21T00:00:00Z \
  --output /tmp/analytics-refresh-example/envelope.json
python -m website_analytics.analysis.refresh import \
  --input /tmp/analytics-refresh-example/envelope.json \
  --store /tmp/analytics-refresh-example/evidence.sqlite
```

Preparation writes exclusively and refuses to overwrite an existing envelope.
Repeat **import** with the same envelope for an idempotent retry. Use the returned
`job_id` with `refresh status --store ... --job-id ...`; use `snapshot_id` with
the existing `analysis replay --snapshots ... --snapshot-id ...` CLI, or serve
locally with `python -m website_analytics.analysis.service --snapshots
/tmp/analytics-refresh-example/evidence.sqlite --port 8876`. The service binds
loopback only. Its existing agent client can replay with `--port 8876`.
All these example data are synthetic, not actual user-confirmed SEO outcomes.

## Bounded production proposal — not executed

1. Agree explicit refresh jobs/windows and source-specific finality evidence;
   do not auto-extend before/after windows or assume continuous September edits
   have an exact completion date. Decide how to present incomplete/latest attempts.
2. Implement a separate source worker under the existing approved source identity,
   injecting existing adapters and reviewed classification. No credentials or SDKs
   enter the `analyticsquery` service. First test one limited aggregate dry-run;
   no Feishu writes or old synchronization changes.
3. Review promotion and publication permissions separately. A future deployment
   must resolve the current query identity's read-only SQLite/file permissions:
   test a narrowly owned writer/staging/export arrangement (or SQLite sidecar
   readability) before choosing live database sharing. Do not silently grant the
   query identity write access. Keep the existing fixed-case database untouched.
4. Only after approval, deploy worker code, an isolated evidence store and one
   daily timer plus an administrative on-demand entry. Query HTTP stays read-only.
   Start with shadow outputs and reconcile source counts/date coverage before
   selecting any new snapshot for colleagues. Service code/identity/access and
   scheduling each require concrete deployment approval; none is covered here.
5. Roll back by stopping only the new worker/schedule and reselecting the previous
   complete snapshot. Never delete old snapshots or alter old sync/Feishu records.

The local query-side catalog for discovering heads and historical versions is
now implemented; see [agent usage and acceptance](refresh-catalog-agent.md).
The [bounded worker stage](bounded-worker-publication.md) now implements local
explicit registered-source wiring, provisional refresh and export/publish; these
were tested only with injected fixtures, not real credentials. Remaining gaps:
live compatibility/finality observations, reviewed launcher/handoff and daily
scheduling, automatic history retention, cross-machine acceptance and deployment.
Existing provisional
continuous-case reports use `CaseStore`; this stage does **not** route them through
the exact-publication `ConfirmedBatch` model or relabel their interpretation.
The separate local builder preserves approximate start, unknown end and original
URL case without converting these records to ConfirmedBatch. It still needs live
acceptance. This stage must not be presented as a live
daily refresh of the real four-page case or a complete V1.

## Acceptance

Tests cover offline CLI capture/import/status, existing adapter injection with
fake responses, UV semantics, concurrent cold/duplicate imports, immutable run
conflicts, errors/truncation/missing/estimated cutoffs, stale/future captures,
rollback fault injection, restart replay and two independent loopback clients.
Existing HTTP rejects refresh/import/fetch; production is never contacted.
