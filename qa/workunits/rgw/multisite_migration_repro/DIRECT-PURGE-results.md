# Direct multisite purge after rename

**Date:** October 9, 2026. **Result: FAIL.** On this patched Reef build,
`bucket rm --purge-objects` on Zone1 removed OLD locally and removed its visible
bucket metadata on both zones, but **did not clean OLD's data on Zone2**.

**Synchronization qualification:** before purge, both renamed bucket names/IDs
were visible and data listings matched, but OLD's post-rename status was still
**behind on 8 shards in both zones**. This is a failure of purge before that
checkpoint settled, not proof of failure after full post-rename synchronization.
Dedicated `migration-synced-*` cases now require that stronger gate before any
deletion; their results must be evaluated separately.
Those comparisons are now recorded in
[SYNCED-DELETE-results.md](SYNCED-DELETE-results.md): after a passing full
post-rename gate, administrative purge still left 0/10 OLD raw objects. The
enumeration case was blocked before deletion, and the SDK rejected nonempty OLD.

This test closes the gap in the earlier #67661 retest, which explicitly deleted
versions on both zones before bucket removal. That earlier 0/0 result must not be
used as proof that a direct populated-bucket purge cleans both zones.

## Exact tested workflow

Dedicated selector: `migration-direct-purge` in `exercise.py`.

1. Create OLD, enable versioning, write numbered history and numbered markers.
2. Suspend versioning; add null data, null overwrites, and null markers.
3. Confirm matching OLD version lists and replication checkpoints on both zones.
4. Copy current objects to never-versioned NEW, perform the final copy pass, and
   checkpoint both buckets before rename. This uses boto3 CopyObject, not rclone.
5. Rename OLD to a temporary name and NEW to the original name; verify the saved
   OLD and NEW bucket IDs on both zones. Confirm NEW's exact listing and known
   fixture payloads before purge.
6. Run **only on Zone1**:

   ```bash
   radosgw-admin bucket rm --bucket=OLD-TEMPORARY-NAME --purge-objects
   ```

7. Poll metadata and read-only physical inventories by OLD's saved marker on
   both zones, while verifying NEW's identity, listing, and payloads.

**No per-zone object/version cleanup, Zone2 purge, manual bucket sync, lifecycle
cleanup, or direct RADOS deletion was performed.** The automatic regression
fails unless both OLD inventories are empty and NEW remains correct for two
consecutive observations. Missing metadata is not treated as empty storage.

Run from the local build directory against the existing owned lab:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-direct-purge --objects 3 --timeout 180
```

## Observed result

Run: `build/rgw-repro/runs/rgwlab-20261009-085339-cf44/report.json`.

- Source/test backport remained unchanged: base `10cef5efe60f`, with #65158,
  #65948, #58133, #62469, and #67661 adaptations applied on both nodes.
- OLD bucket ID on both zones: `ef1724bd-03f2-484a-841f-37232e7a8ebc.4243.13`.
- NEW bucket ID on both zones: `ef1724bd-03f2-484a-841f-37232e7a8ebc.4243.14`.
- Before purge, both OLD lists matched: **17 data versions** (7 null and 10
  numbered), plus **6 delete markers** (3 null and 3 numbered).
- The purge command returned **0**. The regression completed and returned
  **1**, correctly identifying a backend failure rather than infrastructure loss.

| Observation | Zone1 | Zone2 |
|---|---:|---:|
| OLD namespace after purge | `NoSuchBucket` | `NoSuchBucket` |
| OLD raw objects before purge | 24 | 24 |
| OLD raw objects after purge | **0** | **24** |
| Nonempty OLD payload objects retained | 0 | **17** |
| Zero-size OLD heads retained | 0 | **7** |
| NEW saved bucket ID preserved | Yes | Yes |
| All 8 expected NEW payloads match | Yes | Yes |

The polling budget was 180 seconds; serial command/SDK observations made actual
observation time **186.88 seconds**. Zone2's 24 OIDs and sizes remained the same
as before purge. OLD disappeared from Zone2's namespace between the first two
observations, but its physical inventory did not change.

A read-only follow-up at **09:17:18 UTC** still found 0/24 OLD raw objects.
Direct reads of all **17 nonempty Zone2 payload objects matched their known
fixture contents**. These are retained data, not merely empty OLH bookkeeping.

Artifacts under that run directory:

- `migration-direct-purge-old-before-purge.json`
- `migration-direct-purge-old-before-purge-physical.json`
- `migration-direct-purge-purge.json`
- `migration-direct-purge-purge-observations.json`
- `migration-direct-purge-old-after-purge-metadata.json`
- `migration-direct-purge-old-after-purge-physical.json`
- `migration-direct-purge-new-after-old-purge-metadata.json`
- `migration-direct-purge-new-after-old-purge.json`
- `migration-direct-purge-late-physical.json`
- `migration-direct-purge-retained-peer-payloads.json`
- Redacted logs under `logs/`.

## Source interpretation

The administrative Rados SAL path lists versions, deletes local objects, and
then removes the bucket. It does not perform a distributed peer-purge or wait
for a peer-deletion acknowledgment before metadata removal:
`src/rgw/driver/rados/rgw_sal_rados.cc:423–472`.

The helper requests operation logging, but logging is not a peer-completion
barrier: `src/rgw/driver/rados/rgw_bucket.cc:153–165`.

Rename is an additional synchronization risk: the sync-status object names use
the bucket name as well as its ID. A newly initialized renamed stream can start
incremental replay at the source's current bilog watermark; full sync of an
already-empty source does not reconcile destination-only objects. This is a
**candidate explanation**, not a proven purge-time scheduling trace:
`src/rgw/rgw_basic_types.cc:61–78` and
`src/rgw/driver/rados/rgw_data_sync.cc:3643–3705,6440–6468`.

The failure retains numbered payloads too, so it must not be attributed solely
to the separate bulk-null deletion defect. No backend fix was added in this task.

## Separate fixture-preparation failure

The first attempt, `rgwlab-20261009-084353-dd99`, put null markers **above existing
numbered history**. It stopped before copying/rename/purge because OLD's lists
never matched: Zone1 had 3 null markers, Zone2 had none, although sync status
reported caught up. Its report is incomplete, not a purge verdict.

That fixture and a read-only `null-marker-history-parity-failure.json` snapshot
are retained. To isolate direct purge, the completed test uses standalone null
marker keys while retaining numbered history and numbered markers in separate
cohorts. It does not establish that null-marker/history replication is repaired.

## Validation and practical conclusion

- Changed Python passed `python3 -m py_compile`; helper self-tests passed.
- `ninja -j10` succeeded from local `build/` after the test changes.
- Independent review addressed observation failures, pre-purge NEW validation,
  known-write payload oracles, selector/backlog compatibility, and failure exit
  codes. Whitespace checks passed against the saved original harness blobs.
- Existing C++ backports, original eight explicit case paths, and prior fixtures
  were preserved. Both clusters were reused without reset.

**Do not treat rename metadata visibility as permission to immediately purge
OLD before its renamed data-sync checkpoints settle.** The previous per-zone object cleanup
workflow is a different tested procedure. Do not assume a later Zone2 purge by
name remains available once the namespace has already disappeared.

Leftovers and replacement buckets remain available for investigation. No claim
is made about production-scale, multipart, other placements, or GC behavior.
