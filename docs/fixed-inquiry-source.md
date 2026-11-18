# Fixed registered inquiry source

`fixed_registered_source` supports a verified existing source without claiming
physical database dedication. Existing `dedicated_database` configurations keep
their prior behavior; do not change their attestation to make an unverified
source pass.

The fixed contract binds an existing credential reference to an exact database,
the hardcoded `contacts` table, and explicit approved GeneMedi hosts:

```yaml
inquiry_source:
  kind: legacy_contacts_mysql
  credential_env: WEBSITE_ANALYTICS_EXISTING_DSN
  site_scope: fixed_registered_source
  site_scope_evidence: "Dated schema/grant and aggregate-origin observation; not proof of exclusive writers or a known submission day boundary."
  registered_database: v2_genemedi_net
  registered_table: contacts
  allowed_hosts: [genemedi.net, www.genemedi.net]
```

This is an illustrative fragment, not an instruction to edit production. The
credential value is never part of the configuration or evidence. Hostnames must
also belong to the enclosing site's registered domains. The factory validates
the selected DSN database before connecting, and the adapter checks `SELECT
DATABASE()` before executing fixed aggregate queries. Database/table/host
mismatches fail closed. SQL is not supplied by callers. No customer rows are
returned; the existing adapter uses grouped submission counts and page URLs.

Foreign origins fail the entire source instead of silently dropping records.
Missing, relative or invalid attribution is preserved as unknown, not assigned
to GeneMedi or a batch. Unattributed counts refer to the fixed database scope,
not a verified website total. Unknown/truncated attribution makes evidence
partial; a successful SQL response does not establish calendar completeness.
PageURL is an attribution field, not proof of the real submitting origin.
Inquiry submission-day timezone remains unverified. Aggregate UVs must still
come from the filtered GA4 interval request; page UVs are not added together.

## November 3 bounded trial

The one approved retry used temporary code and configuration, the existing
readonly source identity, the original four exact-case URLs and the separate
lowercase variant. Windows were October 19–25 and October 26–November 1, 2026.
They are completed calendar weeks, not evidence of source finality.

Standard Google refresh returned HTTP 200 without changing ADC. Request statuses
for the first week succeeded for GA4, GSC and inquiry. The second week's
four-page GSC request returned an error; the separate lowercase GSC request and
GA4/inquiry requests returned successful statuses. Successful requests alone do
not establish source completeness, valid zeroes or a publishable result.

Strict case assembly then failed. No fresh validated case, numeric comparison
or production evidence was published. The generic GSC error did not preserve a
safe reason code, so its API/network cause remains unknown. A deterministic
real-clock ordering bug was subsequently reproduced and fixed locally: create
the unavailable-availability marker before recording the enclosing capture
time. It was not retried online, and the unretained failed packet prevents
claiming it was the only online assembly cause.

The temporary remote directory was removed and the process was confirmed
absent. Protected production file hashes, release links and ADC metadata were
unchanged; original timers and query service remained active/enabled. No grant,
timer, production configuration, Feishu or website changes were made. Local
evidence receipts are under `validation/isolated-retry-20261103/` in the task
workspace (outside this worktree).

Before a further bounded live validation, retain safe per-source error codes
and failed-capture metadata without response bodies or secrets. Another live
trial is not included in the exhausted one-retry instruction. Formal refresh
deployment remains unapproved; source finality, the inquiry calendar and a
successful validated aggregate packet remain outstanding acceptance gates.

## Follow-up diagnostics

`analysis.diagnostics.diagnostic_notes` returns only fixed error categories,
integer HTTP error statuses, allowlisted OAuth codes and known contract reason
codes. It does not stringify exceptions or inspect response bodies, request
URLs, headers or payloads. `AdapterReader` retains these codes in evidence notes;
error evidence still has no metrics and cannot become a complete snapshot.
Unknown errors remain unknown. A timeout/rate-limit category is not proof of a
historical root cause.

The follow-up local suite passed 594 tests with 12 existing renderer skips.
A proposed minimal diagnostic would query only the failing four-page GSC
aggregate for October 26–November 1 once, then run the collector over that
cached result with all other sources/variants explicitly unavailable. The
execution approval review rejected this remote action before launch because
the earlier one-retry limit was exhausted and a fresh user approval was not
present in the reviewed record. No new source request or remote upload ran.
Read-only postflight confirmed the proposed directory absent, no remaining
trial directories, unchanged protected configuration hashes and active/enabled
original timers/query service. The failed GSC request's historical reason and
the clock fix's live integration therefore remain unverified.

### Subsequently approved single diagnostic

After specific user approval, one GSC query ran on November 3, 08:38 UTC for
October 26–November 1 and the four original paths only. It returned one
untruncated aggregate row: 6 clicks, 62 impressions, CTR 0.0967741935483871,
average position 26.06451612903226. The lowercase variant was kept distinct and
not queried again. GA4 and inquiry query counts were zero. Standard access-token
refresh returned HTTP 200; ADC bytes and metadata were unchanged. The previous
GSC failure did not reproduce; its historical reason remains unknown.

The remote cached collector check rejected the diagnostic harness's runtime
metadata: it included hashes for two analysis modules outside the strict
public-source-hash whitelist. This was a harness error, not a source request or
time-order failure. A local replay reproduced that exact rejection, then used
the permitted GSC adapter hash inside the packet and retained all code hashes
in the separate trial manifest. Strict case build and default-clock ordering
passed using the captured real aggregate, without another source request. All
uncollected sources, page contributions and the variant remained unavailable.
The record stayed provisional and was not stored in the case catalog or
published. The corrected harness was not rerun remotely.

GSC source-day boundary, coverage and complete-through fields remain unverified
in this capture. The assessment is partial and comparison-ineligible despite
the successful request; there is no new performance or causal conclusion.
The trial process exited and its temporary directory was removed. At 08:40 UTC,
protected production hashes, ADC metadata and original timer/service states
matched the fresh preflight. Evidence receipts are the `approved-*` files and
`local-cached-replay-result.json` in the task's diagnostic validation directory.
