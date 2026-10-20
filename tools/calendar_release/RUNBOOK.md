# 1.5.44 fixed deployment candidate — not executed or approved for production

Target: existing ubuntu@119.28.17.175; DB `/var/lib/cityu-mail-pilot/pilot.sqlite3`.
Only this local candidate reserves 1.5.44. Check current production and sole writer
again immediately before a separately approved maintenance window. Base code must
match BASELINE-SHA256SUMS; drift stops before maintenance. No dirty-tree upload.

The orchestrator is deliberately for this first deployment, not a general installer.
It reuses the existing project's offline backup-before-replace and systemd service
identity design (`pilot_app/deploy_pilot.sh`, AGPL-3.0), P4 SDK hash lock and existing
SQLite backup API. No new library or copied external project code. Source is frozen
from previously reviewed P4 release reconstruction, not the original dirty checkout.
The old SOURCE-STAMP/PUBLISH metadata was stale and removed; RELEASE-SHA256SUMS
and SOURCE-PROVENANCE replace it for this unpublished local candidate.

## Operator preconditions before executing anything

This runbook requires a later explicit user production approval, reviewed manifest
and root-owned immutable staging directory at `/opt/cityu-calendar-release-1.5.44`.
Verify the whole manifest against the externally supplied manifest SHA/commit; never
sudo code from a directory writable by another user. Staging/upload/root execution,
production runtime installation, backup and downtime are not authorized this round.
Freeze any other deployer, maintenance/cron job and manual DB writer for the whole
window. The script serializes only its own commands with a flock; it cannot lock out
other unrelated administration. Confirm disk has space for app/tools/env + full DB
snapshot and new sender venv. Confirm current web/worker are active and the known
backup timer/path units exist. No unknown sender unit or key/config may pre-exist;
resolve drift before running. Env DB must exactly equal the verified fixed path.

Keep nginx/certbot unchanged; web downtime occurs during the offline backup/migration
window and can return temporary 502. Set a short agreed window with operator present.
Existing web `.venv` is never replaced; new sender runs its own Python 3.14 venv at
`/opt/cityu-mail-pilot-sender/1.5.44/venv`. Dependency installation must finish before
stopping services. Application dependencies are hash locked; http_ece build-tool
bootstrap is not a fully locked build toolchain, as disclosed in the Linux evidence.
A completed runtime preparation is checked and reused without reinstalling.
A failed partial runtime install must be inspected and removed only with explicit
operator approval before retry; it is not silently reused or overwritten.

## Fixed command sequence after production approval (not executed now)

Run each separately, under the operator's existing approved privilege mechanism:

```sh
/usr/bin/python3.14 -I /opt/cityu-calendar-release-1.5.44/tools/calendar_release/deploy_disabled.py prepare-runtime
/usr/bin/python3.14 -I /opt/cityu-calendar-release-1.5.44/tools/calendar_release/deploy_disabled.py apply
```

The script validates source manifest, existing base source, fixed DB env and runtime
lock before downtime. `apply` stops web/worker and backup triggers/service, verifies
inactive, saves old app/tools and protected env, makes a consistent SQLite backup
using backup API, integrity-checks and hashes it, and queries schema/paused/recovery
ONLY inside this actual backup window. Backup directory is fixed, root-only 0700
`/var/backups/cityu-calendar-releases/1.5.44`, files private. No secret values print.
The existing application runtime and existing web/worker unit files remain in place;
only new disabled drop-ins are added, and these are removed on offline rollback.

Only after a complete backup does it replace app source, install the new helper and
disabled sender unit/config, then migrate using existing `Database.initialize()` as
cityumail via fixed helper, not web or full worker startup. Migration code executes
existing initialization/data fixups (not only DDL), covered by the P4 source review.
No SecretBox/provider/mailbox access occurs in this helper; no external calls.
Schema must be 3 and notification_channel_paused must be 1 before reopening.
Normal worker later completes recovery with existing mechanisms; paused stays 1.

