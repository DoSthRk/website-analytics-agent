For the NEW repeatable-launcher candidate, see [REPEATABLE-RELEASE.md](REPEATABLE-RELEASE.md).
The original single-shot instructions below are retained as historical v1 reference.

# Frozen refresh release candidate — LOCAL, not installed

November 4, 2026. This directory defines a manual source pilot and an explicitly
reviewed evidence publication/query switch. It installs no scheduler. Daily and
on-demand scheduling remain a separate release. No server was contacted while
preparing this candidate. Actual deployment commands below are a proposal and
must not be executed without the specific server-change/execution approval.

## What is frozen

- Deterministic gzip/PAX bundle, sorted public Python/config/template files,
  zero archive timestamps, exact file SHA256 and no local paths or credentials.
- `analytics-release/v1` manifest. Release ID is `rc-` plus the first 16 hex
  digits of the **manifest** SHA256, not the archive SHA or a Git commit. An
  external `release-receipt.json` supplies both full digests; do not put the
  archive's own digest into a file contained in that archive.
- Target: CPython 3.12, Linux x86_64, manylinux baseline <=glibc2.28. Worker locks
  the last observed server's five main library versions, PySocks and all 28
  resolved dependencies. Query locks only PyYAML; it needs no Google/MySQL SDK.
  Both locks contain exact versions and SHA256. Wheels are from official PyPI,
  binary-only; resolver cutoff is 2026-11-04. This is a **new** independent
  environment, not a freeze of every installed production transitive version.
- `runtime-provenance/v2`: actual Python/platform, public adapter hashes,
  manifest/lock/public source-config hashes, explicit `local_fixture`,
  `server_shadow` or `production_worker`. This is capture provenance, not proof
  of Google authorization or data finality. Old v1 records replay unchanged.
- Three argument-free launchers. Use exact release-local Python `-I -B`:
  `source-pilot.py`, `publish-candidate.py`, `query.py`. Their source paths,
  profiles and identities are fixed; clients cannot pass a path, SQL, command,
  source endpoint or arbitrary URL. The source pilot is limited to the original
  four exact-case URLs and the separate lowercase variant, <=2 contiguous
  completed weeks. It refuses uncertain prior runs instead of auto-refetching.

Local fixture mode requires an explicitly injected reader. Production entries
reject fixture evidence and fail before source clients on a manifest, lock,
Python patch, input digest, identity or source-binding mismatch. No unsupported
adapter/SDK fallback or implicit dependency upgrade is allowed.

## Roles, files and rights

| Object | Required target / rights |
| --- | --- |
| Source code and offline wheels | `/opt/website-analytics-refresh/releases/<RC>/`, root:websiteanalytics, dirs0750, payload0640, no group/world write |
| Source venv | `<source release>/venv-worker`; same ownership; exact worker lock, bootstrap pip allowed |
| Publisher venv | `<source release>/venv-query`; query lock only, no source clients |
| Public fixed inputs | `/etc/website-analytics-refresh/{sites.yaml,request.json,metadata-windows.json,pilot.json,publish.json}`, root:websiteanalytics0640, directory0750 |
| Source identity | Existing websiteanalytics, no new account/grant/login/sudo |
| Existing source secrets | Reference the existing analytics.env named inquiry key, old service proxy whitelist and existing ADC **in memory only**. No secret in a new config, argument, receipt or bundle |
| Private state | `/var/lib/website-analytics-refresh/worker/`, websiteanalytics0700; seed/capture cases.sqlite and runs.sqlite0600; receipt0600 |
| Private export | `/var/lib/website-analytics-refresh/outbox/`, websiteanalytics0700; candidate cases.sqlite0400 |
| Source unit | New `/etc/systemd/system/website-analytics-refresh.service`, manual oneshot, Restart=no; no timer or enable |
| Publisher unit | New `/etc/systemd/system/website-analytics-refresh-publish.service`, fixed root oneshot, no network/source credentials, write access only to new live directory |
| Public evidence | `/var/lib/website-analytics-query/live/`, root:analyticsquery0750; cases.sqlite0440; atomic CAS publication with retained history |
| Query code/venv | `/opt/website-analytics-query/releases/<RC>/`, root:analyticsquery, dirs0750/payload0640; independent YAML-only venv-query |
| Query switch | New `/etc/systemd/system/website-analytics-query.service.d/20-scoped-evidence.conf`; keeps existing analyticsquery identity, sandbox and only127.0.0.1:8765 |

