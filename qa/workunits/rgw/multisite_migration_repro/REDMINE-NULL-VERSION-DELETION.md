# Redmine issue draft

## Subject

RGW multisite: NULL-version deletion and admin purge can leave markers or data
on a replica even when synchronization reports caught up

## Issue details

| Field | Value |
|---|---|
| Tracker | Bug |
| Component | RGW multisite, RADOS backend, bucket index / OLH |
| Suggested priority | High: replica deletion inconsistency and retained storage |
| Fix status | Implemented in the customer branch; verified in a two-zone lab |
| Observed environment | Reef customer base `croit_18.2.8_core1`, with PRs #65158, #65948, #58133, #62469, #67661, and #64272 backported |
| Marker-replication fix (original commit) | `ef4457cfb0386f826d4d3bce381fa4634ea3aaed` — `rgw/multisite: safely replicate explicit NULL marker removals` |
| Admin-purge fix (original commit) | `06fcf07ab4c8b217e9ea2a0e447504706485cf38` — `rgw/multisite: preserve explicit NULL intent during admin purge` |

This is an issue draft, not a claim that a Redmine issue has already been filed.
It describes the two code fixes separately because they address different points
in the same deletion flow.

The IDs above identify the fixes before this document was folded into the earlier
QA/docs commit. Replaying the code commits after that documentation-only
amendment changes their Git hashes, but preserves their subjects and patches.

## Summary and impact

A versioning-suspended bucket can contain data versions and delete markers with
the S3 version ID `null`. Removing one of these instances on Zone1 can succeed
locally but fail to remove the corresponding instance on Zone2.

Two failures were reproduced:

1. **Explicit marker deletion:** S3 `DeleteObject` with `VersionId="null"`
   removed a marker on Zone1, but Zone2 kept it. Both bucket-sync streams reported
   caught up.
2. **Administrative bucket purge:** Zone1's
   `radosgw-admin bucket rm --purge-objects` returned success. The bucket namespace
   disappeared on both zones, but Zone2 retained seven real NULL-version payload
   bodies and three zero-size marker heads. Global synchronization was caught up.

The retained bodies were verified against their original payloads by read-only
RADOS GETs. This was not just a stale counter or listing. A zero-size head alone
was not treated as proof of an orphan.

The practical impact is inconsistent replica state and storage that remains
after a successful purge. **A successful DELETE, an empty current-object list,
or a caught-up sync cursor does not by itself prove complete deletion.**

## Terms used in this issue

| Term | Meaning here |
|---|---|
| NULL version | The literal S3 version ID `null`. It is not the same as omitting `VersionId` from a DELETE request. |
| Delete marker | A version record that makes the current object appear deleted. It is metadata, not an object payload. |
| OLH — Object Logical Head | RGW metadata that tracks the current version and pending changes. A physical OLH head can exist without readable object data. |
| Bucket index | The records RGW uses to list versions and find data or marker instances. |
| Bilog | The bucket index log consumed by multisite replication. Its NULL-version flag preserves the meaning of an explicit NULL deletion. |
| Epoch | A version/change ordering value. The patched branch uses timestamp-based epochs. |
| CLS / STALE acknowledgment | CLS is the OSD-side object-class code that updates the index atomically. A STALE acknowledgment records that an outdated operation was handled without deleting a newer target. |

In a suspended bucket, an ordinary DELETE without `VersionId` can create or
replace a NULL delete marker. An explicit `VersionId="null"` DELETE removes that
particular NULL instance. Replication must not confuse these operations.

## Expected and actual behavior

| Operation | Expected after replication | Observed before the relevant fix |
|---|---|---|
| Explicit NULL-marker DELETE on Zone1 | The same marker is removed on Zone2; unrelated versions remain intact. | Zone1 removed it, but Zone2 kept it despite caught-up status. |
| Purge of the populated test bucket on Zone1 | The bucket disappears on both zones and the fixture's backing raw objects are cleaned up. | Both namespaces disappeared, but raw-object counts were 0 on Zone1 and 10 on Zone2. |
| Delayed unlink for an older NULL instance | A newer replacement in the same NULL slot is preserved. | The fix needs an atomic safeguard; a metadata read before unlink is not enough to protect a concurrent replacement. |