Sender unit is disabled, never started, has a missing approval-file condition AND
hard Environment=INFE_PILOT_PUSH_ENABLED=0. Core web/worker receive a disabled drop-in.
Template webpush.env is 0640 root:cityumail under root:cityumail 0750 webpush directory.
No production key is generated. Subject=https://mycampusmail.com, future key path
`/etc/cityu-mail-pilot/webpush/vapid-private.pem`, key-id=1; public key stays empty.
Because the existing `/etc/cityu-mail-pilot` parent may be root-only, any future key
ceremony must explicitly review minimal traversal permission/ACL for cityumail;
this script does not relax the parent's permissions or claim the key is readable.

The persisted `exposed` phase is written BEFORE starting web/worker. `opened` is
written only after local /health confirms 1.5.44 and disabled gates are checked.
The local read-only readiness probe retries transient startup failures and wrong
versions at 250ms intervals within a 20s deadline (each request timeout at most 2s).
Only status=ok plus the exact expected version succeeds; deadline failure preserves
exposed and never replays deployment or migration.
A crash between phases is conservatively treated as potentially user-writable.
Repeated apply only returns success after rechecking health and gates for `opened`;
any incomplete phase refuses blind replay. It never replays a one-shot backup service.

After apply, operator checks public HTTPS health/version, expected existing units,
static hashes, anonymous boundaries, authenticated manual calendar CRUD/completion
and original-mail navigation. Inspect recovery until complete. Check sender remains
inactive/disabled and no outgoing notification attempts. A local health check alone
is not user acceptance or real device proof. Save only sanitized facts.

## Executable failure recovery

Before `exposed` (stopping/backing_up/backed_up):

```sh
/usr/bin/python3.14 -I /opt/cityu-calendar-release-1.5.44/tools/calendar_release/deploy_disabled.py rollback-offline
```

Before complete backup, original code and DB are untouched; this resumes prior active
writers. After backed_up, it validates saved snapshot hash, restores old app/tools/DB,
removes only newly introduced config/unit/drop-ins, reloads systemd and resumes prior
active writers. It does not restore env: apply never changes pilot.env. Snapshot DB
restore is legal here only because the reviewed window has no writers or real sender.
The persisted restored phase means old data/source have been restored, but services
are not yet confirmed. A failed first or partial start, or failed old-version health,
leaves restored. Repeating rollback-offline then only resumes services and checks
1.5.43 health; it does not stop writers, copy the DB or overwrite new user writes.
Only after prior active units and exact old health are confirmed is rolled_back
saved. Repeating a completed rollback also rechecks services/health. Source-version
drift refuses automatic recovery. All backup material remains for audit.

At/after `exposed`, including a failed health/start or any later real send:

```sh
/usr/bin/python3.14 -I /opt/cityu-calendar-release-1.5.44/tools/calendar_release/deploy_disabled.py contain
```

This stops sender/web/worker/triggers and preserves DB/environment and accepted/unknown
facts. Offline restore explicitly refuses after exposure. No blind old-DB restore,
unknown retry, old 1.5.43 worker or unreviewed downgrade against schema3 is permitted.
Prepare a concrete forward recovery/compatible disabled version after inspecting the
actual failure and user writes; that unknown recovery cannot safely be pre-scripted
as a data-losing rollback. Original backups and live DB are preserved. Runtime stage
is retained; neither rollback nor containment deletes potentially useful evidence.

## Later notification enabling — separate approval, not an action in this script

After candidate review, actual deployment/backup/migration/UX acceptance and recovery
completion, prepare one explicit key/config enable transaction: protected P-256 key,
internal public/private pair match, approved subject, parent traversal permissions,
public key/key-id config for web and sender; source/SDK/runtime and guard readback;
then gated removal of disabled overrides/approval sentinel/paused flag and deliberate
sender enable. This candidate contains no enable command. Do not create the sentinel
or unpause in isolation. The web must load the approved public config too, not just
the sender. Real test sends and browser/iPhone Home Screen/TWA background/lock-screen,
click ownership, logout/account switch, edits/cancellation and offline recovery need
explicit authorization and devices. provider_accepted alone proves none of these.

Android WebView still has no provider; Firebase remains unconfirmed and UnifiedPush
undecided. P5A/P5B candidates are not included or signed here. All RAG HOLD/Q1 BLOCKER/
NO-GO1921HOLD/artificial source-semantic/A2 and production gates remain effective.
