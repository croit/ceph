# Explicit-version emptying of retained OLD

**Date:** October 9, 2026. **Result: FAIL—OLD is not empty on both zones.**
All 10 historical data-version removals replicated successfully. The first
explicit NULL delete-marker removal succeeded on Zone1 but did not remove the
marker on Zone2. The checked workflow stopped there; no manual peer cleanup or
bucket purge was used to hide the mismatch.

## Exact continuation

This continued the actual passing
[single-object DELETE fixture](SINGLE-DELETE-results.md), rather than creating a
new bucket or replaying full synchronization:

- Original run: `rgwlab-20261009-132521-57c7`.
- Retired OLD: `rgwlab-20261009-132521-57c7-migration-synced-single-delete-tmp`.
- OLD ID/marker on both zones: `ef1724bd-03f2-484a-841f-37232e7a8ebc.4243.31`.
- NEW: `rgwlab-20261009-132521-57c7-migration-synced-single-delete`.
- NEW ID on both zones: `ef1724bd-03f2-484a-841f-37232e7a8ebc.4243.32`.

The resume path checked the completed source report, every ordinary-DELETE
receipt, both live name/ID mappings, OLD's saved physical marker, complete
version histories, suspended status, NEW's known payloads, and caught-up streams
before permitting any mutation. It authorized only those two exact owned names.
The original report was not modified; results were written to a new run.

Command from local `build/`:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --empty-after-run rgwlab-20261009-132521-57c7 --objects 3 --timeout 180
```

Report: `build/rgw-repro/runs/rgwlab-20261009-154753-8f0b/report.json`.

## What was attempted

At the start, OLD contained **10 numbered data versions and 14 delete markers**
(11 null, 3 numbered), identically on both zones. Current object listings were
empty, but historical data and markers were deliberately retained by the earlier
ordinary DELETEs.

The frozen plan had 24 explicit-version requests, only on Zone1:

```python
s3.delete_object(Bucket=retired_name, Key=key, VersionId=version_id)
```

Data versions are removed before latest markers to avoid exposing old data by
marker removal. The test waits for the existing renamed streams after each
request; it does not reset full sync. Remaining historical payloads and all NEW
payloads are checked before proceeding.

### Historical data: successful

The first ten requests removed:

- Two numbered versions for each of `history0000`, `history0001`, `history0002`.
- One numbered data version for each of `marker0000`, `marker0001`, `marker0002`.
- The numbered `seed` data version.

Each request returned HTTP 204. After every request both version lists matched
the expected removal and replication caught up. At the end of this phase, both
zones had **0 data versions and 14 markers**. NEW remained correct.

### NULL marker: replicated removal failed

Request 11 targeted the current NULL delete marker for `data0000`:

```python
s3.delete_object(Bucket=retired_name, Key='data0000', VersionId='null')
```

It returned **HTTP 204**, with response VersionId `null`. Zone1 removed the
marker; Zone2 retained it throughout the 180-second checkpoint polling budget.

| Final observation | Zone1 | Zone2 |
|---|---:|---:|
| OLD data versions | **0** | **0** |
| OLD delete markers | **13** | **14** |
| Of those, NULL markers | 10 | 11 |
| OLD current object listing | Empty | Empty |
| OLD raw objects, late inspection | **13** | **14** |
| OLD bucket still present/suspended | Yes | Yes |
| NEW's eight payloads preserved | Yes | Yes |

All four synchronization streams eventually reported caught up, but OLD's
version lists were inconsistent. **Caught-up status was not accepted as proof
of matching contents.** A read-only follow-up at **16:29:28 UTC** found the same
13/14 marker counts and raw inventories, with NEW intact.

The test issued 11 of 24 planned requests and stopped at the mismatch. Therefore
**the other 13 marker deletions were not attempted**, and numbered-marker removal
behavior is not established by this run. The exercise completed with a recorded
regression failure and exit 1, not an infrastructure error.

## Evidence and source interpretation

Under the new run directory:

- `migration-synced-empty-pre-empty-sync.json`: successful pre-deletion gate.
- `migration-synced-empty-version-delete-00.json` through `-09.json`: historical
  data-version removal results and remaining-payload checks.
- `migration-synced-empty-version-delete-10.json`: request/response and the
  exact source/peer marker mismatch.
- `migration-synced-empty-version-delete-10-sync.json`: all streams caught up,
  but `old_zone2_versions_match=false`.
- `migration-synced-empty-late-consistency.json` and `-late-physical.json`:
  read-only follow-up; no manual repair.
- Redacted logs under `logs/`.

A verified receiver-side code defect is consistent with this result:
`RGWAsyncRemoveObj::_send_request()` returns a failed data-state lookup before
constructing the deletion operation (`src/rgw/driver/rados/rgw_cr_rados.cc:891–895`).
Explicit NULL marker heads can have OLH metadata without a data manifest,
causing that lookup to report ENOENT even while the marker index entry exists
(`src/rgw/driver/rados/rgw_rados.cc:5938–5948`). Incremental sync can finish its
progress marker after ENOENT (`src/rgw/driver/rados/rgw_data_sync.cc:4433–4450`).

**The exact incoming entry/receiver return was not retained in the log window**,
so this is a source-supported explanation, not a captured end-to-end trace.
Do not attribute this single-S3 request to the separate bulk DeleteObjects gap.
No backend fix was made. Blindly ignoring the state-lookup error would also risk
dereferencing an unpopulated state pointer in the subsequent mtime/ACL handling.

## Test implementation and limits

`migration-synced-empty` supports a fresh copy/rename/synchronize/plain-delete
workflow followed by this explicit-version phase. `--empty-after-run` permits
only an unchanged, completed, passing owned single-delete fixture; it rejects
partial histories, changed IDs, arbitrary paths, and incompatible selections.
This fixture is now partially consumed, so reusing its original successful run
with that option will be rejected rather than silently re-deleting it.

Changed Python passed `py_compile` and offline helper tests; `ninja -j10` passed.
Independent safety review found no material issue in the resume authorization or
deletion checks. Existing C++ backports were preserved.

**The requested full empty-and-consistent result was not achieved.** Historical
payload cleanup worked, but the NULL-marker replication blocker must be addressed
before completing this workflow. OLD remains available for investigation. No
bulk API, Zone2 manual deletion, bucket purge, or direct RADOS deletion was used.
