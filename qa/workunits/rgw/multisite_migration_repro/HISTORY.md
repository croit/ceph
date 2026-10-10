# Suspended-version multisite delivery history

## Base and preserved safety snapshot

- Delivery branch: `wip/rgw-suspended-multisite-reef`.
- Original Reef/customer base: `10cef5efe60f078b70208ec17f03305a8dcc4100`
  (`croit_18.2.8_core1`).
- Signed safety snapshot: `backup/rgw-suspended-verified-20261010` at
  `bf4826e016f72265b49595a5a9a506b38773ba09`, tree
  `83f36dd70d8ca0249c651d97834efb8a428e4fde`.

The delivery reconstructs the reviewed backports as 14 separate signed
cherry-picks, followed by a separate customer NULL-marker fix and a separate
customer QA harness/documentation commit. Upstream `fixup!` and `squash!`
subjects retain their individual commits. Original upstream sign-off trailers
are preserved. Each of the 14 local cherry-picks and the custom fix carries
the exact requested delivery sign-off:

```text
Signed-off-by: Md Mahamudur Rahaman Sajib <mahamudur.sajib@croit.io>
```

The separate customer QA harness/documentation commit carries the same exact
sign-off. These are DCO sign-off trailers, not a claim of GPG-signed commits.

## 1. Fourteen upstream cherry-picks

These commits cover PRs #65158, #65948, #58133, #62469, #67661, and #64272,
including the approved timestamp-epoch prerequisite and reviewed Reef
adaptations. The table is in local delivery order.

| Order | Upstream commit | Local signed commit |
|---|---|---|
| 1 | `3fed58f43c3cb3977130926a2d1bca551deefade` | `22d693945f9d6db955bfc5e0a17a0cbb0fae2068` |
| 2 | `7e3d493dc3240fc7c8b2976e0de09cf2ecaebd99` | `da370e04de727e5e10d06809f5e2ccfbb2c153ae` |
| 3 | `485020f97b08222f696d7aaf95f9ec3b49c5d171` | `2e9304ebed5537a560e3b5f8cc13c333a1f5caf2` |
| 4 | `92002be04ebea3d1ec0d0ddfda4c8624131ad917` | `caccd4b4d629c40f2a3350d4cc1ffe4ab4132639` |
| 5 | `4195486c8edc68bfdf5cd4d87e04ef67f35a9364` | `b72d9c203576c1a6432ea43956442f083dbb9400` |
| 6 | `75c7b8ece796b813ce734a14b7354593d03d3cfc` | `09634eb3e8c2ddf9f55596641637bef0df987199` |
| 7 | `b4b8c63ace17e846fe289490ba2f69b6d4fc3f30` | `0ec5105d7ecf674208f9efeb3c11c5650aaa320d` |
| 8 | `c6825a58cc7075859d2193ddf0f2a75aec0f81b2` | `e5837607779c72d0270f6207b40928ba715df5dd` |
| 9 | `b438348a65e84af3652d6a8e59196f9b7f3dd257` | `e61652199157fa58f4bc950778b448b72e1f1993` |
| 10 | `2d5d75f75964941d26c8563cce8a828435be6c72` | `267beb015666f8f4fbcf058cac14a9cf3a87716b` |
| 11 | `3ae00074d901a6b5010a2b659f511fab71554d3d` | `284031abf8361402e6d1b24da4271ab6b3d05033` |
| 12 | `1c7171bb8615598f6e4ccade35940ebffb398a7e` | `80d27c946c2fbc452fd98aae047a84983029f052` |
| 13 | `224821147f2664e54f81b0bb93ccd23669f31f04` | `bb432aca812f8cd301223af51bc8e328205cb78b` |
| 14 | `8654b1f202ad5719901dbcd8c64a384a75a08adc` | `5d6119b3bb7ca588ffc2bf8d9d92f35829954c83` |

The final cherry-pick has the exact reviewed six-PR tree
`f880b3a8e9e74d139770f55ce5ae34ee02b7fb19`.

## 2. Separate customer NULL-marker fix

Commit `15769bd102cee207eab92bf264da3bc80436c4d1` (`15769bd102c`):
`rgw/multisite: safely replicate explicit NULL marker removals`.

This separately committed and signed-off fix preserves explicit NULL intent, permits
the expected OLH-only delete-marker state, compares the marker's logical index
timestamp, and atomically protects newer mutable NULL data/marker targets from
stale unlinks. It includes the focused source regression coverage described in
the historical timestamp-fix report.

After this commit, all **24 verified source/test files** are byte-identical to
the signed safety snapshot. The entire `src` subtree has the same Git tree
object on the custom-fix commit and the snapshot:
`501fe3acf9f7b66bf8865944ef85d24f759220ad`. The history reconstruction therefore
preserves the verified core source bytes. The separate QA delivery preserves
that core-source equality.

## 3. Separate customer QA harness and documentation

The 16 existing customer QA files were restored from the safety snapshot for
a separate delivery commit after the custom fix. The only updates to restored
content are the committed-build guard in `lab.py` and its documentation in
`README.md`; this `HISTORY.md` is new. The exercise, requirements, and historical
result reports retain their restored bytes.

The build guard requires all four local/remote `ceph` and `radosgw` binaries to
report the same commit. Each binary must match its own configured source
checkout's current Git `HEAD`, and each checkout must descend from the full
original base. Failed Git/ancestry checks fail closed. Returned version records
retain their existing structure.

Saved-version equality and all owned FSID, topology, ordinary-user, and PID
protections remain enforced by `Lab.validate()`. After the final delivery commit
is built on both nodes, the lab owner explicitly refreshes only the private
configuration's `versions` entry after verifying the existing owned live and
configured FSIDs, exact saved topology and endpoints, user/daemon identities,
and matching current-HEAD binaries on both nodes. See the
[README reuse procedure](README.md#reuse-after-a-committed-build).

This QA delivery contains source harness files and documentation. Credentials,
private state, keyrings, build artifacts, and PDFs are excluded from the delivery.

## Historical evidence and post-commit rerun

Existing result reports describe their original runs. Their statements that
patches were uncommitted at the original base are historical and remain intact.
The successful workflow and focused safety checks in
[NULL-MARKER-TIMESTAMP-FIX-results.md](NULL-MARKER-TIMESTAMP-FIX-results.md)
predate this reconstructed committed delivery; they are not a post-commit pass.

**Post-commit customer rerun: PENDING.** After the separate QA commit, rebuild
both nodes at the final delivery `HEAD`, verify owned identity/topology, and
explicitly refresh only saved versions as above. Then run this exact
command from the local build directory against the existing owned clusters:

```bash
.venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/exercise.py --case migration-synced-empty --no-copy-rename --objects 3 --timeout 180
```

If an already-established SSH control connection is needed, optionally set
`RGW_REPRO_SSH_CONTROL_PATH` to its absolute socket path for that invocation.
Record the measured post-commit outcome only after the rerun completes.
