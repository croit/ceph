# Deletion comparisons after post-rename synchronization

**Date:** October 9, 2026. Three dedicated tests were added. The administrative
purge still fails multisite cleanup after a confirmed post-rename checkpoint;
the enumeration variant is blocked before deletion by a version-list mismatch;
direct boto3 DeleteBucket correctly rejects the populated bucket.

The earlier [DIRECT-PURGE-results.md](DIRECT-PURGE-results.md) run waited for
renamed names/IDs, not post-rename data catch-up. Its OLD status was behind on
8 shards in both zones. This report evaluates the stronger prerequisite.

## Common fixture and strict gate

Each test creates separate OLD and NEW buckets, writes numbered history into
OLD, suspends versioning, and adds null data and both marker types. NEW is
never-versioned. Current objects are copied with boto3 CopyObject, then OLD is
renamed to a temporary name and NEW takes the original name. Object keys are
not renamed.

For the three-key fixture, OLD initially has identical listings on both zones:
17 data versions (10 numbered, 7 null) and 6 markers (3 numbered, 3 null). NEW
has 8 expected current objects with payloads derived from the fixture writes.
The null-marker cohort is standalone, not layered over numbered history.

After rename, the tests:

- Verify distinct OLD/NEW bucket IDs and correct name mappings on both zones.
- Run `bucket sync run` for OLD and NEW in both directions. The final version
  waits for each direction before initiating its reverse, avoiding overlapping
  opposing replays.
- Require **two consecutive observations** of all four renamed streams in
  completed incremental sync and reporting caught up.
- Require both OLD version lists to match the frozen pre-rename listing and
  NEW's identity, complete listing, and every expected payload to remain correct.

Neither successful commands nor caught-up status alone satisfy the gate. If
version listings differ, **no object enumeration/deletion or bucket deletion is
performed**. The blocked case returns exit 2 and retains its fixture/evidence.

All mutations are restricted to newly generated, owned lab buckets. No direct
RADOS deletion, manual Zone2 object cleanup, or backend fix was performed.

## Latest results

All paths below are relative to `build/rgw-repro/runs/`.

| Test | Post-rename gate | Operation/result | OLD raw objects, Zone1/Zone2 | Verdict |
|---|---|---|---|---|
| `migration-synced-purge` | Passed; all four streams caught up twice and listings match | Zone1 `bucket rm --purge-objects` returns 0; OLD becomes `NoSuchBucket` on both zones | **0/10** | **Cleanup failure, exit 1** |
| `migration-synced-enumerate` | Blocked: OLD's Zone1 version listing differs after synchronization | No explicit-version DELETEs or purge issued | Not a cleanup result | **Incomplete/blocked, exit 2** |
| `migration-synced-boto-delete` | Passed; all four streams caught up twice and listings match | Zone1 boto3 `delete_bucket` returns **409 BucketNotEmpty** | **24/24**, unchanged | Expected rejection verified, exit 0; **bucket not deleted** |

Reports:

- Admin purge: `rgwlab-20261009-112046-1c65/report.json`.
- Enumeration: `rgwlab-20261009-111228-b28b/report.json`.
- Boto3: `rgwlab-20261009-112820-ce78/report.json`.

### 1. Direct administrative purge after catch-up

The final `migration-synced-purge-post-rename-sync.json` records a passing gate,
all four streams caught up on 11 shards, matching versions, and consecutive
success count 2. Both OLD lists still have all 17 versions and 6 markers before
the command. No per-object deletion is performed by the harness.

Purge removes all local OLD objects and its namespace on both zones. Zone2
retains **7 nonempty null-version payload objects and 3 zero-size null-marker
heads** under OLD's saved marker. Numbered payloads are no longer retained.
All NEW IDs, listings, and 8 payloads remain correct.

A read-only follow-up at **11:38:28 UTC** still found 0/10 OLD objects. Raw reads
of all 7 nonempty peer objects matched their known fixture payloads. Evidence:

