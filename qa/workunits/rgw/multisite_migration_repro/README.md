# Two-cluster RGW migration reproducer

**Disposable development clusters only.** `--reset` replaces vstart data on both
nodes. vstart can also stop other Ceph processes owned by the invoking user.
Never point this harness at production.

## Environment

- Zone1: local `/workspace/croit_repo/ceph/build`, `http://172.31.139.21:8000`.
- Zone2: `root@172.31.139.22`, container `ceph-dev`, same build directory,
  `http://172.31.139.22:8000` (host networking).
- Passwordless SSH, customer binaries built from each checkout's current Git
  `HEAD`, Python 3/venv. Both checkouts must descend from the original base
  `10cef5efe60f078b70208ec17f03305a8dcc4100`.
- Each node starts `MON=1 OSD=1 MGR=1 RGW=1 ../src/vstart.sh -n --without-dashboard`.
  MDS/FS are disabled. Both zones are writable; dynamic resharding and automatic
  lifecycle threads are disabled to isolate these cases.

## Reuse after a committed build

`Lab.versions()` accepts the original base and committed descendants. On each
node, both `ceph` and `radosgw` must report exactly the Git `HEAD` of the source
checkout at that node's configured build directory parent. All four binaries
must report the same commit. Each checkout must also pass Git's ancestry check
against the full original base above; a failed ancestry lookup rejects reuse.

`Lab.validate()` still requires the complete version records to match the saved
private configuration. After rebuilding both nodes from the new delivery
`HEAD`, the lab owner must explicitly refresh **only** the private configuration's
`versions` entry. Before that refresh, verify on both nodes that the live and
configured FSIDs match the recorded owned cluster FSIDs, and that the committed
realm/period, zonegroup, zone identities, membership, masters, and endpoints
match the saved topology. Confirm the ordinary S3 user and credentials and the
recorded daemon PID/build/entity identities, then verify that the rebuilt
binaries match both checkout HEADs and satisfy the common-commit/base-ancestry
guards. Preserve every other saved field and the private file permissions.

The harness does not automatically refresh saved versions. After the explicit
version-only refresh, run the exercise against the existing owned clusters;
its full validation still checks saved versions, FSIDs, topology, user, and PIDs.

## One command: fresh clusters, multisite, and all cases

Run from the **local build directory**:

```bash
python3 ../qa/workunits/rgw/multisite_migration_repro/lab.py setup --reset --run --objects 3 --timeout 90
```

Setup creates the realm/zonegroup, system credentials, an ordinary S3 user,
commits the shared period, restarts both RGWs, and checks topology, FSIDs,
versions, metadata synchronization, and endpoint connectivity in both directions.
Boto3 is installed into `build/.venv-rgw-repro`, not system Python.

To keep the running clusters and repeat only the exercise:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --objects 3 --timeout 90
```

Each run uses new `rgwlab-...` buckets. Unpurged fixtures and leftover heads are
retained. **No direct RADOS deletion is performed.** The bucket purge affects
only the OLD test bucket; copy/rename cases retain their replacement for inspection.

## Cases

### Empty a suspended bucket without copy or rename

Run only this focused scenario against the existing lab:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --no-copy-rename --objects 3 --timeout 180
```

This is the existing `migration-synced-empty` test with copy/rename omitted, not
a separately implemented deletion test. It reuses `single_deletes()` and
`empty_versions()` and their reports, with the same numbered/null-data and marker
cohorts. It records caught-up/listing checkpoints for the enabled bucket,
numbered writes, suspension, null writes, and each DELETE. Replacement-bucket
checks are omitted because no NEW bucket exists. There is no manual full sync,
purge, Zone2 cleanup, or raw deletion. It stops at the first mismatch and retains
the fixture and report. Exit 0 requires logical and physical emptiness in both
zones; exit 1 is a measured failure, 2 incomplete. The earlier standalone
`suspended_empty.py` attempt has been superseded by this option.
The completed reused-code run passed the setup, ordinary DELETEs, and all
numbered-version deletions, then failed at the first NULL-marker deletion despite
caught-up status. See [NO-COPY-RENAME-results.md](NO-COPY-RENAME-results.md) for
the exact per-stage report and retained 13/14-marker result.
After the focused receiver patch was applied and built on both nodes, the
original failure passed, but a separate NULL-marker cohort failed on the existing
OLH-head timestamp guard, leaving 3/4 markers. See
[NULL-MARKER-RECEIVER-FIX-results.md](NULL-MARKER-RECEIVER-FIX-results.md).
The subsequent marker-metadata timestamp refinement plus atomic stale-NULL
unlink protection passes the complete requested workflow: all data and all 14
markers are removed on both zones, with zero raw objects. See
[NULL-MARKER-TIMESTAMP-FIX-results.md](NULL-MARKER-TIMESTAMP-FIX-results.md) for
the patch scope, three focused safety tests, and final two-zone evidence.
The first attempt on October 9, 2026 was incomplete: Zone2 SSH timed out during
lab validation, before fixture creation or emptying. No sync result was measured.