The source identity already has source permissions; this release does not claim
to narrow all permissions it already holds. The publisher is a reviewed
administrator action, not an HTTP operation. The query identity gets no ADC,
DSN, environment file, login or new access. No new colleague identity/key, SSH,
firewall, Nginx, public listener or Google grant is part of this candidate.

## A. Server validation / shadow pilot — requires execution approval

Before uploading, recheck actual server Python, service identities, disk,
existing files/collisions, current query release/database versions+hash, old
sync configuration and timer unit hashes/state/next plans, ADC nonsecret
owner/mode/hash. Do not output secrets or whole process environments. Record
these as a protected baseline. Last seen Python3.12.3 is not a current check.
An incompatible platform or missing existing source authorization is a stop,
not permission to upgrade or start OAuth.

The following are exact command patterns for a root deployment operator after
approval; `<RC>`, `<manifest SHA>` and paths must be replaced from the verified
external receipt. Do not execute placeholders. Use only a reviewed archive
uploaded to a **new** staging folder. Inspect archive paths before extraction;
reject absolute/traversal/symlink/device entries.

```sh
sha256sum /approved-staging/website-analytics-refresh-rc.tar.gz
install -d -o root -g websiteanalytics -m 0750 /opt/website-analytics-refresh/releases/<RC>
tar --no-same-owner -xzf /approved-staging/website-analytics-refresh-rc.tar.gz -C /opt/website-analytics-refresh/releases/<RC>
```

Resolve the existing Python interpreter path with
`readlink -f /opt/website-analytics/venv/bin/python`, check its version and retain
it as `<PYTHON>`. Create **three new venvs**, never reuse/modify the old one:
worker and publisher in the new source release; query in an independently
extracted copy of the same bundle under the new query release.

```sh
<PYTHON> -m venv /opt/website-analytics-refresh/releases/<RC>/venv-worker
/opt/website-analytics-refresh/releases/<RC>/venv-worker/bin/python -I -B -m pip --isolated install --no-index --find-links /opt/website-analytics-refresh/releases/<RC>/wheels --require-hashes --only-binary=:all: --no-deps -r /opt/website-analytics-refresh/releases/<RC>/deploy/refresh-rc/requirements-worker.lock
<PYTHON> -m venv /opt/website-analytics-refresh/releases/<RC>/venv-query
/opt/website-analytics-refresh/releases/<RC>/venv-query/bin/python -I -B -m pip --isolated install --no-index --find-links /opt/website-analytics-refresh/releases/<RC>/wheels --require-hashes --only-binary=:all: --no-deps -r /opt/website-analytics-refresh/releases/<RC>/deploy/refresh-rc/requirements-query.lock
```

The same `venv-query`/query-lock offline command applies to the separate query
release path. Do not install pytest/test locks on server. Preserve executable
bits of venv scripts/interpreters; make payload0640 and directories0750,
root-owned with the respective read group. Do not recursively chmod venv files
0640. Run `check_dependencies` with strict extras and `verify(...,
expected_manifest=<manifest SHA>)` using a reviewed bootstrap with `-I -B`;
no Python source client is constructed by these checks. Keep bytecode disabled
so integrity checks reject unexpected payload files rather than silently
accepting modified source.

Create only the new directories in the table. Export the **actual** old query
history with `analysis.publication.export`, validating original hash and all
old case/version rows. The private seed must preserve its rowids and old
versions; include newer saved aggregate evidence only after explicit review,
without overwriting/reordering existing rows. Never assume the local evidence
ID is already on server. Keep the old `/var/lib/website-analytics-query/cases.sqlite`
and query release untouched. Seed the private cases.sqlite0600.

Prepare the new public sites.yaml from verified existing registration, only
GeneMedi hosts, exact registered GA/GSC properties and the existing inquiry
credential **name**, never its value. Use `fixed_registered_source` bound to
`v2_genemedi_net.contacts` and explicit GeneMedi hosts. Do not edit old sites.yaml,
claim physical dedication, or assert an inquiry DATE timezone. Request only the
approved four URLs + lowercase variant and two explicitly approved weeks. The
metadata JSON is `{}` unless exact extra metadata dates were approved. Fill
pilot.json.example with the actual Python patch and computed input/seed hashes,
real prior evidence ID, unique run ID and `capture_mode: server_shadow`.