The last row is a safety requirement covered by focused tests. It is not a claim
that loss of a newer replacement was observed in the end-to-end reproducer.

## Short reproducer: human steps

Use a disposable test bucket in a two-zone lab, not a production bucket. These
steps summarize the verified fixture described in the test section below.
To observe the first failure, use the six-backport baseline without the marker
receiver fix. To isolate the second failure, keep the marker receiver fix but
omit the admin-purge NULL-intent fix. On the fully fixed build, both cases should
complete cleanup on both zones.

### Case 1: explicit NULL-marker deletion

1. Create a bucket on Zone1 with versioning Enabled. Upload objects to create
   numbered history and delete markers, then suspend versioning and add NULL
   data versions and NULL markers. Wait until both zones have matching versions
   and synchronization is caught up.
2. On Zone1, delete the live objects without specifying a version ID. Then remove
   the remaining numbered data versions individually. Wait for replication after
   each deletion; the bucket now contains markers but no data versions.
3. Remove one NULL delete marker on Zone1 using the client's **delete specific
   version** action with version ID `null`, not another ordinary object DELETE.
4. After synchronization catches up, compare the complete version/marker lists.
   Before the fix, Zone1 has removed the marker but Zone2 still lists it. After
   the fix, it is absent on both zones. Do not manually delete it on Zone2.

### Case 2: administrative purge

1. Prepare a **fresh populated bucket** with the same numbered history, NULL
   data, and marker mix. Do not first perform Case 1's deletion steps. Wait for
   matching versions, readable payloads, and caught-up replication on both zones.
2. Record the original bucket ID/marker on each zone so its raw objects can still
   be identified after the bucket namespace disappears.
3. On Zone1 only, request administrative bucket removal with the purge-objects
   option. Do not empty it through individual version deletes or purge Zone2.
4. Wait for global peer data synchronization and Zone2 metadata synchronization
   to catch up. Confirm that both namespaces report the bucket missing, then
   inspect raw storage read-only using the saved markers. Before the fix, the
   recorded fixture left 0 objects on Zone1 and 10 on Zone2. After the fix, both
   zones have 0 objects. Namespace absence alone is not the cleanup check.

## Root cause

### 1. The receiver treated missing data as a missing marker

`RGWAsyncRemoveObj::_send_request()` checked object state before deleting the
replicated instance. An explicit NULL lookup of an OLH-only head can return
`-ENOENT` because there is no readable payload. However, the delete marker can
still exist in the bucket index.

The receiver returned before reaching index-backed unlinking. Incremental sync
tolerates ENOENT and can advance its cursor, so the event was consumed while the
marker remained.

Allowing this specific state exposed a second problem: the timestamp preflight
compared the replica's physical OLH-head `mtime` with the source marker's logical
timestamp. A replica can create its local OLH head after the logical marker was
created. Treating that later metadata-creation time as a newer user change can
incorrectly skip the deletion.

The correct comparison for this marker-only case is the marker's bucket-index
`meta.mtime`, not the physical OLH-head creation time. Readable data still needs
the existing timestamp protection.

### 2. Admin purge lost explicit NULL identity when writing the bilog

Admin purge enumerates object versions. `rgw_remove_object()` converts a listed
empty instance to literal `"null"` and calls `RadosObject::delete_object()` to
remove that specific instance.

The convenience adapter did not preserve this identity in
`del_op.params.null_verid`, which defaults to false. The backend selected the
correct instance locally, but normalized its stored key to an empty instance.
Because `null_verid` was false, the unlink bilog record omitted
`RGW_BILOG_NULL_VERSION`, the flag that distinguishes an explicit NULL deletion
from an unqualified DELETE.

The replica restores literal `"null"` only when that flag is present. Without it,
the receiver can select delete-marker creation semantics instead of the
requested instance unlink, or return ENOENT before unlinking a marker-only
instance. The explicit-NULL receiver handling cannot apply when the request has
lost its NULL identity.

Replication can advance after a successful no-op or tolerated ENOENT, while
bucket metadata removal propagates separately. Consequently, a successful
purge and caught-up synchronization do not guarantee complete replica cleanup.