If fresh SSH connections intermittently fail, establish an OpenSSH control
connection separately and set `RGW_REPRO_SSH_CONTROL_PATH` to its absolute socket
path when invoking the harness. This reuses that connection; it does not retry
or replay commands, reset the lab, or fix underlying S3/replication packet loss.
The follow-up packet-level investigation found flow-selective loss affecting
both SSH and RGW. With SSH reuse, the next attempt reached fixture preparation
but stopped because Zone2 did not receive versioning enablement. No emptying
DELETEs ran. See [CONNECTIVITY-results.md](CONNECTIVITY-results.md).

| Case | Operation |
|---|---|
| `control` | Never-versioned bucket, ordinary DELETE. |
| `plain-delete` | Enable/suspend versioning, write new null objects, ordinary DELETE. |
| `null-single` | Explicit `versionId=null` DELETE. |
| `null-bulk` | DeleteObjects with explicit null version IDs. |
| `null-numbered` | Delete an old null version while preserving a numbered current version, then delete the numbered version. |
| `pending-head` | Delete a non-current marker, then the remaining versions. |
| `migration-keep` | Numbered history plus null overwrites; copy, final pass, checkpoint, rename, manual sync, version/marker cleanup, purge. |
| `migration-reap` | Same, but remove confirmed marker-only entries before rename. |
| `migration-direct-purge` | Copy and rename a populated suspended OLD bucket, then purge only on Zone1; no per-zone version cleanup or manual sync. |
| `migration-synced-purge` | By default, synchronize both renamed buckets in both directions before purging populated OLD on Zone1 only. With `--no-copy-rename`, checkpoint only the original natural OLD streams, then apply the same Zone1 purge. |
| `migration-synced-enumerate` | Same post-rename gate, enumerate/delete every OLD version and marker on Zone1, wait for both zones to be empty/caught up, then purge. |
| `migration-synced-boto-delete` | Same post-rename gate, call boto3 `delete_bucket` on populated OLD and verify expected `BucketNotEmpty` rejection and preservation. |
| `migration-synced-single-delete` | Same post-rename gate, issue one plain S3 DeleteObject on Zone1 at a time; check both zones after every key without bulk deletion or bucket purge. |
| `migration-synced-empty` | Complete the successful single-delete phase, then delete every remaining data version and marker by explicit VersionId on Zone1; require empty synchronized OLD on both zones, without purging it. |

`CopyObject` models **rclone's server-side copying**, not the rclone executable.
The normal rename cases checkpoint replication before switching names.

Select cases by repeating `--case`. A separate, synthetic timing experiment can
pause Zone2 RGW across final writes and rename:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-keep --objects 3 --timeout 180 --rename-with-backlog
```

This pause is not attributed to the customer. Restart lease expiry can delay
metadata convergence; use the longer timeout. The first 60-second backlog run
timed out waiting for renamed identities, which later converged. It did not
establish the customer's exact persistent post-rename failure.

### Direct multisite purge after rename

This dedicated regression leaves numbered versions, null data versions, numbered
delete markers, and null delete markers in OLD on both zones. After checkpointed
copy and rename, it runs only `radosgw-admin bucket rm --purge-objects` on Zone1.
It does **not** manually delete OLD versions, purge Zone2, or run manual bucket
sync. Read-only inventories use the saved OLD IDs/markers even after metadata
disappears; NEW's identity, listing, and every expected payload are also checked.
The null-marker cohort has no numbered history beneath it, isolating purge from
a separately observed pre-purge replication mismatch in that combination.

Run against the existing owned lab without resetting either cluster:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-direct-purge --objects 3 --timeout 180
```

