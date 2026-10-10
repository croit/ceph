# NULL-marker receiver fix: application and validation

**Follow-up:** the subsequent marker-metadata timestamp refinement and atomic
stale-NULL protection passed full two-zone emptying. See
[NULL-MARKER-TIMESTAMP-FIX-results.md](NULL-MARKER-TIMESTAMP-FIX-results.md).
The failed initial-patch run below is retained as historical evidence.

## Outcome

**Applied to both clusters and built successfully, but the complete emptying
regression still fails.** The original `data0000/null` failure is resolved in
this run. A separate NULL-marker cohort exposes an OLH-head timestamp comparison
that skips removal despite a matching logical marker timestamp.

Only the existing `migration-synced-empty --no-copy-rename` regression was run.
No additional scenarios or unit-test suites were executed.

## Applied source change

Only `src/rgw/driver/rados/rgw_cr_rados.cc` differs from the previous combined
six-PR source patch. `RGWAsyncRemoveObj::_send_request()` now:

- Initializes the state pointer and captures literal NULL identity.
- Allows ENOENT only for a versioned explicit NULL instance with a loaded
  OLH-only state marked nonexistent; other state errors still return.
- Retains the loaded state for existing timestamp and ACL checks.
- Passes the captured NULL identity to the index-backed delete operation.

The existing timestamp check, ACL decode, versioned epoch, owner fields, and
zones trace were not otherwise changed. Review found no blocker in this initial
diff. `clang-format` ran only on diff-derived line ranges. No changes were
committed, and no on-disk or wire format was changed.

## Builds and synchronization

- Both checkouts remain detached at `10cef5efe60f078b70208ec17f03305a8dcc4100`.
- Preserved PRs #65158, #65948, #58133, #62469, #67661, and #64272.
- Remote pre-fix source matched the sealed six-PR patch byte-for-byte before
  applying the new patch. All 24 combined source/test file hashes match between
  nodes afterward; unrelated remote worktree/index state was preserved.
- Combined patch SHA256:
  `babe9e4150b72df094cafbf2cf77ca0a2a30f826b1cd885fb09636b36ce67e7e`.
- Saved combined patch blob: `ada08a31109005e4fff74246260e52c055b55058`.
- Fixed receiver file blob: `ca5aced9f1535c7d2a3206ecd961f8297d66a39d`.
- `ninja -j10` passed on Zone1 (34 scheduled steps) and Zone2 (277 steps after
  CMake regeneration and guarded combined-patch application).
- Python compilation and `git diff --check` passed.
- Both owned gateways were restarted using the rebuilt binaries. Live cluster
  identities and saved multisite topology validated without resetting data.

## Regression results

```bash
RGW_REPRO_SSH_CONTROL_PATH=/tmp/opencode/zone2-ssh.sock .venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --no-copy-rename --objects 3 --timeout 180
```

Run: **`rgwlab-20261009-222744-738e`**. This is a completed checked regression
failure (exit 1), not an incomplete/infrastructure result.

| Phase | Result |
|---|---|
| Enabled bucket, numbered versions, suspension, and NULL writes | Passed all setup checkpoints in both zones |
| Eight ordinary current-key DELETEs | All passed |
| Ten numbered-data-version DELETEs | All passed; no data versions remain |
| Seven NULL markers created by the ordinary current-key deletion phase | All passed, including previously failing `data0000/null` |
| Three numbered delete markers | All passed |
| Explicit `nullmarker0000/null` deletion from the separate setup cohort | Zone1 removed it; Zone2 retained it despite caught-up status |

At failure, both zones have zero data versions and empty current-object
listings. Zone1 retains **3 NULL markers**, Zone2 **4 NULL markers**. Explicit
deletion stopped at request 21 of 24: ten data and ten marker removals passed,
then one marker removal failed replication. The remaining three requests were
not sent. There is no final logical/physical emptiness pass.

No copy, rename, manual full synchronization, bucket purge, Zone2 manual cleanup,
or raw deletion was performed. Both the new failed fixture and earlier fixtures
remain available.

## Remaining failure: physical OLH time versus logical marker time

The full Zone2 log at **22:51:28.561 UTC** confirms that the patched receiver
gets past the missing-data lookup, but then skips removal because:

```text
obj mtime       = 2026-10-09T22:31:48.011229+0000
request timestamp = 2026-10-09T22:31:20.014773+0000
```

A read-only index inventory confirms the NULL-marker instance's logical
`meta.mtime` is **22:31:20.014773 UTC**, equal to the request timestamp. Its
replica's raw OLH head was created later, at **22:31:48.011229 UTC**. Treating
that physical head time as a newer logical object incorrectly skips this event.

Evidence under `build/rgw-repro/runs/rgwlab-20261009-222744-738e/`:

- `report.json`: setup checkpoints and complete issued-request history.
- `migration-synced-empty-version-delete-10.json`: formerly failing
  `data0000/null` now passes in both zones.
- `migration-synced-empty-version-delete-20.json` and `-20-sync.json`: remaining
  marker failure, 3/4 counts, and caught-up status.
- `logs/zone2-null-marker-timestamp-skip.txt`: exact receiver log plus selected
  authoritative marker-index metadata, without authentication material.

The fix remains applied on both nodes, but **must not be described as fully
validated**. The follow-up needs a correct NULL-marker metadata lookup for the
timestamp preflight, without weakening protection for genuinely newer writes.
The existing `bi_get_instance()` literal-NULL lookup is not a drop-in marker
lookup: the NULL-marker record has a distinct instance-index key. A blind
removal of the timestamp guard is not included in this patch.