- `migration-synced-purge-post-rename-sync.json`
- `migration-synced-purge-old-before-purge.json`
- `migration-synced-purge-purge.json`
- `migration-synced-purge-old-after-purge-metadata.json`
- `migration-synced-purge-old-after-purge-physical.json`
- `migration-synced-purge-late-physical.json`
- `migration-synced-purge-retained-peer-payloads.json`

The first completed synchronized trial, `rgwlab-20261009-102505-b020`, also
passed its full gate and left **0/10**, before the synchronization commands were
serialized. This result is not solely the earlier unsynchronized 0/24 failure.

### 2. Enumeration, then empty-bucket purge

Implemented workflow: pass the common gate, enumerate every version and marker
on Zone1, issue single explicit-version-ID DELETEs (including literal `null`),
then require empty OLD lists and caught-up checkpoints on both zones before
issuing administrative purge. No bulk DeleteObjects or Zone2 manual deletion.

**The destructive part was not reached.** The final run records
`blocked_at=post-rename-sync`, `enumerated_deletion_performed=false`, and
`bucket_deletion_performed=false`. OLD's Zone1 listing diverged during sync,
although status reported caught up. NEW remained correct.

Earlier retained attempts show the same gate problem:

- `rgwlab-20261009-103237-87f7` and `rgwlab-20261009-104819-a4dc`, three-key fixtures.
- `rgwlab-20261009-105728-b0da`, a one-key fixture, blocked on Zone2's listing.

In `103237-87f7`, the diagnostic snapshot shows Zone1 gained an additional
non-current `history0001` null-version listing: 18 data entries/8 null on Zone1,
versus the original 17/7 on Zone2. Its
`post-rename-gate-failure-snapshot.json` preserves the exact difference.

The listing mismatch was not repaired or bypassed. **No claim is made that this
source-only enumeration/purge variant works or fails cleanup after a valid
checkpoint.** It remains blocked by the synchronization/version-list issue.

### 3. Direct boto3 DeleteBucket on populated OLD

The exact call is `s3.delete_bucket(Bucket=retired_name)` on Zone1, without
enumeration or purge. After the passing common checkpoint, the server returns
**409 BucketNotEmpty**, as expected for S3 DeleteBucket. It is not a purge API.

The rejection regression passes only when OLD's namespace/ID, all version
entries, raw OIDs/sizes on both zones, and NEW's data remain unchanged. The
report explicitly records `bucket_deletion_succeeded=false`; `passed=true`
means correct rejection, **not successful multisite cleanup**.

The earlier `rgwlab-20261009-104000-213f` trial produced the same rejection and
unchanged 24/24 inventories.

## Commands and validation

Run from local `build/`; each command uses fresh owned buckets without resetting
the clusters:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-purge --objects 3 --timeout 180
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-enumerate --objects 3 --timeout 180
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-boto-delete --objects 3 --timeout 180
```

- `python3 -m py_compile exercise.py` and helper self-tests passed.
- `ninja -j10` succeeded from local `build/` after code changes.
- Changed Python blocks retain neighboring formatting; diff checks passed.
- Independent review tightened incremental-status parsing, distinct-ID checks
  before synchronization/deletion, and checked-case exit-code documentation.
- The existing C++ backports and their source/test fingerprint were not changed.
- SDK errors/inventory failures are not converted into empty buckets or success.

## Practical conclusion

**Waiting for post-rename synchronization did not make a single Zone1
administrative purge complete multisite cleanup in the successful-gate trials.**
Direct boto3 DeleteBucket cannot remove a populated bucket. The new enumeration
workflow must not be recommended as validated: it never passed the required
pre-deletion version-list gate in these attempts. The earlier per-zone cleanup
test is a separate workflow, not evidence for this source-only variant.

All remaining fixtures/artifacts are retained. These are small inline-object
tests, not production-scale, multipart, other-placement, or GC certification.