## Proposed fix

### Commit `ef4457cfb03`: safe NULL-marker replication

- Capture literal NULL identity before loading state, and pass it to the delete
  operation.
- Allow only the expected **versioned, explicit-NULL, loaded OLH/no-data state**
  to proceed despite ENOENT. Other storage errors still fail normally.
- For this marker-only case, read the marker's logical index timestamp. The
  internal `bi_get_delete_marker()` helper uses the existing binary `"\0d"`
  selector for the `name\0i\0d` marker record. This is a read selector, not a new
  deletion identity or a new index format.
- Check the mutable NULL target's epoch atomically in CLS before unlinking. A
  newer NULL instance, or a newer matching current NULL OLH, must survive a stale
  incoming unlink. Record a matching STALE acknowledgment instead of leaving the
  pending operation unacknowledged.

The patch retains ACL handling, ordinary data timestamp checks, deletion
preconditions, retry behavior, and zones trace. It does not globally ignore
ENOENT or remove protection against genuinely newer writes.

### Commit `06fcf07ab4c`: preserve admin-purge NULL intent

Populate the existing parameter in `RadosObject::delete_object()` before backend
normalization:

```cpp
del_op.params.null_verid = (get_instance() == "null");
```

This lets the existing bilog machinery preserve explicit NULL identity. It does
not treat every empty instance as an explicit NULL deletion, change numbered
version IDs, or introduce a new SAL API or persistent format.

**Both fixes matter:** the producer must describe the correct instance, and the
receiver must be able to unlink its marker safely. Fixing only the receiver does
not correct admin-purge events that lose their identity at the source.

## Test description

### Environment, fixture, and safety rules

Tests use the existing `exercise.py` harness in this directory, not a separate
replacement test. There are two read/write RGW zones, with RADOS storage and
natural bidirectional replication. The six backports listed above remain
applied. Each end-to-end case creates a **fresh owned test bucket**.

For the end-to-end cases, all fixture writes and requested deletions are issued
on Zone1. Setup is:

1. Create an Enabled-versioning bucket and write a numbered `seed` version.
2. For each of three `history` keys, write two numbered data versions. For each
   of three `marker` keys, write data and issue a plain DELETE to create a
   numbered marker. Retain the underlying numbered data.
3. Suspend versioning. Write three `data` keys and overwrite the three `history`
   keys with NULL data. Create three separate `nullmarker` keys by writing NULL
   data and issuing a plain DELETE. Write the NULL `final` object.
4. At each setup stage, wait for natural replication on both zones. Verify saved
   bucket identities, versioning state, and exact version lists. Before deletion,
   check the expected current and numbered payloads on both zones.

With `--objects 3`, the populated fixture is:

| Before deletion, per zone | Count |
|---|---:|
| Numbered data versions | 10 |
| NULL data versions | 7 |
| Numbered delete markers | 3 |
| NULL delete markers | 3 |
| Readable current keys | 8 |
| Raw objects observed in the admin-purge fixture | 24 |

Raw objects include storage heads and metadata; their count is not the same as
the number of S3 version-list rows. Payloads are small, approximately 4.5 KB;
these are not large-object or multipart/GC-tail tests.

The two end-to-end cases use **no copy, rename, manual full-sync reset, bulk
DeleteObjects request, Zone2 manual cleanup, or direct RADOS deletion**. The
purge case deletes only its fresh Zone1 test bucket. Do not run it against a
production bucket.

Run the commands below from the existing lab's `build/` directory. They require
the harness's saved owned-cluster configuration and matching builds. The recorded
lab also used the optional `RGW_REPRO_SSH_CONTROL_PATH` setting for an existing
SSH control connection; that setting is not part of RGW deletion semantics.

### Test A: remove versions and markers through individual S3 DELETEs

