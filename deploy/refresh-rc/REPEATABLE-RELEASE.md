# Repeatable refresh fixed-launcher release — local candidate only

This is a NEW immutable release candidate, not the installed
rc-667b38ed4adb25a4. Its external receipt records manifest/archive SHA, release
ID, identical dependency locks, target wheels and test evidence. This stage
uses only fixtures and existing local binary wheels; no server connection,
source query, service/timer/access/grant/Feishu change, commit or push occurs.
Linux native execution of this NEW launcher/sandbox remains unvalidated.

## Fixed entry contract

Three argument-free scripts, release-local Python `-I -B`:

- refresh-daily.py: selection-calendar daily key, bounded rolling request.
- refresh-on-demand.py: exact root-owned manual-request.json key; same key
  replays original intervals even after the calendar week changes. A new key
  means an explicitly new administrative invocation, not an implicit retry.
- publish-repeatable.py: no source calls; validate/drain up to16 pending private
  handoffs, inspect unknown publication target SHA before resolving, retain all
  outcomes/history, and repair a completed receipt/status without re-fetching.

No caller can supply a source, URL, SQL, remote command, path, calendar, target
country or runtime override. Startup values are root-owned public config. The
fixed task is the original four exact-case paths plus the independent lowercase
variant; source registration/intervention/runtime digests are pinned.

The launcher preflights the full release, strict dependency versions, exact
Python patch, Linux x86_64 identity, owner/mode, site/schema and task hashes
before loading an environment or reader. Production forbids fixture injection.
It references the existing ADC and only the named inquiry environment value in
memory; it creates/saves no credential. Publisher uses only the YAML-only venv,
no source environment, network or SDK factory.

Source/publisher roles do not merge. The source's private capture/export is
never a public write. The administrator separately starts the publisher after
source termination, INCLUDING a failed source, so sanitized failure/pending
state can be published while previous evidence stays intact. Templates include
NO timer, enable, automatic OnSuccess/OnFailure chain or source restart. A
future automatic chain and bounded catch-up policy need explicit design and
approval; these files do not silently enable daily delivery. Pending queue
items are durable; remaining_unattempted/exit75 requires another approved
publisher invocation, not another source fetch.

Before real source setup the launcher checks old sync is inactive/failed. If
active, it records old_sync_active_no_fetch without loading credentials and
returns failed; the outcome is immutable. This is a best-effort overlap check,
not a shared lock with the unmodified old runner. It does not stop/kill or alter
old tasks, wait indefinitely, start OAuth, or automatically retry. A reviewed
new manual key may be used after the old job is idle. Scheduled deferral/retry
policy is still a user decision, separate from enabling a timer.

## Exact proposed objects (do not execute without approval)

Let `<RC>` be `rc-` plus the external receipt's manifest digest first16 hex.

| Object | Identity / rights / purpose |
| --- | --- |
| `/opt/website-analytics-refresh/releases/<RC>/` | root:websiteanalytics, dirs0750, payload0640; independent worker/query venvs, offline hash-locked wheels |
| `/opt/website-analytics-query/releases/<RC>/` | root:analyticsquery, dirs0750/payload0640; separate YAML-only query venv |
| `/etc/website-analytics-refresh/<RC>/` | root:websiteanalytics0750; loop.json, sites.yaml, task.json, manual-request.json0640; public references only |
| `/var/lib/website-analytics-refresh/generations/<RC>/worker/` | websiteanalytics0700; history.sqlite0600, runs0700 and immutable per-run files0600/candidates0400 |
| `/var/lib/website-analytics-query/live/` | existing root:analyticsquery0750; cases.sqlite and refresh-status.json0440; root journal/history0700 |
| source daily unit | new website-analytics-refresh-daily-v2.service, websiteanalytics, no capabilities, only generation worker writable, 600s/512MiB, Restart=no |
| source manual unit | new website-analytics-refresh-manual-v2.service, same restrictions; manual key file has no parameters beyond contract/key |
| publisher unit | new website-analytics-refresh-publish-v2.service, root, CAP_CHOWN/CAP_DAC_READ_SEARCH only, no network, only live directory writable, 90s/256MiB |
| query switch | update only approved 20-scoped-evidence.conf to new absolute query release; retain source-path denial in30-source-isolation.conf and loopback8765 |

Do not recursively chmod venv executables0640; retain necessary execute bits.
Do not reuse/upgrade the old venv or modify old source current/config, timers,
Google publishing/grants/ADC, SSH, accounts/groups, firewall/Nginx, Feishu or
website. Source admission remains fixed_registered_source for
v2_genemedi_net.contacts and explicit GeneMedi hosts, not a dedication claim.
The current production property376561184 must match; no TarMart scope is added.

