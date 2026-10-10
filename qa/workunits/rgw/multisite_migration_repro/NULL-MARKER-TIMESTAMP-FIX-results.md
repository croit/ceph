# NULL-marker timestamp refinement: complete emptying passes

## Result

**The requested no-copy/no-rename suspended-bucket workflow now passes on both
patched clusters.** All eight ordinary DELETEs and all 24 explicit-version
DELETEs completed, including the previously failing standalone NULL-marker
cohort. Both zones reached zero data versions, zero markers, and zero physical
raw bucket objects, with caught-up status and two consecutive final observations.

Run: **`rgwlab-20261010-010600-1e91`**, October 10, 2026,
01:06:00–01:31:54 UTC. Completed, **exit 0**, `regression_failures: []`.
Both original suspended bucket identities remain present; no bucket purge or
raw-object deletion was performed.

## Research findings and chosen fix

The initial receiver fix allowed the expected missing-data state but retained a
second mismatch: the timestamp guard compared a replica's locally created raw
OLH head with the source's logical marker timestamp. The measured marker index
mtime equaled the request timestamp, while the raw head was newer.

Two additional details mattered:

1. Literal `"null"` passed to the raw BI instance getter does not select the NULL
   delete-marker record. That record uses the existing `name\0i\0d` encoding.
   An empty selector instead reads the plain version placeholder. The new
   internal RADOS helper copies the object and uses the two-byte `"\0d"` selector
   only for the read, then checks marker type and decoded logical identity. The
   original object/deletion identity remains literal `"null"`.
2. Reading marker metadata before deletion is not an atomic race guarantee.
   A newer mutable NULL data version or marker may commit before unlink. The
   CLS mutation now checks the target/current-NULL-head epochs under the index
   object's lock. An older incoming unlink cannot remove that newer NULL target.

For the explicit-NULL OLH/no-data case, the receiver now compares the marker's
logical index mtime. Readable-data timestamp checks, loaded physical state,
ACL handling, versioned epoch, NULL intent, and zones trace remain intact.
Missing marker metadata retains the existing data/absence path; malformed
metadata or other lookup errors still fail closed.

A rejected stale NULL unlink appends the existing `STALE` acknowledgment for
its pending `op_tag`, without changing the newer target. This allows normal
OLH-log application to remove the pending xattr instead of leaking it through
a bare successful no-op. Missing authoritative OLH state uses the existing
cancellation/retry path rather than creating a phantom head.

## Scope and compatibility

Changed from the initial receiver-fix baseline:

- `src/rgw/driver/rados/rgw_cr_rados.cc`: marker-aware effective timestamp.
- `src/rgw/driver/rados/rgw_rados.cc` and `.h`: internal, nonvirtual marker getter.
- `src/cls/rgw/cls_rgw.cc`: atomic stale-NULL guard and pending acknowledgment.
- `src/test/cls_rgw/test_cls_rgw.cc`: three focused regression tests.

No changes to existing RPC layouts, persistent encodings, public SAL virtual
interfaces, S3 response behavior, or the deferred bulk-delete caller. The new
reader uses the existing index/RPC format, not a new marker representation.
The atomic safeguard requires the updated CLS code on the OSDs and comparable
nonzero writer epochs; zero incoming epochs retain the prior behavior. Existing
#62469/#67661 coordinated-writer, clock, and mixed-version limitations remain.

All six earlier PRs and the initial receiver fix remain applied. The refinements
are uncommitted on detached base `10cef5efe60f078b70208ec17f03305a8dcc4100`.
The review found no blocker in the implemented diff. Changed C++ was formatted
only on diff-derived line ranges.

## Builds and activation

- `ninja -j10` passed on Zone1 (163 scheduled steps) and Zone2 (277 steps after
  CMake regeneration and guarded patch transport).
- Python compilation and whitespace checks passed.
- Pre-update remote source matched the saved initial-fix patch exactly. All 24
  combined source/test file hashes match between nodes after the update;
  unrelated remote worktree/index state was preserved.
- Combined patch SHA256:
  `61ca28cdb1e14c1c24521d07ef94b7a8a3e7b3e4a5de13b3293d1041cb4231ac`.
- Saved patch blob: `ff10132b8f48e2af7300d07f0cda7e1a6285aff2`.
- Restarted the existing owned OSD on each node to load the new CLS module,
  then restarted both RGWs. Live/configured identities and saved topology were
  checked. No vstart initialization, formatting, or data reset occurred.

## Focused safety checks

These three tests passed on **each node** against its updated OSD:

- `cls_rgw.olh_stale_unlink_preserves_newer_null_data`
- `cls_rgw.olh_stale_unlink_preserves_newer_null_delete_marker`
- `cls_rgw.bi_get_null_delete_marker_binary_selector`

They cover both NULL spellings/variants; unchanged newer instance/list/head
state; matching STALE acknowledgments without removal directives; equal/fresh
unlink; numbered targets and older NULL history under a newer numbered head;
and exact logical marker metadata from the compatible binary selector.
This is not a concurrency stress test or a guarantee for incomparable epochs.
No unrelated regression or unit-test suite was run.

## Existing workflow retest

```bash
RGW_REPRO_SSH_CONTROL_PATH=/tmp/opencode/zone2-ssh.sock .venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --no-copy-rename --objects 3 --timeout 180
```

Counts are **numbered data / NULL data / delete markers**, equal on both zones:

| Stage | Counts per zone | Verdict |
|---|---|---|
| Enabled bucket with seed | 1 / 0 / 0 | Consistent, caught up |
| Original numbered-history/marker cohorts | 10 / 0 / 3 | Consistent, caught up |
| Suspend versioning | 10 / 0 / 3 | Both Suspended; consistent, caught up |
| NULL data/marker writes | 10 / 7 / 6 | Consistent, caught up |
| Eight ordinary DELETEs | 10 / 0 / 14 | Passed after every request |
| Ten numbered-data-version DELETEs | 0 / 0 / 14 | Passed after every request |
| All 14 marker DELETEs: 11 NULL, 3 numbered | 0 / 0 / 0 | Passed after every request |
| Final physical and logical verification | Logical 0 / 0 / 0; raw 0 | Passed twice on both zones |

Every emptying DELETE was sent only to Zone1. No copy, rename, manual full-sync
reset, bulk deletion, Zone2 manual cleanup, bucket purge, or raw deletion was
used. Earlier failed fixtures were not manually repaired or discarded.

## Evidence and limits

Under `build/rgw-repro/runs/rgwlab-20261010-010600-1e91/`:

- `report.json`: completed passing run, all setup checkpoints and issued DELETEs.
- `migration-synced-empty-version-delete-20.json`: former `nullmarker0000/null`
  failure now passes.
- `migration-synced-empty-version-delete-23.json`: final `seed/null` removal.
- `migration-synced-empty-final-empty.json`: all final checks true, both buckets
  retained/Suspended, empty lists, caught-up status, two successful observations.
- `migration-synced-empty-final-empty-physical.json`: read-only zero-object
  inventories for both saved bucket markers.
- `logs/`: collected private/redacted cluster and gateway diagnostics.

This validates the requested no-copy/no-rename single-DELETE workflow on two
fully updated disposable nodes. It does not certify production scale, a mixed
upgrade, clock rollback, bulk deletion, retrospective replica repair, or a new
post-fix copy/rename/purge test. Those outcomes are not inferred from this pass.
