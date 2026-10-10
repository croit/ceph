# Single-object DELETE after synchronized bucket rename

**Date:** October 9, 2026. **Result: PASS for this tested Zone1 → Zone2 workflow.**
After copy, bucket-name switch, and confirmed post-rename synchronization, all
eight plain S3 DeleteObject requests on Zone1 produced consistent results on
both zones. No bulk deletion, explicit-version deletion, or bucket purge was
performed.

## Workflow

```text
Create historically versioned OLD, then suspend versioning
                         ↓
Copy current objects into never-versioned NEW
                         ↓
Rename OLD → temporary name; NEW → original name
                         ↓
Synchronize both renamed buckets in both directions
Verify IDs, complete listings and known payloads twice
                         ↓
Single DeleteObject on OLD's temporary name, Zone1
                         ↓
Wait for replication; check both zones after this key
                         ↓
Repeat for the next key only if every check passes
```

Object keys are not renamed. The request is
`s3.delete_object(Bucket=retired_name, Key=key)` **without VersionId**. This is
plain DELETE, not DeleteObjects and not `bucket rm --purge-objects`.

## Evidence

Selector: `migration-synced-single-delete` in `exercise.py`.

Run: `build/rgw-repro/runs/rgwlab-20261009-132521-57c7/report.json`.

- OLD ID on both zones: `ef1724bd-03f2-484a-841f-37232e7a8ebc.4243.31`.
- NEW ID on both zones: `ef1724bd-03f2-484a-841f-37232e7a8ebc.4243.32`.
- Post-rename gate passed: all four OLD/NEW incremental streams caught up twice,
  correct distinct name/ID mappings, OLD's complete version listings match the
  frozen fixture, and NEW's eight known payloads match.
- Before DELETE, OLD has 17 data versions (7 null, 10 numbered) and 6 markers.
  The numbered cohort is independently checked against the fixture writes, not
  accepted merely because a shortened listing agrees between zones.
- Eight requests were sent, all keys processed, every step passed, final case
  `passed=true`, exercise `completed=true`, exit 0. Finished at **13:35:33 UTC**.

Both zones had the following matching data-version/marker counts after each
request:

| Deleted current key | Data versions | Null data | Markers | Null markers |
|---|---:|---:|---:|---:|
| `data0000` | 16 | 6 | 7 | 4 |
| `data0001` | 15 | 5 | 8 | 5 |
| `data0002` | 14 | 4 | 9 | 6 |
| `final` | 13 | 3 | 10 | 7 |
| `history0000` | 12 | 2 | 11 | 8 |
| `history0001` | 11 | 1 | 12 | 9 |
| `history0002` | 10 | 0 | 13 | 10 |
| `seed` | 10 | 0 | 14 | 11 |

The seven null data payloads were removed on both zones. The history keys had
numbered versions beneath their current null data; those numbered payloads
remained correct. `seed` had a numbered current version, which plain DELETE
preserved as history while adding a null marker.

## Checks after every DELETE

- Response is HTTP 204 with a null-version delete marker.
- Existing renamed streams catch up again; no manual full-sync reset is used
  after the DELETE.
- Complete version lists, latest flags, and current-object listings match the
  expected suspended-versioning semantics on both zones.
- Current GET of the deleted key returns **404 NoSuchKey** on both zones.
- Every remaining current OLD payload is readable and correct before the next
  request, detecting collateral corruption/loss of an undeleted key.
- All 10 numbered OLD payloads remain byte-correct on both zones.
- For keys with null data, read-only physical inspection confirms the co-located
  null body is gone/zero-size, rather than merely hidden from listings.
- NEW's ID, listing, and all eight copied payloads remain unchanged.

Per-step evidence is saved as
`migration-synced-single-delete-single-delete-00.json` through `-07.json`, with
matching `-sync.json` and `-physical.json` artifacts. The pre-deletion checkpoint
is `migration-synced-single-delete-post-rename-sync.json`.

## What this establishes—and what it does not

**Plain single-object DELETE replicated correctly from Zone1 to Zone2 after
the synchronized rename in this run.** This closes the earlier uncertainty
about that specific post-rename operation.

It does **not** mean OLD became empty. The expected final state contains
**10 numbered data versions and 14 delete markers on each zone**. Their
payloads/head objects are retained deliberately. Removing current objects is
not equivalent to deleting every historical version or purging the bucket.

It does not change the failed direct administrative purge result or validate
bulk-null deletion. Reverse-origin deletes, concurrent deletions, and
production-scale behavior were not tested here. The earlier null-marker/history
fixture-preparation failures remain separate observations; this passing,
checkpointed sequence is not a blanket repair claim for every timing.

## Run and verification

From local `build/`, without resetting the existing owned clusters:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-single-delete --objects 3 --timeout 180
```

Changed Python passed `python3 -m py_compile`; helper tests and `ninja -j10`
passed. Offline coverage checks suspended-DELETE semantics, numbered-cohort
completeness, undeleted-payload preservation, failure persistence, diagnostic
errors, and SDK/infrastructure errors. The C++ backports were unchanged.

Fixtures, version history, markers, NEW, and artifacts remain available.
**No direct RADOS deletion was used.**
