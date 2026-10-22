# BlueStore: dedup info collection during deep scrub

This document describes how BlueStore collects deduplication info during
deep scrub: a content digest of each object, recorded in the OSD's KV
store, so duplicates can be found cluster-wide later without reading any
data. It covers the record format, how digests are built, the onode
`data_version` that keeps records verifiable, maintenance and the traps
found while building it.

Collection is implemented as the `collect_csum` engine of the deep scrub
reformat framework, described in `DeepScrubReformat.md` (engines, lock
mode, deferred work, scrub read path). The later stages of the dedup
pipeline are only summarized here (section 7).

Code references use function/class names rather than line numbers.

## 1. Configuration

```
ceph osd pool set <pool> deep_scrub_reformat collect_csum
```

`collect_csum` can be combined with `recompress` / `defragment`; it
doesn't modify objects, so on its own it keeps scrub reads under the
shared collection lock.

| Option | Meaning |
|---|---|
| `osd_deep_scrub_stride` | scrub read size; digests are built across reads |
| `bluestore_debug_collect_csum_delay` | dev, seconds; sleep injected before recording a digest (tests the unlocked path). `with_legacy` |
| `osd_dedup_list_max_duration` | max time of a single dedup listing request on the OSD |
| `bluestore_rocksdb_cfs` | default includes `D(3,0-24)`: dedup records' column family, 3 shards hashed over pool + first digest bytes. Applied at mkfs only |

## 2. What gets recorded

One record per object content digest, in KV prefix `D`:

```
key   = pool(u64 BE, +2^63) ':' digest(16 B MD5) ':' ts(u64 BE ns, wall clock) ':' hobj bitwise hash(u32 BE)
value = dedup_record_value_t { ghobject_t oid; uint64_t nid; uint64_t data_version; }  (ENCODE v1)
```

Design choices:

- **Digest right after the pool**, so a pool's records are sorted by
  digest: duplicates are adjacent, and a digest-prefix range is a
  "shard" (`digest_msb`/`shard_bits`) that can be listed independently
  and merged across OSDs in order (k-way merge in the tool).
- **Timestamp in the key**: lets listing skip records older than a cutoff
  (`newer_than`) and prune stale ones while iterating
  (`prune_older_than`). Wall clock (`real_clock`), not mono clock,
  because records survive OSD restarts and are compared with PG scrub
  stamps from other daemons.
- **Object hash in the key**: the tool can map a record to PG and primary
  OSD without the object name. Names stay on the OSD (in the value).
- **nid + data_version** identify the exact state of the object the
  digest was taken for (section 5), so a record can later be verified
  as fresh without rereading data.
- **Non-synced KV submit** (`record_candidate()`): losing a few records
  on crash is harmless (next scrub re-creates them), saves WAL syncs.
- Records are never updated in place: a re-scrub adds a new record with a
  new ts. Old ones must be pruned (open item: growth/pruning policy).

## 3. Which reads record

`OnodeChecksumCollectEngine::validate()` requires:

- `SCRUB | PRIMARY` flags: only the primary records, otherwise every
  replica would record the same object;
