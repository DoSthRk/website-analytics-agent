# Repeatable bounded refresh — local implementation, 2026-11-04

This stage changes local code/tests only. It makes no real source request,
server connection, credential/access/schedule change, deployment, Feishu write,
Git commit or push. Installed RC rc-667b38ed4adb25a4 and its archive stay unchanged.
Daily/on_demand are invocation labels, not a working scheduler.

## Modules and usable local API

`analysis.refresh_loop` provides:

- `registration(seed, runtime, registration_ids(site))`: strict operator-owned
  task contract, pinned original four URLs/case, separate lowercase variant,
  intervention digest, runtime digest and source registration IDs. No query
  caller chooses a source, URL, file, SQL or command. Current real case remains
  provisional; no confirmation is generated.
- `initialize(private, seed_db)`: validated append-only history seed into a new
  private generation; refuses an existing destination.
- `produce(private, registration, runtime, request, request_key=..., mode=...,
  reader_factory=..., clock=...)`: injected existing AdapterReader, common
  collection/build logic, per-run immutable intent/capture/outcome and private
  export. Source ID mismatch fails before read calls. The reader factory is
  trusted startup wiring, never HTTP input. This module creates no credential
  client/environment, scheduler or network service.
- `recover_capture(...)`: explicit recovery from validated saved capture; no
  refetch. Pre-capture crashes require explicit `abandon_capture(...)` or review.
  A duplicate key returns the original outcome; changed input under that key is
  rejected. Pending unknown runs block another capture until inspected.
- `publish_run(private, public, registration, request_key=...)`: separate trusted
  publisher. Dynamic SHA handoff, all-added-row task validation, target CAS,
  immutable publication journal and history-preserving atomic replace. No source
  factory or credential use. Failed source requests do not publish; successful
  transport with incomplete evidence may publish only as explicit provisional.
- `rollback_target(...)`: verify a retained prior generation for an administrator
  to select as a reader rollback. It does not overwrite live, remove evidence,
  restart a service or expose a path through HTTP.

`rolling_request(reg, as_of=...)` constructs the last completed week plus the
labelled current open week, or the last two completed weeks under an explicit
policy. `daily_key(as_of)` derives a stable selection-calendar day key; neither
installs a scheduler.

Run keys are explicit safe lowercase identifiers such as daily-2026-11-04 or
manual-2026-11-04-a. The same key and mode coalesce; daily and manual invocations
share the producer lock. Publishers have their own lock. Actual per-run source
fetched-at clocks advance through collection; capture is timestamped after the
reads, not frozen before network work. `clock` is injected only for local tests.

A minimal developer invocation uses the synthetic seed/readers in
`tests/test_repeatable_refresh.py`:

```sh
python -m pytest -q tests/test_repeatable_refresh.py tests/test_windows_read_client.py
```

These are synthetic fixtures, not a user-confirmed optimization batch. This
stage intentionally adds no production/auth/scheduling command to the client.

## Intervals, interpretation and source semantics

Legacy `bounded-worker/v1` / seo-weekly-read/1 retain completed-week behavior and
old pinned report digests. `bounded-worker/v2` accepts one or two contiguous
Monday-start periods, <=14 total days, explicit America/Los_Angeles selection
calendar, and a labelled open current week; future dates, gaps, overlapping or
mislabelled windows fail. The selection calendar is not proof of source-day
alignment. The v2 packet/report labels open periods and uses GSC dataState=all;
legacy reads retain final. Additional metadata retains exact scope/provenance.

GA4 users come from exact cohort interval queries, never sums of page/week UV.
Unknown/empty results stay unknown. Inquiry DATE calendar, physical dedication
and provider finality are not invented. GA4 SDK-unsupported truncation reasons
remain unknown. Continuous early-September edits, unknown end/stability and
separate URL case survive every revision; this is not causal attribution or
qualified-lead scoring. Target countries/channels have no business default.

## Failure status and read-only query integration

Producer/publisher state is separate from public evidence. Failed fetches and
pre-capture unknown outcomes can be projected to a fixed root-published
refresh-status.json without replacing the last evidence. Error text, response
bodies, customer details and secrets are discarded; only bounded diagnostic
codes are published. A pending capture has finished_at=null.

`StatusReader` and predefined `refresh_status` query expose last attempt,
last known published observation, source windows/calendars/observed cutoffs,
plus SHA agreement between status and selected evidence. If atomic evidence
replacement precedes status publication, the status is visibly inconsistent/
partial until repaired. Repeating a completed publication repairs a missing
status without refetching or replacing evidence. Publication uncertainty checks
the actual target SHA before resolving; it never blindly replays a write.

Operator startup uses `service --cases ... --refresh-status <fixed status file>`.
The fixed query launcher enables this option only when that known file exists.
Agents can use `client --operation refresh_status --task-id purprox-refresh`.
Requests cannot supply the status path or invoke the producer/publisher.
The currently installed server release has not gained this local-only API.

## History and capacity (bounded, no deletion)

Case history now permits 512 total active-generation records (instead of32 per
case), while retaining existing payload/SQLite budgets. `case_versions` accepts
limit1–32 and before_sequence. It returns stable descending rowid cursors;
old <=32-version responses without pagination keep their previous shape.
`list_cases` pages long histories instead of embedding everything. Pinned IDs
remain readable. Observation discovery examines at most64 newest captured
versions, labels that bound and retains pageable older versions; it does not
claim to have searched all history for a strict-complete version.

Exports remain <=32MiB and <=512 table rows. Run-generation budget512 and
private log limits remain explicit. Reaching a bound fails before another fetch
where possible and never purges history. Previous published generations remain
in `.history`; no automatic deletion or infinite retention is claimed. A future
archive/index/restore policy and disk-capacity threshold must be reviewed before
long-term unattended operation. The new bound solves the immediate32-version
failure but is not unlimited storage.

## Tested release path — proposal only

1. Freeze a NEW release/lock/content manifest after review; do not overwrite the
   installed immutable RC. Preserve all live history, URLs and interpretation.
2. Prepare fixed launcher wiring under existing websiteanalytics with root-owned
   registration/source config and per-run directories. Existing credential
   loading remains in the reviewed worker launcher, not in query or handoffs.
   This stage tests library orchestration; that production wrapper and exact
   public source-config hashes must be reviewed before server execution.
3. Replace the static-one-SHA publisher behavior only in the new reviewed
   release. Its bounded receipt/journal/health paths and writable sandbox list
   must be explicit; keep no network, no source SDK/env, current capability
   ceiling and root-owned configuration. Do not let the producer edit root
   profiles or give analyticsquery source/spool write rights.
4. Approve one bounded manual source run/publication acceptance before any timer.
   Verify real metadata/time-order, safe pending/failed state, CAS, two clients
   and restoration of the previous reader generation. A partial observation
   remains partial; provider finality is not an acceptance fiction.
5. Only then approve a new daily timer (candidate08:30 Asia/Shanghai), bounded
   timeouts/quota/defer policy, and administrator on-demand invocation. Do not
   change old timers. The hour and deferral policy are still user decisions.
6. Colleague pilot is separate: boss on Windows+WorkBuddy, initially read-only.
   `clients/windows-workbuddy` has no keys or account setup. Named-account/key,
   SSH restriction and reload require exact action-time approval. No root key,
   source credential, public8765 listener or colleague refresh privilege.

Failure restores only the query reader configuration to the recorded prior
release/database generation; preserve new/private evidence and logs for review.
No worker scheduling, source grant, access or live configuration is changed by
this document or the local test suite.
