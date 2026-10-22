# ceph-dedup-tool search-dups

`search-dups` finds groups of objects with identical content in a pool,
using the dedup info BlueStore collects during deep scrub
(`src/os/bluestore/DedupInfoCollection.md`) and the per-OSD listing API
(`src/osd/DedupInfoListing.md`). It reads no object data and doesn't
modify anything (except pruning old records when asked). This document
describes its usage, the merge algorithm and the reasons behind it.

Code: `ceph_dedup_tool.cc` (`search_dups()`, `SearchForDuplicates`,
`get_pg_cutoffs()`), `dedup_merge.h` (`DedupKWayMerger`,
`DedupShardMerger`), tests in `src/test/ceph_dedup/test_dedup_merge.cc`.

## 1. Usage

```
ceph-dedup-tool --op search-dups --pool <POOL>
    [--dedup-shards <N>]          # power of 2, default 16
    [--dedup-batch-size <RECORDS>] # per OSD request, default 1000
    [--dedup-parallel-osds <N>]   # OSDs queried in parallel per thread, default 4
    [--newer-than <SECONDS> | --since-last-scrub]
    [--prune-older-than <SECONDS>]
    [--max-thread <N>] [--report-period <SECONDS>] [--debug]  # defaults 2, 10
```

Prerequisites: `deep_scrub_reformat` contains `collect_csum` for the pool
and PGs have been deep-scrubbed since. Caps: pool-wide OSD read (write
with `--prune-older-than`), mgr read for `--since-last-scrub`.

Output, one line per duplicate group on stdout:

```
<digest hex> <object count>: <hash>@osd.<N> <hash>@osd.<M> ...
```

`hash` is the object hash (8 hex digits) and `osd` the OSD holding the
record. Objects are identified by hash only, since names stay on the OSDs
(name resolution is planned). Different objects with colliding hashes in
one group are therefore counted once. A JSON summary goes to stderr:
`examined_records`, `stale_records` (with `--since-last-scrub`),
`duplicate_groups`, `duplicate_objects` (objects beyond the first in
each group, i.e. the ones that could be deduplicated).

## 2. How it works

1. The digest space is split into `--dedup-shards` shards; thread `n` of
   `--max-thread` processes shards `n, n+m, n+2m, ...`. Each shard is
   independent because all records of a digest fall into one shard.
2. For a shard: `dedup_candidate_list_slice()` creates a cursor over all
   up OSDs supporting the listing, and a `DedupKWayMerger` over the same
   OSDs.
3. Loop until the cursor ends: ask the merger for `next_targets(parallel_osds)`,
   fetch one batch of up to `batch_size` records from each of those OSDs
   in parallel (`dedup_candidate_list()` explicit-OSD variant), filter
   stale records, `add_batch()` each batch, `emit()` complete groups and
   print them.

### 2.1 K-way merge (`DedupKWayMerger`)

Every OSD (a *source*) delivers records sorted by digest, in batches.
The merger keeps:

- per source: pending records of its last batch(es), completion flag;
- `frontiers`: an ordered set of `<last digest delivered, source>` for
  incomplete sources (empty digest for sources that haven't delivered
  yet);
- a min-heap of sources with pending records, by head digest;
- the group being accumulated.

`emit()` pops records in digest order while the head digest is
**below the lowest frontier**: no incomplete source can deliver that
digest any more, so the group is final once the digest changes. A group
is reported only if it has more than one distinct object hash: the same
object may be reported several times (several scrubs, records left on a
former primary), so `(hash, osd)` pairs are sorted and deduplicated by
hash.

`add_batch(osd, items, completed, last_digest)`: `last_digest` is the
last digest the OSD actually delivered, passed separately because the
tool may filter all of a batch's records out (stale), and the frontier
must still advance or the merge would stall.

`next_targets(n)` returns the incomplete sources with the lowest
frontiers, i.e. those holding back emission. Fetching those first keeps
the buffered amount close to one batch per OSD.

Memory per thread is about `batch_size × OSD count` records, whatever
the shard size.

### 2.2 `--since-last-scrub`

Records are never updated in place: every deep scrub adds new ones, and
old records describe content that may have changed since. To use only
the latest deep scrub's records:

1. `get_pg_cutoffs()`: `osd pool get <pool> pg_num` (mon) and
   `pg ls-by-pool` (mgr) give each PG's `last_deep_scrub_start_stamp`.
   PGs without a stamp (being created/split, stats not yet reported) get
   no cutoff and are left out of the minimum.
2. Pool-wide pre-filter on the OSDs: `newer_than = now - oldest PG
   cutoff + skew margin`, so records older than any PG's last scrub
   aren't even sent.
3. Per record in the tool: PG = `ceph_stable_mod(hash, pg_num, mask)`;
   the record is stale if it was made before that PG's cutoff minus
   `CLOCK_SKEW_MARGIN` (2 s). Record timestamps come from the recording
   OSD, the PG stamp from the PG's primary, possibly another host.

Mutually exclusive with `--newer-than`.

### 2.3 `--prune-older-than`

Passed through to the OSDs, which delete records older than that while
listing. It's the only record cleanup besides `bluestore dedup clear`.

## 3. Design decisions and measurements

- **Streaming k-way merge instead of collecting a shard into a map**
  (`DedupShardMerger`, the first version, kept for tests): the map holds
  the whole shard. Measured with `ceph_test_dedup_merge`: about 4× the
  map's throughput, 113–130 B per held record vs 190 B, and the map
  needed about 11 GB per thread for 61.4M-record shards. Memory no
  longer depends on shard size, so fewer shards (fewer passes) are
  possible.
- **Lowest-frontier fetching** (`kway_lf`) vs round-robin (`kway`):
  holds exactly one batch per OSD at most, with 37–44 % lower peak RSS in
  the benchmark.
- **Shards per thread** rather than threads sharing a shard: no
  synchronization in the merge path; output is serialized under a global
  lock per group batch.
- **Filtering client-side by PG cutoff**: the OSD doesn't know PG stamps
  for PGs it no longer serves (records outlive PG moves).
- `--dedup-batch-size` trades request count against memory.

The numbers above come from runs on a single development host with the
benchmark defaults (1024 simulated OSDs, batch 1000); rerun to get
figures for your hardware.

## 4. Tests and benchmark

`ceph_test_dedup_merge` (not in `make check`):

- `KWayHandcrafted`, `KWayExplicitFrontier`, `KWayNextTargets`: merge
  semantics, filtered batches, target choice;
- `KWayMatchesMap`: randomized comparison against `DedupShardMerger`;
- `Benchmark`: throughput, CPU and RSS per merger; takes minutes by
  default. Tunables via `CEPH_DEDUP_BENCH_*` environment variables
  (seconds, threads, generators, batch, batches, shards, grid factor,
  mergers), see the file header.

End-to-end: a vstart cluster with a few OSDs, pools with
`deep_scrub_reformat collect_csum`, identical objects written, `ceph pg
deep-scrub` on all PGs, then `search-dups` (with and without
`--since-last-scrub`); compare groups with `rados stat`/`osd map`
hashes.

## 5. Limitations / open items

- Objects are reported by hash and recording OSD, not by name.
- Fetching and merging don't overlap: the thread waits for each round
  of requests.
- Down OSDs' records are silently skipped; results are best effort.
- EC pools have no dedup info yet.
- No action is taken on duplicates: linking is a separate, proposed
  design (`src/osd/DedupLinking.md`).
