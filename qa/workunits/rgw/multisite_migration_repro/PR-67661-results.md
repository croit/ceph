# Retest: PR #67661 and timestamp-epoch prerequisite #62469

**Date:** October 9, 2026. **Result:** the deleted-key OLH heads and OLD-bucket
purge leftovers are gone in both updated lab zones. Bulk null-version deletion
remains broken and was deliberately not changed.

**Multisite purge scope clarification:** the successful OLD-bucket purge results
below followed explicit version/marker cleanup on both zones. A later dedicated
test skipped that cleanup and purged the populated renamed OLD only on Zone1:
the command succeeded, but Zone2 retained 24 raw objects despite `NoSuchBucket`
on both zones. See [DIRECT-PURGE-results.md](DIRECT-PURGE-results.md). The earlier
0/0 results do not certify direct multisite purge.
That direct-purge run had matching renamed identities but OLD was still behind
on 8 shards in both zones. Its negative result does not answer the fully
post-rename-synchronized case; the dedicated synchronized comparisons do.
See [SYNCED-DELETE-results.md](SYNCED-DELETE-results.md) for those comparisons:
administrative purge still left 0/10 after a confirmed post-rename checkpoint;
the source-only enumeration variant was blocked before deletion by listing
divergence; direct S3 DeleteBucket returned the expected `BucketNotEmpty`.

## Applied scope