**Purpose:** prove that explicit NULL-marker deletion replicates, without
mistaking ordinary DELETE success or empty current listings for full emptying.

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --no-copy-rename --objects 3 --timeout 180
```

The harness performs this sequence on Zone1 only:

| Step | Requests | Expected state on each zone after catch-up |
|---|---|---|
| Delete all eight current keys without VersionId | 8 ordinary DELETEs | 10 numbered data versions, 0 NULL data, 14 markers |
| Delete the retained numbered data by VersionId | 10 explicit-version DELETEs | 0 data versions, 14 markers |
| Delete every remaining marker by VersionId | 14 explicit-version DELETEs: 11 NULL and 3 numbered | 0 data versions, 0 markers |

There are **32 DELETE requests in total**: eight ordinary and 24 explicit-version
requests. Numbered data is removed before its latest markers, so marker removal
does not accidentally expose old payloads.

After every request, the test checks the S3 response, natural replication,
complete expected version lists, current-read behavior, and remaining payloads
on both zones. It stops at the first inconsistency rather than continuing and
hiding the failure with later cleanup.

Final acceptance requires two consecutive successful observations of:

- Empty current-object and version/marker lists on both zones.
- Zero raw objects under each saved bucket marker.
- Both original bucket identities still present with versioning Suspended.
- Both original bucket-sync streams caught up.

**This test does not purge the bucket.** It tests replicated individual deletion
and complete emptying while retaining the bucket metadata.

### Test B: purge the populated bucket without first emptying it

**Purpose:** verify that the admin-purge path replicates deletion of numbered
and NULL instances, and that namespace deletion is accompanied by physical
cleanup.

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-purge --no-copy-rename --objects 3 --timeout 180
```

Start from a new populated fixture. Do **not** perform Test A's eight plain
DELETEs or its explicit-version cleanup. After the pre-purge parity and payload
checks, issue exactly one administrative purge on Zone1:

```bash
radosgw-admin bucket rm --bucket <fresh-owned-test-bucket> --purge-objects
```

The harness observes both zones using the saved bucket IDs/markers, even after
the bucket metadata disappears. Final acceptance requires:

1. The purge command returns 0.
2. Both S3 namespaces report `NoSuchBucket`.
3. Both expected global peer data streams are caught up; Zone2 metadata is caught
   up with the master. Status identities must match the saved lab topology.
4. Complete raw inventories show **0 objects on Zone1 and 0 on Zone2**.
5. All these conditions hold in two consecutive observations.

After `NoSuchBucket`, per-bucket sync status is **NA**, not a caught-up result.
The global sync gate and raw inventories are separate requirements. A missing
observation, transport failure, or unavailable inventory is incomplete evidence,
not an empty bucket. The focused case uses exit 0 for a pass, 1 for a measured
failure, and 2 for an incomplete run.

This case measures observable end-to-end cleanup. It does not directly assert
the numeric bilog flag values after purge.

### Test C: marker lookup and stale-unlink safety in CLS/RADOS

Three focused tests in `src/test/cls_rgw/test_cls_rgw.cc` ran on each node
against its updated OSD CLS code:

| Test | What it checks |
|---|---|
| `cls_rgw.olh_stale_unlink_preserves_newer_null_data` | A newer NULL data instance survives an older unlink. The instance, listing, and authoritative OLH state stay unchanged except for a matching STALE acknowledgment. |
| `cls_rgw.olh_stale_unlink_preserves_newer_null_delete_marker` | The same protection holds when the newer NULL instance is a delete marker. |
| `cls_rgw.bi_get_null_delete_marker_binary_selector` | The existing binary selector retrieves the actual NULL-marker record, including its exact logical mtime and owner. Literal `"null"` returns ENOENT in this raw getter; an empty selector reads a plain placeholder instead. |

The two stale-unlink tests deliberately install a replacement at epoch 200 before
replaying an unlink at epoch 100. They test both empty and literal-NULL spellings.
They also isolate a newer current-NULL OLH at epoch 300, verify that a fresh/equal
unlink is accepted, and verify that numbered-version deletion and deletion of
older NULL history under a newer numbered head are not incorrectly blocked.

These are deterministic index-operation tests, not a concurrent thread-stress
test. They verify the race safeguard's state transitions and acknowledgments;
the end-to-end tests separately verify actual replica cleanup.

## Recorded results

