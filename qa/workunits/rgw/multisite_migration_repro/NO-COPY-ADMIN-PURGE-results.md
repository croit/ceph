# Suspended-bucket admin purge without copy or rename

## Latest result after producer fix

**The same admin-purge regression passes with the one-line NULL-intent fix.**
Fresh run **`rgwlab-20261010-223505-93c2`**, October 10, 2026,
22:35:05–22:41:12 UTC, completed with **exit 0**. Zone1's purge returned 0;
both bucket namespaces disappeared, and both zones had **zero raw objects**
in two consecutive complete caught-up observations. The earlier 0/10 failure
and its evidence are retained below. See the final section for the implemented
fix, build/source verification, and passing-run artifacts.

## Result before producer fix

**The populated-bucket admin purge is not physically consistent across zones.**
Zone1's `radosgw-admin bucket rm --purge-objects` succeeded. Bucket metadata
disappeared from both namespaces and the global synchronization gate became
caught up, but Zone2 retained **10 raw objects: seven verified payload bodies
and three zero-size NULL-marker heads**. Zone1 retained zero raw objects.

Run: **`rgwlab-20261010-204715-d83a`**, October 10, 2026,
20:47:15–20:53:33 UTC. Completed checked failure, **exit 1**, not incomplete.
The previously passing single-DELETE emptying result is a different operation;
it does not establish that this administrative purge is replicated completely.

## Environment restored and verified

Zone2's existing `/dev/nvme0n1p1` ext4 filesystem was clean but unmounted, and
`ceph-dev` was stopped. The partition identity and original checkout/build/Ceph
data were verified with a read-only `ro,noload` mount before mounting it normally
read-write at `/mnt/ceph-dev` and starting the existing container. No formatting,
repair, replacement workspace, or data reset was performed.

On the user's explicit restart request, the owned peer RGW/MGR/OSD/MON were
stopped gracefully, then `docker restart ceph-dev` was executed. Docker reported
the container running with start time **20:45:09.731344 UTC**. The original MON,
OSD, MGR, and RGW were restored from their existing data/configuration. Configured
and live cluster identities, saved topology, users, and daemon ownership checked
against private lab state; only saved build-version labels were refreshed.

All **20 changed production RGW/CLS code files** matched the local committed
source and Zone2's working code. Tests were excluded from that comparison.
The equal-tree commit reorder was aligned on Zone2 without changing source
bytes or other working state. Both checkouts and `ceph`/`radosgw` builds report
**`215b01ac51774c417e59fbc28d88fce44954018a`**. `ninja -j10` succeeded in both
build directories; Python compilation and whitespace checks passed.

## Existing harness, one scenario

The test is in **`exercise.py`**, reusing its fixture, synchronization checkpoints,
and `direct_purge()` path. `--no-copy-rename` now supports the synced-purge case
without a replacement bucket. No standalone purge test script was created.
During the pre-fix run, production source was not modified; the harness/README
extension was uncommitted. No other regression or unit-test suite was run.

```bash
RGW_REPRO_SSH_CONTROL_PATH=/tmp/opencode/zone2-ssh.sock .venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-purge --no-copy-rename --objects 3 --timeout 180
```

Bucket: `rgwlab-20261010-204715-d83a-migration-synced-purge`.
The sole purge command was issued on Zone1:

```bash
radosgw-admin bucket rm --bucket rgwlab-20261010-204715-d83a-migration-synced-purge --purge-objects
```

## Before purge

Each setup stage passed the existing caught-up/parity checkpoints in both
directions. Immediately before purge, full version lists, saved bucket
identities, suspended state, and current/numbered payloads matched on both zones.

| Populated suspended state | Zone1 | Zone2 |
|---|---:|---:|
| Numbered data versions | 10 | 10 |
| NULL data versions | 7 | 7 |
| Numbered delete markers | 3 | 3 |
| NULL delete markers | 3 | 3 |
| Readable current keys | 8 | 8 |
| Raw objects | 24 | 24 |

The purge did **not** first enumerate/delete the versions individually. Marker
creation was part of the original fixture population. There was no copy,
rename, NEW bucket, bulk deletion request, Zone2 cleanup, forced/full-sync reset,
bucket purge on Zone2, or raw RADOS deletion.

## After purge and catch-up

The admin command returned **0**. Both S3 namespaces reported `NoSuchBucket`.
Per-bucket status is consequently recorded as **NA**, not "caught up". The test
instead checks saved-topology-matched global peer data streams on both zones and
Zone2 metadata catch-up, with complete read-only inventories using the saved
OLD marker. The final report contains **14 consecutive complete global-caught-up
observations** while Zone2's retained objects remained.