The test uses `--timeout` as its polling budget and requires two successful
observations. Each observation has bounded command/SDK timeouts, so wall-clock
duration can exceed that budget. Exit **1** means this regression failed; exit
**2** means the test was incomplete. Existing observation-only cases retain their
prior semantics. This case must not be combined with `--rename-with-backlog`;
default backlog runs select only the original eight cases.

The completed direct-purge run **failed**: Zone1 had 0 OLD raw objects, Zone2
retained 24 (17 payload objects and 7 empty heads), despite OLD being absent from
both namespaces. See [DIRECT-PURGE-results.md](DIRECT-PURGE-results.md) for the
report, follow-up payload verification, and separate null-marker/history fixture
failure. No backend fix was added for this result.

### Purge a populated suspended bucket without copy or rename

Run exactly one case from the **local build directory**, against the existing
owned lab:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-purge --no-copy-rename --objects 3 --timeout 180
```

This reuses `migrate()`'s populated suspended fixture,
`synchronized_delete(..., replacement=None)`, and `direct_purge()`. The fixture
retains numbered history, null data, numbered markers, and null markers. With
three objects per cohort, OLD has 10 numbered data versions, 7 null data versions,
3 numbered markers, 3 null markers, and 8 readable current keys. No NEW bucket
is created. The enabled/seed, numbered-write, suspension, and final NULL-write
checkpoints record the same stages as the no-copy emptying case. Before purge,
both original natural OLD data-sync streams must be caught up twice, the exact
complete version lists must match the fixture on both zones, OLD's saved IDs and
markers and suspended state must agree, and every expected current and numbered
payload must read correctly on both zones.

The purge command is `radosgw-admin bucket rm --bucket OLD --purge-objects`,
issued **only on Zone1**. Marker creation is part of fixture population; there
is no individual VersionId cleanup before purge, Zone2 cleanup, copy/rename,
manual bucket/full-sync restart, forced reset, or raw deletion. The CLI accepts
`--no-copy-rename` only with exactly one `migration-synced-purge` or
`migration-synced-empty` case, without backlog, retained-run continuation, or
self-test options. This focused run ends after its one case and retains the
fixture and reports on failure.

After purge, each observation explicitly collects full redacted
`radosgw-admin sync status` stdout, stderr, and command/transport error evidence
from both nodes. It validates the realm and zonegroup IDs/names, local zone
IDs/names, and the expected peer source ID/name against the saved topology.
Only that peer's data-status block can satisfy `data is caught up with source`;
Zone2 additionally requires `metadata is caught up with master`. Zone1 must
identify itself as metadata master; it needs no remote metadata stream. Once
S3 reports `NoSuchBucket`, per-bucket sync status is recorded as **NA**, with
`caught_up: null`, and supplies no caught-up evidence.

The strict gate requires two consecutive complete post-purge observations with
both global peer streams and Zone2 metadata caught up. Logical OLD absence and
raw-object counts on **both zones** are then compared using the saved OLD
markers, even after bucket metadata is gone. Reports preserve every observation's
raw object names, sizes, zero-size counts, and sampled OLH/manifest xattrs in
`*-purge-observations.json` and `report.json`, plus the before/after inventories.
Exit **0** requires a successful purge and absence/zero raw objects twice;
**1** is a measured failure after the caught-up gate; **2** means incomplete
observations or a gate that never became caught up, including missing status,
unexpected identities, unavailable inventories, or transport failures.

Limits: this parses the native text status format, requiring a syncing peer with
all nonzero-count shards incremental as well as the explicit caught-up summary.
Bucket-stats exit 0 or ENOENT (2) is required for a complete metadata observation.
Global status is peer-wide; saved-marker inventories independently measure OLD's
physical cleanup.
`--timeout` is a polling budget; bounded SDK/admin/inventory commands can extend
wall-clock time beyond it. A possible failure shape is absent metadata on both
zones with 0 OLD raw objects on Zone1 and 24 on Zone2 (17 nonempty bodies and 7
zero-size heads). Sizes distinguish retained bodies from empty-head candidates;
zero-size or OLH-pending attributes alone do not prove orphan status. Payload
bytes are checked before purge. The existing read-only RADOS collector lists
objects, records sizes, and samples xattrs; it does not GET raw payload bytes after
metadata removal.
The default copy/rename purge still protects NEW's identity, listing, and payloads;
the post-purge global gate applies only to this no-copy purge variant.
The run before the producer-side NULL-intent fix failed physical cleanup:
metadata was absent on both zones and global synchronization was caught up,
but OLD had 0/10 raw objects. All seven retained Zone2 payload bodies were
verified by read-only raw GETs. After the one-line fix in
`RadosObject::delete_object()`, fresh run
`rgwlab-20261010-223505-93c2` passed the same case with absent metadata and zero
OLD raw objects on both zones in two consecutive caught-up observations.
See [NO-COPY-ADMIN-PURGE-results.md](NO-COPY-ADMIN-PURGE-results.md) for both results.

### Synchronized deletion comparisons

With copy/rename (the default), `migration-synced-*` cases run `bucket sync run` for OLD
and NEW in both directions after rename, waiting for each direction before
starting its reverse. Before any deletion they require two
consecutive observations of **all four renamed data-sync streams caught up**,
correct bucket IDs, OLD's original version listing, and NEW's complete listing
and payloads. Failed checkpoints stop the case **without deleting the bucket**.

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-purge --objects 3 --timeout 180
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-enumerate --objects 3 --timeout 180
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-boto-delete --objects 3 --timeout 180
```