- `oid.shard_id == NO_SHARD`: EC shards hold partial content (EC pools
  aren't supported yet; per-shard digests is the open idea);
- `length > 0` and `onode.size > 0`: empty objects aren't recorded;
- `dedup_digest_builder->accepts(oid, nid, data_version, offset)`.

It does not use the base alignment check.

## 4. Multi-chunk digests: `DedupDigestBuilder`

Objects larger than `osd_deep_scrub_stride` are read in several reads,
so the digest is built incrementally. One builder per collection
(`Collection::dedup_digest_builder`, shared_ptr) holds
`{oid, size, nid, data_version, next offset, MD5 state}`:

- a read at offset 0 starts a new digest (abandoning any in progress);
- a read continues it only if oid, size, nid, data_version match and
  `offset == next`; anything else abandons it;
- the digest is returned once `next >= size`; an empty read before that
  abandons it.

So a digest may be lost (e.g. object modified mid-scrub) but is never
built from mixed data. One builder per collection suffices because scrub
reads a PG's objects one by one (`DeepScrubReformat.md`, section 3);
if that ever changes, digests would be lost, not corrupted.

`execute()` doesn't hash under the collection lock: it captures
everything (oid, size, nid, data_version, offset, builder, a **shared**
copy of the read bufferlist, no data copy) into a closure via
`ctx.defer()`, returning false so other engines still run. `read()`
runs the closure after releasing the lock. MD5 of a 512 KiB chunk plus a
KV submit would otherwise block writers to the whole PG; with
modifying engines present the lock is exclusive and that would block
readers too. `bluestore_debug_collect_csum_delay` and
`CollectCsumUnlockedTest` check that a write to another object proceeds
while recording sleeps.

## 5. Onode `data_version`

`bluestore_onode_t::data_version` (varint, onode encoding v4): a plain
counter bumped by `_bump_data_version()` on each data modification:
`_touch`, `_write`, `_zero`, `_truncate`, `_clone` (destination),
`_clone_range`. Each of them calls `write_onode` once; `_do_zero` /
`_do_truncate` no longer write the onode themselves. The explicit
`bump_ver` parameter is passed `true` from `_txc_add_transaction()`
(currently the only caller); it exists so future internal callers have to
decide whether they change the data. Not bumped by xattr/omap ops or by
reformat writes.

A recreated object restarts at 0 but gets a new nid, hence the pair
`<nid, data_version>` identifies a data state. Both are local to the
OSD's copy: they differ between replicas and after backfill, so a record
can only be checked on the OSD that recorded it, while it still holds the
object. `BlueStore::get_object_data_version()` reads them.

**FORCE_V2 trap**: when the store runs without onode segmentation
(`bluestore_onode_segment_size == 0`), `_record_onode()` encodes onodes
with `FLAG_DEBUG_FORCE_V2`, i.e. as v2, despite the "debug" name. That
silently dropped `data_version`. Now v2 is used only
while `data_version == 0`; v4 is still readable by older releases (they
skip unknown fields; on downgrade the value is lost when the onode is
rewritten).

## 6. Maintenance and inspection

Admin socket (`BlueAdmin.cc`, also via `ceph tell osd.N`):

- `bluestore dedup stats <pool-id>`: estimated bytes and record count;
- `bluestore dedup clear <pool-id>`: `rm_range_keys` (enforced range
  delete, see the `kv: enforced rm_range_keys` commit) + async compact.
  Needed after record format changes;
- `bluestore dedup list <pool-id> [shard] [shard_bits] [after]
  [max] [newer_than] [prune_older_than] [max_duration]`: debugging view of
  what the OSD listing request returns (digest, hash, ts, age, cursor).

Listing (`DedupStatsCollector::list_candidates`) iterates the digest
shard range from cursor `pnext`, returns records with
`ts >= retrieve_after`, deletes those with `ts < remove_before`, stops at
`max_return` or `max_duration` (`-EINTR` = partial, more to come).
Perf counter `dedup_list_lat`.

## 7. Downstream (summary)

- OSD: `MOSDDedupList` / reply (feature `OSD_DEDUP_LIST`), served by a
  dedicated finisher, bounded by `osd_dedup_list_max_duration`.
- PG history: `last_deep_scrub_start_stamp`, used by the tool's
  `--since-last-scrub` (keep only records newer than the PG's last deep
  scrub start, with 2 s clock-skew margin).
- librados `dedup_candidate_list` (per-OSD batches) and
  `ceph-dedup-tool search-dups` (k-way merge by digest across OSDs,
  lowest-frontier fetching, `--dedup-batch-size`,
  `--dedup-parallel-osds`).
- Next (designed, not implemented): resolving records to object names and
  linking duplicates.

## 8. Pitfalls and lessons

- **Record only once per object**: only the primary (`PRIMARY` flag) and
  only non-shard objects. Replicas would duplicate records, EC shards
  aren't object content.
- **Hash outside the collection lock**: defer MD5 and the KV submit, and
  share the read bufferlist instead of copying it.
- **Digests span several reads**: never combine chunks of different object
  states; abandoning is preferable to a wrong digest.
- **The single builder per collection** relies on scrub reading a PG's
  objects one at a time.
- **FORCE_V2 encoding** drops newer onode fields (section 5).
- **Wall clock** for persistent timestamps; tolerate skew when comparing
  with other daemons' stamps.
- **Record format changes** require `bluestore dedup clear` on OSDs
  holding old records.
- **nid/data_version are per OSD copy**: don't compare them across
  replicas.

## 9. Limitations / open items

- EC pools: no dedup info (per-shard digests idea).
- Record growth: no automatic pruning policy besides listing-time prune.
- Record value stores the full `ghobject_t` (compact format pending);
  digest is a `std::string` (fixed-size type pending).
- MD5 vs SHA-256: SHA-256 proposed so the digest can serve as content
  identity for linking.
- `D` column family sharding applies only to OSDs created (mkfs) with it.
- Crimson: not implemented.

## 10. Tests

`ceph_test_objectstore --gtest_filter='*StoreTestReformatting*CollectCsum*:*StoreTestReformatting*DataVersion*'`
(each test runs with `bluestore_write_v2` = false/true):

| Test | Covers |
|---|---|
| CollectCsumTest | records, sharded listing, prune |
| DataVersionTest | data_version bumps per op, persistence |
| CollectCsumChunkedTest | multi-chunk digests, abandoned digests |
| CollectCsumECShardTest | no records for shard objects |
| CollectCsumUnlockedTest | recording outside the collection lock |

Also `unittest_bluestore_types`, `ceph-dencoder` round trips for
`bluestore_onode_t`, and `ceph_test_dedup_merge` (tool merger
benchmark/tests, not part of `make check`).