Backported the complete [PR #67661](https://github.com/ceph/ceph/pull/67661)
series, with the approved timestamp-epoch prerequisite
[PR #62469](https://github.com/ceph/ceph/pull/62469), to both development nodes.
Earlier PRs #65158, #65948, and #58133 remain applied. No separate bulk caller,
lifecycle, or #55162 fix was added.

Pinned prerequisite: `4195486c8edc68bfdf5cd4d87e04ef67f35a9364`.
The #67661 series comprises `75c7b8ece796`, `b4b8c63ace17`, `c6825a58cc70`,
`b438348a65e8`, `2d5d75f75964`, `3ae00074d901`, and `1c7171bb8615`.

The backport separates the target version's epoch from local OLH-log ordering.
Successful operations that do not change the head now acknowledge their pending
xattrs. Promotion keeps the successor's original epoch instead of assigning the
newer unlink epoch.

Reef-specific compatibility and safety adaptations:

- Retain the original v1 persistent OLH and log-entry encodings. The read RPC's
  `get_stales` flag uses v2/compat-1; default-false readers filter STALE records
  **before** pagination. No custom persistent clock field or RESTORE opcode.
- Use Reef's existing bounded unsigned parser rather than changing common code.
- Normalize the exact object's complete legacy wide-counter list-key range
  atomically before ordering-dependent mutations, using 128-key read pages and
  authoritative instance metadata. Preserve the null DATA/DM distinction when
  removing obsolete duplicates. Untouched read-only histories are not migrated.
- Read the complete pending-log batch before destructive application, allowing
  a later-page relink to cancel an earlier instance removal.
- Preserve successful non-current DATA restoration with existing operations:
  `LINK(restored), LINK(current)` in one epoch/vector. The Rados planner respects
  vector order and does not repeat the remote version/SID head selection.
- Do not roll back an already-applied target on replay; nevertheless retain
  terminal UNLINK cleanup intent when `clear_olh()` needs retry.
- Retain the prior single-null-delete flags, appended/defaulted helper arguments,
  and actual-null-key guard on physical removals. Bulk handling is unchanged.

All changes remain **uncommitted** on base HEAD
`10cef5efe60f078b70208ec17f03305a8dcc4100` (`croit_18.2.8_core1`). Both nodes'
combined source/test patches and all 19 changed file-content hashes matched.
The patch includes tracked diffs plus the two new, untracked helper/test files:

```text
SHA-256(combined source/test patch)
aa96b281868b932465171c44216d5bedd74c4f3550deec1535e626e4dde8802d
Local Git blob snapshot: 7c95fa959c9afcd38fea522fe3f4f86635def9f9
```

## Build and tests

- Full `ninja -j10` succeeded from `build/` on both nodes. An initial helper
  namespace collision with global `rados::cls` was corrected before retesting.
- Changed existing C++ was formatted using diff-derived line ranges; the two new
  C++ files were formatted fully. `git diff --check` passed on both nodes.
- `python3 -m py_compile src/test/rgw/rgw_multi/tests.py` passed on both nodes;
  the local lab/exercise Python files also passed.
- **21 focused CLS tests passed on each node:** 19 OLH mutation/index tests and
  two wire/record compatibility tests. Coverage includes stale acknowledgments,
  preconditions, non-current restoration, promotion, legacy mixed ordering,
  normalization pagination, null-variant collisions, and read pagination.
- **11 pure Rados OLH tests passed on each node**, including cross-page relink,
  replay protection, cleanup retry, acknowledgment-only behavior, unknown ops,
  and pagination errors. Built explicitly with
  `ninja -j10 unittest_rgw_olh`, then ran `bin/unittest_rgw_olh`.
- Added multisite-framework regressions for version promotion/deletion and OLH
  cleanup. The full upstream teuthology/nose framework and lifecycle regression
  were **not run**; the live eight-case boto3 exercise was run separately.

Fresh multisite reset and all eight cases:

```bash
python3 ../qa/workunits/rgw/multisite_migration_repro/lab.py setup --reset --run --objects 3 --timeout 90
```

Evidence:

- Before this backport: `build/rgw-repro/runs/rgwlab-20261009-005739-11cf/report.json`.
- After this backport: `build/rgw-repro/runs/rgwlab-20261009-055443-e92c/report.json`.
- The new report completed at `2026-10-09T06:09:29Z`. Exit 0 means the exercise
  completed, **not** that the intentionally deferred bulk case passed.

## Results

Counts below are Zone1/Zone2, with three keys per cohort.

| Case | Before | After | Verdict |
|---|---|---|---|
| Never-versioned control | Pass | Pass | No regression |
| Plain suspended DELETE | Pass | 0/0 null data; matching expected null markers | No regression |
| Single explicit null DELETE | Pass | 0/0 data versions and markers | No regression |
| Bulk explicit null DeleteObjects | Zone2 retained 3 readable payloads | Same 3 readable payloads, despite caught-up status | **Still broken; deferred** |
| Null beneath numbered current version | Pass | All 3 numbered payloads preserved per zone; final version deletion succeeds | No regression |
| Non-current-marker/final-head cleanup | 3/3 deleted-key heads | **0/0 deleted-key heads** | **Fixed in this run** |
| Copy/rename and replacement reads | Pass | All sampled replacement payloads match before/after OLD purge; no copied OLH attrs observed | No regression |
| OLD purge, markers retained | 3/6 raw leftovers | **0/0 raw leftovers** | **Fixed in this run** |
| OLD purge after marker reaping | 0/3 raw leftovers | **0/0 raw leftovers** | **Fixed in this run** |

The pending-head case still has one zero-size object per zone: the deliberately
retained, legitimate seed head. It is not a deleted-key orphan.

Both migration cases had successful manual sync, no version/marker cleanup
errors, successful administrative purge, and zero raw objects under the OLD
bucket IDs. Replacement reads remained correct. **No direct RADOS object
deletion was used for bucket cleanup.**

## Limits and rollout cautions

- This validates two fully updated disposable zones, not a production-scale
  131-million-object cleanup, retrospective repair, or a mixed-version rollout.
- V1 byte compatibility is not proof of semantic rolling-upgrade compatibility:
  older RGW readers retain replay/cancellation defects; old counter writers can
  reintroduce legacy history. A coordinated upgrade strategy still needs review.
- Upstream wall-clock log epochs assume forward progress; rollback and repeated
  timestamps are not made safe by a custom persistent high-water clock here.
- Collecting a complete pending-log batch and normalizing one object's wide
  legacy history use resources proportional to that per-object history. No
  production stress/large-history benchmark was run.
- A non-promoting null DATA relink under an absent OLH returns `-ECANCELED` rather
  than encode an operation sequence that could delete the restored null payload.
  The focused CLS test covers this safety boundary; retry/availability behavior
  for that exceptional case is not certified by the eight live scenarios.
- The customer's exact persistent rename-sync lag remains unconfirmed. The OLD
  lists matched in this run; temporarily behind shards caught up after manual
  sync. No claim that the customer lag or the bulk-null defect is fixed.

Both clusters remain running with current fixtures and private artifacts
retained. Historic reports survived reset; previous lab fixtures did not.
