# Scoped completeness and latest observations (local development)

`scoped-completeness/v1` separates request success, coverage, calendar, processing
maturity and quality. It is a deterministic projection of saved evidence, never
a fetch operation. No production service, credentials or schedules were changed.

The detail, trend and page contribution responses of worker-backed cases expose
the same `scoped_completeness`. Snapshot replay, pinned refresh and legacy latest
refresh responses also expose it. The pinned report and its digest are unchanged.
Legacy `data_completeness` / `refresh_latest` retain their v1 meanings for
compatibility; use the new projection for strict provider-completeness claims.

## Read operations

```json
{"contract":"batch-service/v1","operation":"case_observations","case_id":"purprox-seo-four-pages"}
```

Groups are separated by exact URL identities, source registration, intervention
model and date windows. Each has a `latest_provisional_observation` pointer, a
separate `strict_complete` pointer and the latest saved attempt. Capture time,
not import order, selects versions. Equal-time different versions are exposed as
conflicts and require explicit pinning. Failed worker records without metrics
do not replace an older observed-data pointer. Historical IDs remain queryable.

```sh
python -m website_analytics.analysis.client --operation case_observations \
  --case-id purprox-seo-four-pages --port 8876
```

```json
{"contract":"batch-service/v1","operation":"refresh_observed","job_id":"<64 lowercase hex>"}
```

This refresh view serves the latest saved observed metrics independently of a
strict complete pointer, including valid partial data. Failed imports cannot
silently become complete. `legacy_v1_complete_head` is explicitly the old
contract's head, not a new finality attestation. The search loads at most 64
recent capture versions; truncation of that search is explicit. Pre-capture
failures remain in worker logs, not fabricated as query catalog entries.

Both operations use the existing loopback-only service, strict operation/field
allowlists and existing agent client. They accept no SQL, path, command, URL or
source credentials. Neither starts source fetching or scheduling. They have not
been deployed or connected to colleagues.

## Observation injection and provenance

`case_refresh.collect/run(..., observations=[...])` accepts bounded
`source-observation/v1` records from an operator's source-reading phase. The
reader captures non-secret `worker_source_ids`; a credential-free query cannot
create them or import evidence. Registrations hash only the registered property,
approved hosts, fixed inquiry schema/table/scope and exact-filter contract, never
a credential. Existing dedicated-database behavior is unchanged; a registration
hash is not proof of physical database dedication.

Each observation has exactly these fields:

```text
contract, source, source_id, scope, fetched_at,
calendar: {name, basis}, dates_observed, maturity, quality
```

Scope is the existing site/start/end/pages/countries/channels document. Paths
retain exact case; observation windows are bounded to 14 days. Stored case
observations cannot refer to pages outside that case. Source registration,
page/segment identity and range containment must match before metadata applies.
Overlapping eligible proofs are ambiguous, not resolved by arbitrary order.
The response retains source-fetch time, observation-fetch time, observation
digest and whether the observation precedes the metric request. No freshness
SLA is invented from those timestamps.

Source-specific rules:

- **GSC**: calendar basis is the documented API contract
  `America/Los_Angeles`. Maturity fields are `basis:gsc_api_metadata`,
  `data_state:all|final`, `first_incomplete_date:<date|null>`. A present boundary
  applies only inside its matching observation range. Absent metadata, final-only
  rows or a latest returned date do not create a watermark. Returned date rows
  do not prove all search data; top-row/privacy limitations remain explicit.
- **GA4**: calendar basis can be `api_response`. Maturity accepts only unknown or
  `configured_lag` with `estimated_through` and a policy version. Unsampled data
  and elapsed days never create provider finality. Quality supports nullable
  sampling/threshold/other-row/restriction/truncation flags. Unsupported SDK
  `dataTruncationReasons` capability must remain unknown, never `truncated:false`.
- **Inquiry**: the stored DATE calendar remains unknown until writer evidence
  exists. `stored_date_observation.max_record_date` is not a data-through date.
  A mutable database observation does not establish finalized ingestion. Missing
  attribution and truncation remain explicit; counts are not qualified leads.

Unknown fields, raw response bodies, arbitrary text, future observations and
duplicate proofs are rejected. Unsupported contracts fail closed. Live
availability-fetch/SDK-capability collection is now wired to actual adapter
interfaces and the bounded worker locally, tested with injected clients and
saved sanitized evidence. Actual-server validation is still pending. See
[adapter integration and release proposal](adapter-metadata-and-deployment.md).
No SDK dependency, production environment or grant was changed.

## Meaning of strict complete

Strict complete requires successful requests, actual metric fields, verified
calendar/range coverage, provider-processing evidence and sufficient quality
evidence. Current real GA4/inquiry sources do not provide a defensible final
watermark; GSC has a non-exhaustive API limitation. Therefore real strict-complete
pointers may legitimately be null. Provisional observations remain usable and
visible without weakening that gate. Missing rows stay unknown, not zeroes.

This does not confirm a draft, choose a target country, establish an intervention
completion date or identify a causal effect. Continuous September-onward edits,
unknown stage dates and the unconfirmed October completion estimate are retained.
Legacy records without observations replay unchanged; new evidence is appended
as new versions, never retroactively promoted or rewritten.