The enumeration case uses single explicit-version DELETEs, not bulk null deletes,
and does not manually delete on Zone2. It waits for empty lists and checkpoints
on both zones before purge. The boto3 case tests the actual S3 DeleteBucket API,
which has no purge option: a passing rejection test does **not** mean OLD was
deleted. Existing observation-only cases and default backlog selections remain
unchanged.
See [SYNCED-DELETE-results.md](SYNCED-DELETE-results.md) for completed/blocked
outcomes: synchronized admin purge left 0/10 OLD raw objects; enumeration was
blocked by post-sync version-list divergence before any deletion; direct boto3
DeleteBucket correctly rejected populated OLD with `BucketNotEmpty`.

### Single-object deletion after synchronized rename

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-single-delete --objects 3 --timeout 180
```

The case uses the same copy/rename fixture and strict synchronization gate. It
then deletes each copied current OLD key with one `delete_object` request on
Zone1 **without VersionId**. After each request it waits for the existing streams
without restarting full sync, checks the complete expected version lists and
current reads on both zones, checks null payload removal by read-only physical
inspection, and verifies all numbered OLD payloads and NEW's contents remain
correct. It stops at the first inconsistency and retains the fixture.

Plain DELETE in a suspended bucket creates a null delete marker and preserves
numbered history. A passing test does not mean OLD is empty or its bucket was
purged. Explicit `versionId=null`, bulk DeleteObjects, and bucket purge are
different operations, not performed by this case.
The completed run passed all eight requests from Zone1 to Zone2 after the full
post-rename gate. See [SINGLE-DELETE-results.md](SINGLE-DELETE-results.md). Final
OLD state is 0 null data, 10 numbered versions and 14 markers per zone—not an
empty or purged bucket.

### Empty the retained historical bucket

Fresh, complete copy/rename/synchronize/plain-delete/explicit-version-emptying
workflow:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --objects 3 --timeout 180
```

