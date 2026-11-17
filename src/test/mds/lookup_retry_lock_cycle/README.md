# MDS lookup-retry lock cycle reproduction

Deterministic reproduction of an MDS lock-order cycle between a lookup retry
and a directory lock cache. A lookup that is waiting on a
snapshot lock retries its path traversal. If the parent directory's file lock
is now marked *cached* by a directory lock cache, the retry asks for a new
parent `ifile` writer lock **while still holding its dentry read lock**,
which reverses the normal parent-before-child order.

The only artificial delay is a held snapshot-lock owner. It is released before
the result is judged.

## What runs

`cluster.py` starts an isolated single-OSD vstart cluster in an arm directory
from build-tree binaries. It never installs anything. `stop` signals only
processes whose environment carries that arm's `CEPH_CONF`, and unmounts only
mounts below the arm directory.

`repro.py <arm> <mode>` drives these actors. Each one is a separate CephFS
client session:

| Actor | Client | Operation | Effect |
| --- | --- | --- | --- |
| H | libcephfs | unlink `/e/h` (hard link to F) | takes F.snap X; parked by `unlink_hold_cap_deferral arm_unlink_hold h` |
| L | libcephfs | lookup `/d/f` | takes dentry N RD, waits on F.snap RD |
| K | kernel, `nowsync` | first unlink in `/d` (sync) | MDS creates an UNLINK lock cache on D, issues DIR_UNLINK |
| U | same kernel client | unlink `/d/f` (async) | attaches to the lock cache (borrows D.file/D.nest WR), waits on N X |
| G | libcephfs | getattr/lookup of `/d` | starts the D.file transition to sync and invalidates lock caches |

Then H is released. F.snap X drops and L retries. Because U keeps the lock
cache alive, D.file is still *cached*, so L requests D.file WR and waits while
holding N RD:

```text
L holds N RD  -> waits D.file WR (transition in progress)
transition    -> waits for the lock cache's WR, kept alive by attached U
U             -> waits N X
N X           -> waits for L's N RD
```

## Modes

| Mode | Change | Expected |
| --- | --- | --- |
| `borrow` | the schedule above | L, U, G stay blocked after every delay point is released |
| `prevent` | `mds_allow_async_dirops=false` (admin socket, verified) before the kernel unlinks | no lock cache; everything completes |
| `toggle` | as `borrow`, then `mds_allow_async_dirops=false` after the cycle formed | stays blocked (no wakeup) |
| `direct` | U is a different (libcephfs) client and the kernel's cap acks are deferred | completes: see the notes below |
| `restart` | kernel client first builds lock caches; then `ceph config set <REPRO_CONFIG_WHO, default mds> mds_allow_async_dirops false`, `ceph mds fail`, then the `borrow` schedule | without the startup fix: deadlock; with it: completes |

## Usage (root, kernel `ceph` module available)

```sh
export CEPH_ROOT=<this tree>   CEPH_BUILD=<build of this branch>
# optional: CEPH_MDS_BINARY=<ceph-mds built from this branch>
D=/some/workspace/dir/arm-borrow
python3 cluster.py prepare $D
TMPDIR=$D/tmp python3 cluster.py start $D
python3 repro.py $D borrow        # evidence in $D/evidence-borrow/
python3 cluster.py stop $D
```

Use a fresh arm for every run: the deadlock is permanent until the MDS restarts.
`evidence-<mode>/` contains `driver.log`, `result.json`, and admin-socket
snapshots (ops in flight, `dump inode` of D and F, delay-point events) at each
step. The MDS log is `<arm>/out/mds.a.log` (`debug mds = 20`).

## Results (2026-11-17, 18.2.7-61-gb74587edcaf plus this branch's delay points)

| Mode | Lock cache created / borrowed | L retry requests D.file WR | Outcome 60 s after release |
| --- | --- | --- | --- |
| borrow | 1 / 1 | yes | **L, U, G blocked** (wrlock / xlock / rdlock waits) |
| prevent | 0 / 0 | no | all completed |
| toggle | 1 / 1 | yes | blocked; still blocked 60 s after the runtime toggle |

Final D state in `borrow`: `ifile excl->sync w=1`, `inest lock w=1`,
`isnap sync` with readers.

Restart runs (option persisted as false, MDS failed and respawned; the MDS log
shows `set_mon_vals mds_allow_async_dirops = false` after the respawn):

| MDS | Config section | Lock caches created / borrowed after respawn | Outcome |
| --- | --- | --- | --- |
| without the Server startup fix | `mds` | 1 / 1 | **deadlock** |
| without the Server startup fix | `global` | 1 / 1 | **deadlock** |
| with "mds: initialize mds_allow_async_dirops from config at startup" | `mds` | 0 / 0 | all completed |

`Server::mds_allow_async_dirops` defaults to `true` and was only updated by
`handle_conf_change()`. A freshly started or failed-over MDS therefore kept
creating lock caches even though `config get` reported `false`. Setting the
same value again does not help (`(no change)`, so no notification).

## Notes and limits

- The kernel client is the only active client, so it is the loner and the
  final state is `excl->sync`. A variant without a loner (`lock->sync`, many
  clients holding caps on the directory) has not been reproduced.
- DIR_CREATE/DIR_UNLINK caps are issued only while D.file is EXCL (loner).
  A writer from a *different* client forces EXCL->LOCK, which revokes them
  first, so in the `direct` variant the lock cache has no issued caps when it
  is invalidated and is destroyed at once. That variant did not form a cycle.
- This demonstrates that the lock-order cycle can form in this code.