| Post-purge state | Zone1 | Zone2 |
|---|---|---|
| Bucket namespace | Absent | Absent |
| Peer data synchronization | Caught up | Caught up |
| Metadata synchronization | Authoritative master | Caught up with master |
| Raw objects under saved marker | **0** | **10** |
| Nonempty bodies | 0 | **7** |
| Zero-size heads | 0 | **3** |

Zone2 retains the NULL-data bodies for `data0000..2`, `history0000..2`, and
`final`, plus zero-size heads for `nullmarker0000..2`. A read-only follow-up
`rados get` to stdout verified **all seven bodies byte-for-byte** against their
fixture payloads after bucket metadata was removed. The bodies have sizes
4480 bytes (data), 4494 bytes (history overwrites), and 4485 bytes (final).
These are real retained payloads, not just stale counters. Zero size or pending
OLH attributes alone is not used to classify the remaining heads as orphans.

## Evidence

Under `build/rgw-repro/runs/rgwlab-20261010-204715-d83a/`:

- `report.json`: completed failure, pre-purge gate, global status observations,
  namespace absence, retained object counts, and failed physical-cleanup check.
- `migration-synced-purge-purge.json`: exact Zone1 purge, return code 0.
- `migration-synced-purge-old-before-purge-physical.json`: 24/24 raw objects.
- `migration-synced-purge-old-after-purge-physical.json`: final 0/10 objects,
  retained names/sizes, sampled manifests and OLH xattrs.
- `migration-synced-purge-purge-observations.json`: global catch-up and per-bucket
  NA evidence independent of raw cleanup.
- `post-purge-retained-payloads.json`: seven successful read-only raw GET/hash
  comparisons after namespace deletion.
- `logs/`: collected private/redacted gateway and cluster diagnostics.

This is a measured administrative-purge cleanup gap on the current fully updated
two-zone development lab. No additional backend fix, retrospective cleanup, or
production-scale claim is made. The raw leftovers and older fixtures are retained.

## Code-level diagnosis of pre-fix run: producer drops explicit NULL intent

This diagnosis refers to committed `215b01ac51774c417e59fbc28d88fce44954018a`
before applying the one-line producer fix described in the final section.

Normal admin purge lists every version and calls `rgw_remove_object()`
(`src/rgw/driver/rados/rgw_sal_rados.cc:422–447`). That helper converts an empty
listed instance to literal `"null"`, then invokes `Object::delete_object()` with
the log flag (`src/rgw/driver/rados/rgw_bucket.cc:153–164`).

The RADOS convenience adapter constructs a backend Delete operation but sets
only owner/versioning status (`rgw_sal_rados.cc:2304–2312`). It omits
`params.null_verid`, which defaults false (`rgw_rados.h:849–852`). The backend
correctly unlinks the explicit NULL locally, but its normalized log key has an
empty instance and the NULL bilog bit is not supplied. For that versioned unlink,
source inspection predicts flags `0x1` instead of `0x3`; the source bilog had no
remaining rows when subsequently queried, so the numeric flags are not claimed
as independently captured from the run.

Zone2 restores literal `"null"` only when that bit is present
(`rgw_data_sync.cc:4400–4407`). Actual full gateway-log events confirm that the
purge's `data0000` and `nullmarker0000` removal requests arrived **without**
`[null]`. Thus the explicit-NULL receiver fix is not selected for these requests.

- `data0000` was dispatched through the empty-instance/unqualified deletion
  semantics, which select marker creation instead of instance unlinking
  (`rgw_rados.cc:5448–5482`). Its receiver log shows the 4480-byte manifest,
  `LINK(data)` and `STALE` acknowledgment, then success; the raw payload remained.
  Those operations match the marker-creation equality/precondition acknowledgment
  branch, which can preserve the current data and return success
  (`cls_rgw.cc:1984–2027`); the actual comparison values were not logged in the
  selected event excerpt.
- `nullmarker0000` followed the head to an existing marker, returned ENOENT, and
  aborted without unlinking it. The guarded explicit-NULL exception requires a
  literal NULL identity, which this request lacked.
- Successful no-op progress and tolerated ENOENT can finish synchronization
  cursors (`rgw_data_sync.cc:4433–4450`), while bucket metadata removal propagates
  independently. This explains namespace absence/caught-up progress without raw
  cleanup. Numbered SIDs retain their explicit instance and are not subject to
  this lost-NULL identity problem.