| Case | Before fix | After fix |
|---|---|---|
| Test A: explicit NULL-marker deletion | Run `rgwlab-20261009-205217-ac14`: first NULL-marker removal left 13 markers on Zone1 and 14 on Zone2 despite caught-up status. | Run `rgwlab-20261010-010600-1e91`: all 32 requests passed; both zones had zero versions, markers, and raw objects, with the suspended buckets retained. Exit 0. |
| Test B: populated-bucket admin purge | Run `rgwlab-20261010-204715-d83a`: namespaces absent and global sync caught up, but raw counts were 0/10. Seven retained payloads were verified byte-for-byte. | Run `rgwlab-20261010-223505-93c2`: namespaces absent, global sync caught up, and raw counts 0/0 twice. Exit 0. |
| Test C: focused CLS/RADOS checks | Added to protect the marker lookup and mutable-NULL unlink behavior. | All three tests passed on each node. |

Both-node `ninja -j10` builds and the relevant Python compilation/whitespace
checks passed during implementation. The first fix required loading the updated
CLS code on the owned OSDs as well as restarting RGW. The second changed only
RGW code and was followed by owned-gateway restarts.

These results were collected before the final commit-history rewrite, against
the corresponding fixed source bytes. Historical reports therefore mention
earlier commit IDs or uncommitted patches. They are not new test runs on the
current commit labels. No builds or tests were rerun to write this issue draft.

## Log evidence for both failures

These are selected **pre-fix Zone2 gateway logs**, not new test output. `...`
marks omitted thread/debug/context fields and long bucket/zone identifiers.
Timestamps, object keys, operation codes, return codes, epochs, and cursor values
shown below are unchanged. Full selected lines are retained in the cited files;
HTTP signing/authentication material is excluded.

### Case 1: explicit NULL identity received, ENOENT returned, cursor advanced

Source:
`build/rgw-repro/runs/rgwlab-20261009-205217-ac14/logs/zone2-null-marker-removal-confirmed.txt`.

```text
2026-10-09T21:08:17.791+0000 ... entry[data0000]: ... log_entry=00000000024.352.14 op=6 op_state=1
2026-10-09T21:08:17.791+0000 ... _send_request(): deleting obj=...:data0000[null]
2026-10-09T21:08:17.791+0000 ... _send_request(): get_obj_state() obj=...:data0000[null] returned ret=-2
2026-10-09T21:08:17.791+0000 ... entry[data0000]: failed, retcode=-2 ((2) No such file or directory)
2026-10-09T21:08:17.791+0000 ... updating marker marker_oid=... marker=00000000024.352.14 ...
```

`op=6` is `UNLINK_INSTANCE`, `op_state=1` is COMPLETE, and `ret=-2` is ENOENT.
The receiver had the correct `[null]` identity, but state loading failed before
unlinking. It then advanced the cursor over the same entry. The version-list
checkpoint independently confirmed that the marker remained on Zone2.

The intermediate receiver fix also exposed the timestamp mismatch addressed by
the complete marker fix. Source:
`build/rgw-repro/runs/rgwlab-20261009-222744-738e/logs/zone2-null-marker-timestamp-skip.txt`.

```text
2026-10-09T22:51:28.561+0000 ... _send_request(): skipping object removal obj=...:nullmarker0000[null] (obj mtime=2026-10-09T22:31:48.011229+0000, request timestamp=2026-10-09T22:31:20.014773+0000)
```

The separate read-only bucket-index inventory in that file contains:

```text
entry.name: nullmarker0000
entry.instance: ""
entry.meta.mtime: 2026-10-09T22:31:20.014773Z
entry.tag: delete-marker
```

The logical marker timestamp matches the deletion request; only the physical
head timestamp is later. This is why allowing ENOENT alone was not sufficient.

### Case 2: admin-purge removal arrived without explicit NULL identity

Source:
`build/rgw-repro/runs/rgwlab-20261010-204715-d83a/logs/zone2-admin-purge-null-intent-trace.txt`.

