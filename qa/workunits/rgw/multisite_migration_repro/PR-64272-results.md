# PR #64272: two-node backport and NULL-marker retest

## Result

**The full PR #64272 backport builds on both nodes, but does not fix the
multisite NULL delete-marker removal failure.** The fresh synchronized migration
regression again stopped at the first explicit `VersionId="null"` marker DELETE:
Zone1 removed it; Zone2 retained it despite caught-up bucket sync status.

GET and HEAD of a retained current delete marker returned HTTP 404 with
`x-amz-delete-marker: true` on both patched gateways. Automatic lifecycle threads
remained disabled; this run does not claim to validate lifecycle expiration.

## Source and builds

- Base on both nodes: detached `10cef5efe60f078b70208ec17f03305a8dcc4100`
  (`croit_18.2.8_core1`). Changes remain uncommitted.
- Preserved earlier PRs: #65158, #65948, #58133, #62469, and #67661.
- Applied both commits from PR #64272:
  `224821147f2664e54f81b0bb93ccd23669f31f04` and
  `8654b1f202ad5719901dbcd8c64a384a75a08adc`.
- Reef adaptations use `get_obj_state()`, copy/default-initialize cached `is_dm`,
  expose the state before returning an error, forward marker detection through
  SAL, and preserve the existing `get_delete_marker()` interface.
- Lifecycle tolerates only marker ENOENT and avoids post-delete raw-state pointer
  access in notifications. GET/HEAD retain the original error and emit the marker
  header on the permitted ENOENT path. The focused review found no blocker.
- Exactly eight source files differ from the saved five-PR patch. The combined
  24 source/test files match byte-for-byte between nodes, and unrelated remote
  worktree/index state was preserved.
- Combined patch SHA256:
  `1438c3f49e804d1fbd92fafe08c25e679a2f375316385589d184c79a4aa50db0`.
  Saved Git blob: `2690ec345c9a6fcb5f63be4e2569423491ad9ddc`.
- `ninja -j10` succeeded in both build directories. Zone2 completed 277 scheduled
  steps after CMake regeneration; the local recheck required no C++ recompilation
  after its earlier successful PR build.
- Eleven `unittest_rgw_olh` tests passed on each node. Harness `--self-test`,
  Python compilation checks, and `git diff --check` passed.
- No separate receiver fix, bulk-null fix, or PR #55162 was added.

## Zone2 environment restoration

The host had lost its NVMe mount, leaving the container's `/workspace` bind empty.
The existing ext4 filesystem on `/dev/nvme0n1p1` was clean. It was first mounted
read-only with `ro,noload`; the original checkout, build, and Ceph data were
verified. It was then mounted read-write at the original `/mnt/ceph-dev` path,
and `ceph-dev` was restarted to refresh its `rprivate` bind mount.

Configured cluster identities and the existing OSD identity matched the owned
lab state before startup. The original MON/OSD/MGR data was reused, without
vstart initialization, formatting, repair, reset, or data deletion. The daemon
startup commands unset the client's `CEPH_KEYRING` override so OSD/MGR could use
their configured daemon keyrings. Both live cluster identities, gateway PIDs,
user metadata, and committed multisite topology subsequently validated. No fstab
or host boot configuration was changed.

## Fresh checked regression

Command from local `build/`:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --objects 3 --timeout 180
```

Run: **`rgwlab-20261009-190840-5f8d`**, October 9, 2026, 19:08:40–19:30:31 UTC.
The report completed with a checked regression failure (exit 1), not an
infrastructure/incomplete result.

1. Copy/rename and the strict post-rename synchronization gate passed, including
   two successful observations of all four streams, complete OLD version lists,
   and all eight NEW payloads on both zones.
2. All eight ordinary Zone1 DELETEs without VersionId replicated correctly.
3. All ten individual numbered-data-version DELETEs replicated correctly.
4. Explicit request 11 targeted current marker `data0000`, `VersionId="null"`.
   Zone1 returned HTTP 204 and VersionId `null`. Its marker disappeared, but
   Zone2 retained the marker. All four streams reported caught up; only OLD
   Zone2's complete version list disagreed with the expected result.
5. The harness stopped immediately. The remaining 13 planned marker DELETEs
   were not sent. No Zone2 manual deletion, bucket purge, raw RADOS deletion, or
   full-sync reset was performed after deletion started.

Read-only follow-up at **19:32:38 UTC** confirmed the persistent outcome:

| OLD state | Zone1 | Zone2 |
|---|---:|---:|
| Numbered or null data versions | 0 | 0 |
| Delete markers | 13 | 14 |
| Null delete markers | 10 | 11 |
| Physical raw objects | 13 | 14 |

Both OLD bucket identities remain present. Both NEW identities and all eight
payloads remain correct. The earlier partially consumed fixture was not mutated.

Artifacts under `build/rgw-repro/runs/rgwlab-20261009-190840-5f8d/`:

- `report.json`: complete checked workflow and final failure.
- `migration-synced-empty-version-delete-10.json`: request/response and mismatch.
- `migration-synced-empty-version-delete-10-sync.json`: caught-up status and
  consistency polling.
- `pr64272-late-checks.json`: later counts, GET/HEAD header checks, NEW verification.
- `pr64272-late-physical.json`: read-only raw-object inventory.
- `logs/`: retained redacted cluster and gateway diagnostics.

## Scope of the remaining cause

PR #64272 handles lifecycle's marker-state ENOENT and response marker detection;
it does not change `RGWAsyncRemoveObj::_send_request()` in
`src/rgw/driver/rados/rgw_cr_rados.cc:891–895`. That receiver still returns a
failed state lookup before constructing the delete operation. This remains the
source-supported candidate mechanism for the observed marker replication gap;
the specific receiver return for this request was not captured as an end-to-end
runtime trace. This round establishes that PR #64272 alone is insufficient, not
that a separately implemented receiver fix has been tested.