Selected exact backend lines, with original absolute byte offsets and no
authentication material, are saved in
`logs/zone2-admin-purge-null-intent-trace.txt` under this run.

The minimal producer-side correction is to populate the convenience adapter's
`del_op.params.null_verid = (get_instance() == "null")` before the backend
normalizes its copy. Do not infer explicit NULL intent from an unqualified empty
instance. This is a different caller gap from the previously deferred bulk
DeleteObjects path. At the end of the read-only investigation, the correction
was **not yet implemented or tested**. No code, build, or additional test changes
were made during that investigation. The subsequently authorized implementation
and focused validation are recorded below.

## Implemented fix and focused retest

The only production-code change is in
`src/rgw/driver/rados/rgw_sal_rados.cc:2308`, before backend normalization:

```cpp
del_op.params.null_verid = (get_instance() == "null");
```

This preserves the existing NULL bilog bit and explicit-instance deletion
semantics. It does not classify an unqualified empty instance as explicit NULL,
change numbered version IDs, bypass existing flags/preconditions, or change any
SAL virtual API or persistent format. The existing receiver/CLS marker and
replacement-race safeguards remain unchanged.

At verification, both nodes were at HEAD
`215b01ac51774c417e59fbc28d88fce44954018a` **plus the uncommitted one-line fix**.
The fixed `rgw_sal_rados.cc` Git blob is
`cc4045590f8b18f17bd994764bfd172c4114a24b` on both nodes. All 20 changed production
RGW/CLS files matched between nodes; tests were excluded. Both `ninja -j10`
builds succeeded, including recompilation of the changed adapter and relinking
of `radosgw` and `radosgw-admin`. Only the owned gateways were restarted.
Original cluster identities, committed topology, users, and saved build versions
validated before the retest. Changed-line C++ formatting, Python compilation of
`exercise.py`, and whitespace checks passed.

The exact same command was run through the existing `exercise.py` on a fresh
fixture, without any additional regression/unit-test suite:

```bash
RGW_REPRO_SSH_CONTROL_PATH=/tmp/opencode/zone2-ssh.sock .venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-purge --no-copy-rename --objects 3 --timeout 180
```

Run: **`rgwlab-20261010-223505-93c2`**, 22:35:05–22:41:12 UTC.
Bucket: `rgwlab-20261010-223505-93c2-migration-synced-purge`.
Saved bucket marker on both zones:
`ef1724bd-03f2-484a-841f-37232e7a8ebc.48417.1`.

All fixture stages and pre-purge checks passed: the same 10 numbered data
versions, seven NULL data versions, three numbered markers, three NULL markers,
and eight readable current keys were synchronized before the purge. The sole
purge was on Zone1. There was no copy/rename, preceding individual-version
cleanup, Zone2 purge/cleanup, manual full sync, reset, or direct RADOS deletion.

| Final observation | Zone1 | Zone2 |
|---|---|---|
| Bucket namespace | `NoSuchBucket` | `NoSuchBucket` |
| Global peer data stream | Caught up | Caught up |
| Metadata | Authoritative master | Caught up with master |
| Raw objects under saved marker | **0** | **0** |
| Nonempty bodies / zero-size heads | **0 / 0** | **0 / 0** |

The final two observations had complete inventories, matching global identities,
all catch-up checks true, and zero raw objects. Per-bucket synchronization was
correctly recorded as NA after namespace removal. The report is completed,
`passed: true`, with no failed checks; the process returned **0**.

Passing evidence under `build/rgw-repro/runs/rgwlab-20261010-223505-93c2/`:

- `report.json`: completed pass and two stable final catch-up/cleanup observations.
- `migration-synced-purge-purge.json`: Zone1 admin purge return code 0.
- `migration-synced-purge-old-before-purge-physical.json`: populated storage before purge.
- `migration-synced-purge-old-after-purge-physical.json`: final 0/0 raw inventories.
- `migration-synced-purge-purge-observations.json`: full status, namespace, and
  physical-cleanup observations.

The earlier failed fixture was not manually repaired, purged on Zone2, or replayed.
The verification run preceded committing the fix and did not rewrite history
or push any commits. This validates the fix for this focused reproducer;
it does not establish bulk-DeleteObjects behavior,
mixed-version rollout, production-scale behavior, or retrospective cleanup of
already-drained incorrect deletion events.