Render `@RELEASE_ID@` in the service template into a new reviewed file; inspect
it before installing. No shell wrapper interprets profile content.

```sh
install -o root -g root -m 0644 /approved-staging/website-analytics-refresh.service /etc/systemd/system/website-analytics-refresh.service
systemctl daemon-reload
systemctl start website-analytics-refresh.service
systemctl show website-analytics-refresh.service -p Result -p ExecMainStatus -p ActiveState
```

Do not enable, add a timer or invoke the original sync runner. Check bounded
receipts/run-log and candidate hash, case schema, exact URL case, non-summed
GA4 interval UV, per-source scope/timezone/coverage/maturity/quality, unknown
inquiry calendar and provisional interpretation. Success may honestly remain
partial. Test finite source timeout, sandbox writes, existing credential use
and real SDK metadata behavior; Mac tests do not prove Linux behavior.
Interruptions leave prior evidence intact and block auto-refetch. Inspect prior
run/candidate/receipt before any retry; use no automatic restart or OAuth.

## B. Publish and query switch — requires explicit release approval

This can be a conditional second phase in one concrete approval, but must wait
for stage A acceptance. Review candidate SHA and actual previous live SHA
(`null` means target absent, never overwrite permission). Fill publish.json.
Install the rendered publisher unit0644, daemon-reload, start only that oneshot.
It validates the candidate, rejects any local_fixture capture, preserves
existing live history and atomically compare-and-swaps the new live database.
If it reports uncertainty after replace, inspect live SHA first; do not blindly
retry. A repeated call after verified publication returns already_published.

Before switching readers, validate the actual original query unit/sandbox and
save its current link, unit/drop-ins and original evidence hash. This candidate
expects the existing ConditionPathExists and InaccessiblePaths to remain valid.
If actual units differ, stop and review a revised drop-in. Stop **query only**
while changing its executable path to prevent mixed lazy imports. The new
release path in the drop-in is absolute; leave the old `current` symlink alone.

```sh
systemctl stop website-analytics-query.service
install -d -o root -g root -m 0755 /etc/systemd/system/website-analytics-query.service.d
install -o root -g root -m 0644 /approved-staging/20-scoped-evidence.conf /etc/systemd/system/website-analytics-query.service.d/20-scoped-evidence.conf
systemctl daemon-reload
systemctl start website-analytics-query.service
```

Verify analyticsquery cannot read source ADC/env, writes to evidence are denied,
only127.0.0.1:8765 is listening, arbitrary path/SQL/command/fetch operations are
rejected, and two independent clients + query restart return identical pinned
versions. `case_observations` must separate latest provisional from strict
complete; partial/unknown evidence cannot be silently presented as complete.
Confirm original query versions/hash, old sync configuration, ADC metadata and
old timers active/enabled/schedule are unchanged. New services remain manual.

## Rollback and remaining gates

Before B, old query and sync were never changed. Stop only the new source unit
if necessary and retain private receipts/candidate for inspection. After B:
stop query, restore the recorded previous `20-scoped-evidence.conf` (or remove
only this newly created drop-in if absent before), daemon-reload and start
query with its original code/evidence. Do not alter old `current` or delete the
new live database/history to roll back a reader. Confirm original query
response/hash and source timer/config baseline. A partial source capture is not
permission to overwrite published evidence, change credentials or retry a denied
request.

Not yet validated here: Linux native wheel execution/systemd sandbox, real
source+proxy/SDK compatibility under the lock, actual installed history seed,
read-access enforcement, live publication/switch and cross-machine client
access. Existing Google authorization may independently need renewal; any new
grant/save stays outside this approval. Strict provider finality can remain
unprovable; GA4 lag is an estimate, GSC metadata is exact-scope only, and inquiry
DATE boundary is unknown. No causal attribution is implied by continuous edits.

Recommended next action: approve **A only**, using the two explicitly chosen
completed weeks, original URLs and aggregate-only output. Review its receipt
before authorizing B. No daily schedule or colleague access is requested yet.