Create a NEW private generation using validated `refresh_loop.initialize` from
ACTUAL current live evidence, preserving every payload/rowid. Then give its
private files/directories to the existing source UID. Do not assume the local
old candidate is still the current server history. Check SHA and collisions
before writes; stop on change and re-review, never overwrite blindly.

Generate runtime-provenance/v2 from the verified new release and public sites
config under the actual target Python/platform WITHOUT client construction.
Generate task.json using the validated seed intervention, that runtime and
registration_ids(site). Freeze task/source-config hashes into loop.json. Actual
server fingerprints cannot be fabricated on Mac; these are bounded deployment
preparation steps needing approval. Use capture_mode=server_shadow for the
first controlled validation; retain that meaning in saved evidence. A later
production_worker mode changes registration/key namespace and requires review.
Generation key hashes bind registration digest as well as task/request key.
Do not edit registration/runtime/mode in place after runs exist: the launcher
rejects mixed registration before credential loading. Use a newly frozen
release/private generation and re-seed actual live history for a rollover.

## Ordered first-server approval scope (proposal)

1. Reconcile identities, current installed release/live SHA+versions, old unit/
   timer/config hashes, ADC metadata and disk. Confirm Python3.12.3/Linuxx86_64
   or stop for review. Existing access only, no new grant.
2. Install only the new immutable releases, offline environments, versioned
   public config/private seed and three reviewed v2 units. Existing v1 units,
   current pointers and live evidence are preserved; no enable/timer.
3. Approve a SINGLE bounded manual source invocation (original four pages plus
   variant, at most14 days: last completed week and labelled current open week;
   exact computed dates shown before execution). Start only the manual-v2 unit.
   If old sync is active, stop this new attempt without source use; do not kill
   it or silently substitute a wider run. No all-site or customer-row fetch.
4. Explicitly start publisher-v2 after inspecting source outcome. It validates
   dynamic candidate SHA/schema/all added rows/runtime/source/task scope,
   preserves history, CAS-publishes aggregate data/health. Failed capture does
   not replace evidence. Post-replace uncertainty requires target inspection.
5. Only after candidate acceptance install approved new query drop-in and
   restart query alone. Verify status/data SHA match, partial/open-period labels,
   old pinned responses, two clients and restart, source credential denial,
   readonly DB, rejected operations and only127.0.0.1:8765. This is a short query
   interruption; prepare prior drop-in/hash as rollback.
6. Do NOT enable daily/on-demand colleague triggers. Daily time (candidate08:30
   Asia/Shanghai), retry/defer and automatic publish/catch-up behavior require
   separate approval. Boss Windows/WorkBuddy tunnel identity/public key/SSH
   changes are separate persistent-access approvals; Library client pack has
   no key or account setup and remains saved-data-only until updated.

## Data and capacity gates

Request success, coverage, maturity and quality are separate. GA4 lag/absent
sampling do not prove finality; unsupported truncation remains unknown. GSC
all-state metadata binds exact page scope, and missing firstIncompleteDate is
not an invented cutoff. Inquiry DATE/writer calendar and physical dedication
remain unknown. Open periods are not full-week totals or a stable endpoint;
continuous early-September changes and unknown end/stability stay provisional,
not a confirmed event or causal effect. Cohort UV is never a sum of page UV.

Active generation capacity512 evidence rows/runs, max32MiB export and64-version
observation scan remain finite and visible. Pagination retains pinned history;
no automatic deletion or archive is claimed. Review capacity/archival/restore
and disk budget before unattended long-term use. A full/unknown/failed state
must preserve last evidence and be visible; HTTP200 alone is not fresh data.

## Rollback

Before query switch, stop only a new v2 invocation if necessary; retain private
logs/candidates. If publication is uncertain, inspect before repeating. Keep
original and published generations; normal monotonic publication rejects
history removal/regression. Known failed publication is not automatically
rewritten: review the durable journal before a new administrative attempt.

After a query switch, stop query alone, restore recorded prior20-scoped-evidence
content (30-source-isolation remains valid if unchanged), daemon-reload and
start query with its prior code/evidence selection. For an evidence-generation
rollback use rollback_target's validated `.history` file as an explicit reader
selection; do not delete/rewrite live history or add an HTTP rollback action.
No old sync/timer or credentials need restoration because none changed.
