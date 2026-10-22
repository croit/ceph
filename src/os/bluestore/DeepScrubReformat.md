# BlueStore: data-at-rest reformatting during deep scrub

This document describes how BlueStore piggybacks on deep scrub reads to
process object data at rest: the reformat engine framework and the
recompression and defragmentation engines. It covers the design, the
reasons behind it, the code layout and the traps found while building it.

Dedup info collection (the `collect_csum` engine) is built on this
framework and described separately in `DedupInfoCollection.md`.

Code references use function/class names rather than line numbers.

**Code history**: the series starts with commit
`a483262451624808e3ce2009dea277d50c076c9a` "osd,mon: add
DEEP_SCRUB_REFORMAT pool option", so `git log a483262451624808e3ce2009dea277d50c076c9a^..`
lists the reformat framework, the dedup info collection and everything
built on them.

## 1. Why deep scrub

Deep scrub already reads every byte of every object on every OSD
periodically, with its own throttling (`osd_scrub_sleep`, scheduling,
`osd_deep_scrub_stride`). Doing extra per-object work on that data costs
CPU but no extra disk reads, and it gets spread over time by the scrub
scheduler for free. Hence:

- **recompression**: data written uncompressed (compression enabled later,
  or `*_lazy` compression modes, see 4.3) gets compressed;
- **defragmentation**: objects whose extents are scattered get rewritten
  into fewer physical extents;
- **dedup info collection**: an MD5 digest of the whole object content is
  recorded in the OSD's KV store, so duplicates can be found cluster-wide
  later without reading any data (`DedupInfoCollection.md`).

Everything is opt-in per pool via the `deep_scrub_reformat` pool option.

## 2. Configuration

```
ceph osd pool set <pool> deep_scrub_reformat "collect_csum, recompress, defragment"
```

- The value is a list of engine names separated by `,` or `;`; leading
  whitespace of each token is ignored, the token is matched by prefix
  (`args.starts_with(...)`), the rest of the token is kept as engine args
  (currently unused).
- The option is **not validated** by the monitor (open item). BlueStore
  therefore logs and ignores unknown tokens (`_update_reformat_engines`),
  which also covers engines unknown to an older release in a mixed cluster.
- Engines are (re)built per collection in `_update_reformat_engines()`,
  called from `_set_collection_opts()`, i.e. whenever pool options change.

Related options:

| Option | Meaning |
|---|---|
| `compression_mode` / `bluestore_compression_mode` | adds `passive_lazy`, `aggressive_lazy`, `force_lazy`: compress only during reformatting (4.3) |
| `osd_deep_scrub_stride` | scrub read size; each read is reformatted separately |

## 3. The scrub read path

`ReplicatedBackend::be_deep_scrub()` and `ECBackend::be_deep_scrub()`
read objects with `scrub_fadvise_flags`, which include
`CEPH_OSD_OP_FLAG_SCRUB` (0x100). This branch adds
`CEPH_OSD_OP_FLAG_PRIMARY` (0x200) when the OSD is the PG's primary.

Properties BlueStore relies on (verify them again if scrub changes):

- An object is read in consecutive chunks of `osd_deep_scrub_stride`
  bytes, starting at offset 0, and all chunks of an object are read before
  the next object of the PG.
- Replicated: the last chunk is shorter. EC: the stride is rounded up to
  the EC chunk size and the read is issued for the full stride (short
  read at the end).
- Every replica (and every EC shard) scrubs its own copy, so recompress /
  defragment run on every OSD independently, which is what we want: the
  on-disk layout is local to each OSD.
- EC shard objects (`oid.shard_id != NO_SHARD`) hold part of the data or
  parity, not the object content.

## 4. The reformat framework

Files: `OnodeReformat.{h,cc}`, `BlueStore::read()`,
`_maybe_do_reformat_onode()`, `_reformat_scan()`,
`_txc_exec_reformat_write()`, engine ids in `bluestore_common.h`.

### 4.1 Engines

```cpp
class OnodeReformatEngine {
  virtual bool modifies_object() const { return true; }
  virtual bool validate(OnodeReformatContext& ctx);   // may this read be handled?
  virtual bool execute(OnodeReformatContext& ctx, PerfCounters&) = 0;
};
```

Engine id doubles as priority (`bluestore_common.h`):

| id | engine | modifies object |
|---|---|---|
| 0 | `CHECKSUM_COLLECTION_ENGINE` (`collect_csum`) | no |
| 1 | `RECOMPRESS_ENGINE` (`recompress`) | yes |
| 2 | `DEFRAGMENT_ENGINE` (`defragment`) | yes |

A collection holds `reformat_engines_t` = array indexed by engine id plus
the atomic `has_modifying_reformat_engines`.

The default `validate()` accepts only `SCRUB` reads whose offset is
aligned to `min_alloc_size` (an unaligned tail is fine).

### 4.2 Read flow

`BlueStore::read()`:

1. **Lock mode chosen upfront.** Scrub reads of collections with modifying
   engines take `c->lock` exclusively, all other reads shared. Upgrading
   shared → unique later isn't atomic, and the exclusive lock is needed so
   no client write sneaks in between reading the data and rewriting it.
2. `OnodeReformatContext` is built (ctor): nothing is enabled for a
   missing onode; otherwise each engine is offered `validate()`, but
   modifying engines only if the lock is exclusive (pool opts may have
   changed after the lock mode was chosen).
3. `_do_read()` is passed `&span_stat` when any engine is enabled;
   `_reformat_scan()` then fills `span_stat_t` for the read range:
   `stored`, `cached` (bytes in buffer cache), `allocated`,
   `stored_compressed`, `allocated_compressed`, `allocated_shared`
   (shared-blob extents with refs > 1), `extents`, `frags` (number of
   merged physical intervals).