```text
2026-10-10T20:50:36.589+0000 ... entry[data0000]: ... log_entry=00000000014.253.17 op=6 op_state=1
2026-10-10T20:50:36.589+0000 ... _send_request(): deleting obj=...:data0000
2026-10-10T20:50:36.589+0000 ... manifest: total_size = 4480
2026-10-10T20:50:36.593+0000 ... olh_log_entry: epoch=1791665436596393995 op=1 key=data0000[]
2026-10-10T20:50:36.593+0000 ... olh_log_entry: epoch=1791665436596393995 op=4 key=data0000[]
2026-10-10T20:50:36.593+0000 ... entry[data0000]: success
2026-10-10T20:50:36.605+0000 ... _send_request(): deleting obj=...:nullmarker0000
2026-10-10T20:50:36.605+0000 ... _send_request(): get_obj_state() obj=...:nullmarker0000 returned ret=-2
2026-10-10T20:50:36.609+0000 ... entry[nullmarker0000]: failed, retcode=-2 ((2) No such file or directory)
```

Unlike Case 1, the `_send_request()` targets have **no `[null]` suffix**. This is
the relevant identity check; empty brackets in normalized OLH records alone
would not establish lost NULL intent. OLH `op=1` is LINK and `op=4` is STALE in
this patched stack. The data event reported success after those acknowledgments,
while the marker-only event returned ENOENT before unlinking.

The manifest line shows a payload was loaded, not that it was deleted. Retention
was independently established by the post-purge raw inventory and payload GETs:
seven bodies still matched their fixture payloads. The excerpt does not expose
numeric bilog flags or the exact comparison values inside the acknowledgment
branch; the missing flag is established by the producer code analysis.

## Acceptance criteria and limits

- A replicated explicit NULL-marker deletion reaches index-backed unlinking
  without confusing marker-only state with an absent target.
- Marker timestamp checks use logical marker metadata, while readable-data
  preconditions remain intact.
- Admin-purge NULL events preserve their explicit instance identity.
- Newer mutable NULL replacements survive stale unlinks, with pending operations
  acknowledged rather than silently abandoned.
- The two end-to-end cases meet their distinct logical and physical end states
  without manual replica cleanup.

The atomic CLS safeguard requires updated OSD code and comparable, nonzero
incoming epochs. Zero incoming epochs retain the previous behavior. The tests
do not certify mixed-version deployment, clock rollback, production scale,
large/multipart cleanup, or the separate bulk DeleteObjects caller.

The fixes do **not** automatically replay deletion events that were already
consumed incorrectly. Earlier failed fixtures were retained; no retrospective
repair or blind raw-object deletion was performed.

Upstream `v20.2.4` source does not contain these two local fixes. That is a
source/history finding, not a runtime reproduction on v20.2.4.

## Source and evidence references

Current delivery source:

- `src/rgw/driver/rados/rgw_cr_rados.cc:883–969` — explicit-NULL receiver state,
  logical marker timestamp, and deletion parameters.
- `src/rgw/driver/rados/rgw_rados.cc:8862–8887` — compatible marker-index lookup.
- `src/cls/rgw/cls_rgw.cc:2287–2300` — atomic stale-NULL safeguard.
- `src/cls/rgw/cls_rgw_types.h:86–101,512–518` — state and operation codes used
  to interpret the log excerpts.
- `src/rgw/driver/rados/rgw_sal_rados.cc:2300–2314` — admin-purge NULL intent.
- `src/test/cls_rgw/test_cls_rgw.cc:1021–1333` — focused CLS/RADOS tests.

Detailed reports in this directory:

- [Original no-copy/no-rename failure](NO-COPY-RENAME-results.md).
- [Intermediate receiver fix and physical-vs-logical timestamp evidence](NULL-MARKER-RECEIVER-FIX-results.md).
- [Complete marker fix, safety tests, and passing emptying run](NULL-MARKER-TIMESTAMP-FIX-results.md).
- [Admin-purge failure, producer diagnosis, and passing retest](NO-COPY-ADMIN-PURGE-results.md).

Per-run evidence is retained under `build/rgw-repro/runs/<run-id>/`. Relevant
artifacts include `report.json`, per-DELETE/checkpoint records, final-empty raw
inventories, purge observations, and the pre-fix retained-payload verification.
Credentials, private lab state, and authentication logs are not included here.
