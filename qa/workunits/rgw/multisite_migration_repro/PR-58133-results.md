# Retest: PR #58133

**Date:** October 9, 2026. **Result:** explicit single-object null-version
deletion is fixed; bulk null-version deletion and OLH-head cleanup still fail.

## Applied scope

Backported [PR #58133](https://github.com/ceph/ceph/pull/58133), including
`485020f97b0` and its final unlink-flags follow-up `92002be04eb`, to both local
cluster1 and remote cluster2. PRs #65158 and #65948 remain applied. No OLH,
lifecycle, or separate bulk-caller fix was added.

The backport carries explicit-null intent from single DELETE through SAL, Rados,
CLS-client bilog flags, and incremental-sync decoding. The receiver restores
literal `"null"` before deleting the selected version.

Reef-specific adaptations:

- New helper arguments are appended/defaulted, preserving existing `log_op`
  and `force` positions and avoiding the upstream positional-Boolean trap.
- CLS unlink uses its existing version-3 `bilog_flags` field. No intermediate
  version-4 Boolean wire field is introduced.
- Existing literal-null normalization is retained in Reef's purge helper and
  CLS to preserve their old API semantics.
- A safety guard restricts null flags on physical removals to actual null keys.
  This avoids marking queued numbered-version removals as null while draining
  an OLH log during a null request. It is a backport adaptation, not another PR.
- Bulk DeleteObjects, purge shortcuts, and lifecycle callers retain their
  default false null-intent parameter, matching the audited upstream caller gap.

Changes are uncommitted on base HEAD `10cef5efe60f`. Both nodes have identical
combined source diffs (including the prior two PRs):

```text
SHA-256(git diff --binary -- src/rgw src/cls/rgw src/test/rgw/rgw_multi/tests.py)
707401f900662f9ff73a74e74247ff91a5b5b7e1213b14874d7f78ea081b06b1
```

## Build and test

- `ninja -j10` succeeded from `build/` on both nodes: 277 build steps each.
- Changed C++ was formatted using diff-derived line ranges, not whole files.
- Added upstream-style `test_null_version_id_delete()` with nonempty payloads.
  `python3 -m py_compile src/test/rgw/rgw_multi/tests.py` passed on both nodes.
- Extended the live boto3 harness with the equivalent `null-numbered` scenario.
  The upstream teuthology/nose framework itself was not run.
- The changed harness passed `py_compile` and its helper self-test.

Fresh multisite reset and all eight cases were executed:

```bash
python3 ../qa/workunits/rgw/multisite_migration_repro/lab.py setup --reset --run --objects 3 --timeout 90
```

Evidence:

- Before #58133, with only the earlier two PRs:
  `build/rgw-repro/runs/rgwlab-20261008-232852-c087/report.json`.
- After #58133:
  `build/rgw-repro/runs/rgwlab-20261009-005739-11cf/report.json`.

## Results

| Case | Before #58133 | After #58133 | Verdict |
|---|---|---|---|
| Never-versioned control | Pass | Pass | No regression |
| Plain suspended DELETE | Fixed by #65948 | Still matches both zones, no retained null data | No regression |
| Single explicit `versionId=null` DELETE | Zone2 retained 3 readable null versions | 0 data versions and 0 markers in both zones; lists match | **Fixed** |
| Bulk DeleteObjects with null IDs | Zone2 retained 3 readable null versions | All 3 remain readable on Zone2 with original payloads; source has 0 | **Not fixed** |
| Null version beneath numbered current version | Additional focused scenario | Null data removed; all 3 numbered payloads preserved in both zones; deleting them leaves no listed data keys | **Pass** |
| Non-current-marker final-head cleanup | 3 deleted-key heads per zone | Same 3 heads per zone | **Not fixed** |
| Copy/rename and replacement reads | Fixed by #65158 | All tested contents match in both zones before/after OLD purge; no copied OLH attributes observed | No regression |
| OLD purge, markers retained | Raw leftovers 0/10 on Zone1/Zone2 | Raw leftovers 3/6 | Cleanup incomplete |
| OLD purge after marker reaping | Raw leftovers 0/7 on Zone1/Zone2 | Raw leftovers 0/3 | Cleanup incomplete |

Raw-head quantities vary with timing and cleanup state; do not treat their
variation alone as a regression or classify objects using body size alone.
No direct RADOS deletion was performed.

### Remaining bulk failure

The bulk caller does not set `DeleteOp::params.null_verid` when its requested
VersionId is null. Its source deletion succeeds, but the new discriminator is
not supplied to replication. Zone2 reports caught up while all three payloads
remain readable. This caller repair was deliberately not included under the
"only #58133" scope.

### What is not established

- This is a tiny-key functional reproduction, not a 131-million-object benchmark.
- No newly applied fix for the customer's exact persistent rename lag is claimed.
- OLH pending-operation cleanup, lifecycle marker handling, and retrospective
  repair of existing replica surplus remain separate work.

Both clusters remain running with current fixtures and artifacts retained.
Historic reports survived reset; previous lab fixtures did not.