4. `_maybe_do_reformat_onode()` (still under the lock):
   `_choose_write_options()` fills the context's `WriteContext` using the
   read's op flags (this is how `*_lazy` modes turn compression on), then
   `exec_engines()`:
   - modifying engines in priority order **until the first one returning
     true** (only one rewrite per read);
   - then all non-modifying engines, unconditionally.
   If a modifying engine succeeded, `_txc_exec_reformat_write()` rewrites
   the range; preallocated space left unused is released.
5. The lock is released, then `run_deferred()` runs work the engines
   deferred via `OnodeReformatContext::defer()` (e.g. dedup digest and
   record). Non-modifying engines run under the shared lock and must not
   do lengthy work in `execute()`; deferred closures must not touch the
   onode, the collection or the context, so they capture what they need.

### 4.3 The reformat write

`_txc_exec_reformat_write()` creates its own `TransContext` on the
collection's sequencer, calls **classic** `_do_write()` with the
prepared `WriteContext` (regardless of `bluestore_write_v2`), writes the
onode and submits. It doesn't go through `_write()`, so:

- the onode `data_version` is **not** bumped (content unchanged, so
  dedup records stay valid, see `DedupInfoCollection.md`);
- the OSD knows nothing about it: no object_info/version/mtime change, no
  replication. Reformatting is purely local.

`WriteContext` extensions used by engines:

- `precompressed`: blobs were already compressed by the recompress
  engine; `_do_write_big()` skips its own processing and
  `_do_alloc_write()` takes `wi.compressed_bl` instead of compressing
  again (still applying the required ratio check).
- `prealloc_slicer`: space preallocated by `maybe_allocate()` is consumed
  by `_do_alloc_write()` before allocating more.

`*_lazy` compression modes (`Compressor::COMP_*_LAZY`): same hint logic
as their non-lazy counterparts but `wctx.compress` is set only when the
write's flags include `CEPH_OSD_OP_FLAG_SCRUB`, i.e. for reformat writes.
Client writes stay uncompressed (low latency), data gets compressed later
by deep scrub. Requires `recompress` in `deep_scrub_reformat`.

## 5. Modifying engines

Both refuse when `span_stat.cached != 0` (object likely being written:
buffers stay cached until the write commits) or
`allocated_shared != 0` (rewriting shared blobs, e.g. after clone, would
increase used space).

### 5.1 recompress (`OnodeReformatRecompressEngine::execute`)

Conditions: `wctx.compress && wctx.compressor && allocated > 0` plus the
above. `wctx.compressor` may be null if the compression plugin failed to
load: this used to segfault (fixed, `RecompressNoCompressorTest`).

Algorithm: split the read data into `target_blob_size` pieces, compress
each piece longer than `min_alloc_size` into a new blob's write item,
sum `need = Σ roundup(result, min_alloc_size)` and apply only if
`need < allocated + allocated_compressed`. Compressed blobs are kept
(`precompressed = true`) either way. Counters:
`reformat_compress_attempted`, `reformat_compress_omitted`.

Note `allocated` covers only uncompressed blobs' read regions while
compressed blobs count their whole on-disk size, so the estimate is
conservative for partially read compressed blobs.

### 5.2 defragment (`OnodeReformatDefragmentEngine::execute`)

Conditions: `frags > 1` plus the above. Preallocates
`roundup(length, min_alloc_size)` via `OnodeReformatContext::maybe_allocate()`;
accepted if fully allocated with fewer fragments than now, otherwise the
space is released. Counters: `reformat_defragment_attempted`,
`reformat_defragment_omitted`. `maybe_allocate()` can be used once per
context.

Common counters: `reformat_issued` (rewrites submitted), `reformat_lat`.


## 6. Pitfalls and lessons

- **Lock upgrade isn't atomic**: choose shared/unique before reading.
- **Don't do heavy work under the collection lock** in non-modifying
  engines: use `defer()`.
- **The engine list is a pool option and isn't validated**: unknown names
  are ignored with `derr`. Missing objects get no engines, as validators
  may dereference the onode.
- **The compressor may be null** even when `wctx.compress` is set (failed
  plugin load).
- **The reformat write bypasses `_write()`**: right for `data_version`,
  but remember it when adding per-write accounting there.
- **EC**: the stride is rounded to the EC chunk size and shard objects
  aren't object content.
- **Dev/test environment**: run `ceph_test_objectstore` with
  `--plugin_dir=<build>/lib`, as stale system compression plugins break
  compression tests. Adding a `with_legacy` option changes the legacy
  config layout: rebuild all client binaries (librados, rados, python
  bindings, tools, mon, mgr), not just the OSD. Use a fresh `--log-file`
  per run (logs are appended).

## 7. Limitations / open items

- No mon-side validation of `deep_scrub_reformat`.
- Reformatting is skipped for reads unaligned at the start, objects with
  cached data and ranges with shared blobs.
- Only one modifying engine applies per read.
- Crimson: not implemented.

## 8. Tests

`ceph_test_objectstore --gtest_filter='*StoreTestReformatting*'`
(each test runs with `bluestore_write_v2` = false/true):

| Test | Covers |
|---|---|
| BasicTest | defragment |
| CompressedTest | recompress, recompress+defragment |
| LazyCompressionTest | `*_lazy` modes |
| RecompressNoCompressorTest | null compressor |
| ReformatMissingObjectTest | no engines for missing objects |
| UnknownReformatEngineTest | unknown engine names ignored |

The `collect_csum` tests are listed in `DedupInfoCollection.md`.