Continue the actual successful single-delete fixture rather than creating new
buckets or replaying full synchronization:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --empty-after-run rgwlab-20261009-132521-57c7 --objects 3 --timeout 180
```

Resume accepts only a completed, passing generated single-delete run inside the
owned lab's artifact directory. It checks both zones' live OLD/NEW names, IDs,
saved OLD marker, complete histories, and suspended state before authorizing
those exact bucket names. Original reports are not overwritten. Changed or
partially emptied fixtures are rejected; other case/backlog combinations are
not permitted with `--empty-after-run`.

Emptying issues individual Zone1 DELETEs **with VersionId**, including literal
`null` for null markers. Numbered data is removed before the latest markers, so
no old payload is exposed by marker removal. Replication and complete version
lists are checked after each request. No bulk deletion, Zone2 manual deletion,
bucket purge, or raw RADOS deletion is used. Success additionally requires empty
logical lists and zero raw OLD objects on both zones, caught-up streams twice,
the same suspended OLD bucket still present, and all NEW data intact.
The retained-fixture attempt did not reach full emptiness: all 10 numbered data
versions were removed on both zones, then the first null-marker deletion left
13 markers on Zone1 and 14 on Zone2 despite caught-up status. See
[EMPTY-OLD-results.md](EMPTY-OLD-results.md). It stopped without peer cleanup or
purge. That partially consumed fixture cannot be resumed using its original
single-delete report again; use a fresh test after addressing the blocker.
The fresh two-node retest with the full PR #64272 backport reproduced the same
null-marker failure after all eight plain DELETEs and ten numbered-version
DELETEs passed. See [PR-64272-results.md](PR-64272-results.md) for the builds,
GET/HEAD marker-header checks, and retained 13/14-marker fixture.

## Results observed on October 8, 2026

The table below is the **unpatched baseline**. For the subsequent two-node build
and retest with PRs #65158 and #65948, see
[PR-65158-65948-results.md](PR-65158-65948-results.md).
For the later #58133 backport and eight-case rerun, see
[PR-58133-results.md](PR-58133-results.md).
For #67661 plus its approved timestamp-epoch prerequisite #62469, see
[PR-67661-results.md](PR-67661-results.md). That rerun removed the deleted-key
OLH heads and left zero OLD-bucket raw objects in both zones; the deliberately
deferred bulk-null case still fails. The report also records compatibility and
rollout limits.

Complete seven-case run: `build/rgw-repro/runs/rgwlab-20261008-220714-d7b9/report.json`.
Three test keys per cohort; this is not a production-scale benchmark.

| Observation | Result |
|---|---|
| Never-versioned control | Removed in both zones; version lists match. |
| Plain, single-version, and bulk null deletion | Zone1 retains 0 null data versions; Zone2 retains 3 in each case. All 3 remain readable with the original payload despite “caught up” status. |
| Non-current marker cleanup | No listed data keys, but 3 deleted-key zero-size heads remain in each zone with OLH pending attributes. The additional live seed head is not classified as an orphan. |
| Copy plus rename | Three history-to-null copies carry OLH attributes in the never-versioned Zone1 replacement and return `NoSuchKey` after the name switch; Zone2's copies are readable. A follow-up one-key run verified successful reads before rename and `NoSuchKey` afterward. |
| OLD purge, markers kept | Commands succeed; remaining raw objects: Zone1 0, Zone2 6. |
| OLD purge, markers reaped first | Commands succeed; remaining raw objects: Zone1 1, Zone2 3. |

Counts can change with timing. Inspect version IDs, payload checks, and xattrs;
zero object-body size alone is not proof of an orphan. An exit code of **0 means
the exercise completed**, not that the RGW build is correct. Exit **1** means
a checked deletion case failed; inspect `regression_failures` and its checks.
Exit **2** means an
incomplete exercise**; read its error and logs instead of treating missing
observations as empty buckets.

Follow-up report with before/after-rename reads and manual-sync checkpoints:
`build/rgw-repro/runs/rgwlab-20261008-223912-d9fd/report.json`.
The checkpointed OLD bucket matched before and after manual sync; that run did
not reproduce the customer's persistent rename lag.

## Inspect and collect

```bash
python3 ../qa/workunits/rgw/multisite_migration_repro/lab.py admin zone2 sync status
python3 ../qa/workunits/rgw/multisite_migration_repro/lab.py admin zone2 bucket sync status --bucket=BUCKET_FROM_REPORT --source-zone=zone1
python3 ../qa/workunits/rgw/multisite_migration_repro/lab.py logscollect
```

- Credentials/owned FSIDs: `build/rgw-repro/state.json` (mode 0600; do not share).
- Setup diagnostics: `build/rgw-repro/bootstrap.log`.
- Reports, snapshots, raw-object inventories/xattrs, and redacted log extracts:
  `build/rgw-repro/runs/<run>/`.
- Live logs: `build/out/radosgw.8000.log` on each node; also MON/OSD/MGR logs in
  `build/out/`. Collected RGW extracts keep the last 8 MiB, not the entire log.

Helper validation: `exercise.py --self-test` using the virtualenv Python.
All Python files passed `py_compile`; `ninja -j10` completed from local `build/`.

## Committed delivery history

See [HISTORY.md](HISTORY.md) for the 14 upstream-to-local commit mappings,
separate NULL-marker fix, customer QA delivery, and preserved safety-snapshot
source equality. The result reports and build/validation statements above are
historical, including their descriptions of then-uncommitted patches. The
customer rerun after the separate QA commit and rebuild is **PENDING**.
