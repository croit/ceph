# Existing suspended-bucket emptying test, without copy or rename

## Result

**The same NULL delete-marker replication failure occurs without copying or
renaming the bucket.** All setup checkpoints, eight ordinary DELETEs, and ten
numbered-data-version DELETEs passed. The first explicit NULL-marker DELETE
removed the marker on Zone1 but not Zone2, although both bucket-sync streams
reported caught up.

Run: **`rgwlab-20261009-205217-ac14`**, October 9, 2026,
20:52:17–21:11:16 UTC. The report completed with checked failure **exit 1**,
not an incomplete/infrastructure result.

## Reused code, not a separate deletion test

This ran `exercise.py --case migration-synced-empty --no-copy-rename`. It reuses
the original fixture and the existing `single_deletes()` and `empty_versions()`
request, oracle, checkpoint, and reporting paths. The standalone
`suspended_empty.py` was removed.

The option omits NEW creation, CopyObject, bucket renames, and NEW-protection
checks. It adds setup checkpoints and follows natural synchronization; it does
not run manual full synchronization. Original migration cases still retain
their NEW-protection checks. Both nodes retain the combined six-PR build,
including PR #64272; no additional backend fix was applied.

Command from local `build/`:

```bash
RGW_REPRO_SSH_CONTROL_PATH=/tmp/opencode/zone2-ssh.sock .venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --no-copy-rename --objects 3 --timeout 180
```

Only this regression was run. Python compilation and the required `ninja -j10`
build check passed; no other regression/unit-test suite was run.

## Stage-by-stage observations

All setup and successful deletion checkpoints require two consecutive caught-up,
identity-matching, exact-version-list observations in both directions. Per-DELETE
checks also verify preserved payloads and deleted-key current reads.

Counts below are **numbered data / NULL data / delete markers**:

| Stage | Versioning, both zones | Zone1 counts | Zone2 counts | Consistent after caught-up |
|---|---|---|---|---|
| Create versioned bucket with original seed write | Enabled | 1 / 0 / 0 | 1 / 0 / 0 | Yes |
| Create original numbered-history/marker cohorts | Enabled | 10 / 0 / 3 | 10 / 0 / 3 | Yes |
| Suspend versioning | Suspended | 10 / 0 / 3 | 10 / 0 / 3 | Yes |
| Create NULL data and NULL-marker cohorts, including final write | Suspended | 10 / 7 / 6 | 10 / 7 / 6 | Yes |
| Delete eight current keys individually, without VersionId | Suspended | 10 / 0 / 14 | 10 / 0 / 14 | Yes, after each request |
| Delete all ten numbered data versions individually | Suspended | 0 / 0 / 14 | 0 / 0 / 14 | Yes, after each request |
| First explicit NULL-marker deletion: `data0000/null` | Suspended | 0 / 0 / 13 | 0 / 0 / 14 | **No**, despite caught-up status |

The pre-existing marker cohorts are intentionally retained from the original
migration test. No CopyObject request or bucket rename was issued. Every
emptying DELETE was sent only to Zone1.

## Failed marker request

```python
s3_zone1.delete_object(
    Bucket="rgwlab-20261009-205217-ac14-migration-synced-empty",
    Key="data0000",
    VersionId="null",
)
```

Zone1 returned HTTP **204**, VersionId **`null`**, and removed the marker.
Zone2 retained the exact `data0000/null` delete marker. At the final checkpoint,
both streams were caught up; only `old_zone2_versions_match` was false. Both
current-object listings were empty and both bucket identities remained present.
The remaining marker counts include 10 NULL markers on Zone1 versus 11 on Zone2.

**The existing test stops at the first inconsistency.** It did not send the
remaining 13 planned marker DELETEs, so it did not fully empty either bucket.
No peer cleanup, bucket purge, raw deletion, or post-delete full-sync reset was
performed. The fixture is retained.

## Evidence

Under `build/rgw-repro/runs/rgwlab-20261009-205217-ac14/`:

- `report.json`: `no_copy_rename: true`, completed checked failure, setup
  checkpoints, eight passing ordinary DELETEs, and explicit-deletion results.
- `migration-synced-empty-create-versioned-bucket-with-seed.json`,
  `migration-synced-empty-create-numbered-versions.json`,
  `migration-synced-empty-suspend-versioning.json`,
  `migration-synced-empty-create-null-versions.json`: setup stage evidence.
- `migration-synced-empty-version-delete-10.json`: exact request, HTTP response,
  counts, and retained peer marker.
- `migration-synced-empty-version-delete-10-sync.json`: both streams caught up
  with Zone2's version-list mismatch at 21:11:15 UTC.
- `logs/`: collected redacted gateway and cluster diagnostics.

This establishes that copy/rename is **not required to reproduce the observed
NULL-marker deletion gap**. It is not a test of a new receiver fix or of complete
marker removal after continuing past the failure.

## Receiver root cause confirmed from the full Zone2 log

The original last-8-MiB extract omitted the actual delete processing. A subsequent
read-only lookup in Zone2's full `out/radosgw.8000.log` found the exact event at
**21:08:17.791 UTC**:

1. COMPLETE `UNLINK_INSTANCE` (`op=6`, `op_state=1`), log entry
   `00000000024.352.14`, was received for `data0000`.
2. The receiver restored the literal NULL instance and attempted removal of
   `data0000[null]`.
3. `RGWAsyncRemoveObj::_send_request()` returned **`-ENOENT`** from
   `get_obj_state()` before obtaining/executing the delete operation.
4. The entry logged `failed, retcode=-2` and immediately **updated the sync cursor
   to that same log entry**, leaving the marker indexed.

Exact backend lines, including original source line numbers, are retained in
`logs/zone2-null-marker-removal-confirmed.txt` under this run. Signing/authentication
material and unrelated log lines were deliberately excluded.

The cause is a mismatch between data-state lookup and version-marker deletion:
an explicit NULL lookup of an OLH-only head with no manifest returns ENOENT
(`src/rgw/driver/rados/rgw_rados.cc:5938–5948`), which is valid for data reads but
does not prove the marker's bucket-index entry is absent. The receiver aborts on
that result (`rgw_cr_rados.cc:891–895`), while incremental sync excludes ENOENT
from its error gate and finishes the cursor (`rgw_data_sync.cc:4433–4450`).

The single S3 producer correctly carries the NULL/versioned flags; this is not
the deferred bulk-null caller gap. Zone1's S3 DELETE path tolerates the missing
data state and reaches index-driven instance unlinking, explaining why it
succeeds locally. PR #64272 applies the analogous exception to lifecycle, not
this multisite receiver.

The focused fix belongs in the receiver's versioned-marker deletion preflight:
distinguish a marker from an actually absent target and reach authoritative
index unlinking, preserving timestamp/race checks, NULL identity, versioned
epochs, and zones trace. Do not globally make marker reads succeed or blanket
suppress storage errors. No implementation, build, or additional test was
performed during this root-cause investigation.
