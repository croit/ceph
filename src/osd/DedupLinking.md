# Dedup object linking: design proposal

**Status: proposal, nothing implemented.** This records the design as
discussed in October 2026, with the decisions taken so far and the ones
still open (section 9). Revisit it before implementing.

It's the step after finding duplicates: `ceph-dedup-tool search-dups`
(`src/tools/ceph_dedup/SearchDups.md`) reports groups of objects with
identical content; linking makes them share one copy of the data. The
design is from scratch: it doesn't reuse the OSD's existing manifest,
redirect or chunk tiering mechanisms.

## 1. Model: links to immutable content objects

Two kinds of objects:

- **Content object C**: holds the data once, is **immutable** and named
  by a strong digest of its content, `sha256:<hex>`. Keeps the set of
  its referrers (section 5).
- **Link L**: an ordinary user object whose data has been replaced by a
  reference to C. It keeps its own name, xattrs, omap, snapshots, version
  history and logical size (`stat` is unchanged).

Why a separate immutable object rather than pointing links at one of the
duplicates: a user object can be modified at any time, and then every
link to it would silently change content. With C immutable, a link's
content is a fixed fact. The cost is one data copy per duplicate group,
made once when C is created.

The reference is stored on L as a dedicated xattr, leaving
`object_info_t` untouched:

```
object_ref_t {
  version;
  content_pool, content_oid;            // C
  logical_size;
  digest;                               // sha256, also C's name
  extents: [(l_off, len) -> c_off]      // whole object = one extent
}
```

Whole-object links match what `search-dups` finds; the extent list
leaves room for chunk-level sharing later without a format change.

**Invariant: a reference is added to C before the link exists, and
removed only after the link is gone.** A crash can leak a reference
(cleaned by GC, section 5), never leave a link pointing at missing
content.

## 2. Linking a duplicate group

All content work happens on OSDs; the tool only handles metadata.

### 2.1 Tool

For each group from the merge, the tool picks a **coordinator**: the
current primary of one member, computed from the record's object hash
(hash → PG via `ceph_stable_mod(hash, pg_num)` → primary from the OSD
map). No object names are needed client-side. It sends

```
LINK_GROUP(members: [(recording OSD, record key)...])
```

If the coordinator replies that *its own* member is stale or gone, the
tool resends to the next member's primary, until one succeeds or fewer
than two members remain. The tool spreads groups across primaries to
balance load, and resends to the new primary on coordinator failure
(everything below is idempotent).

There is no separate client-side verification step for linking.

### 2.2 Coordinator

1. **Check caps**: the coordinator acts with OSD authority on other PGs
   and pools, so it first checks the requesting client's caps: write on
   every member's pool and on the content pool.
2. **Own member first** (cheap, local): resolve its record (locally if
   it's also the recording OSD, else via the recording OSD), check
   freshness (section 3). Stale/gone → dedicated error before any remote
   work.
3. **Resolve the other members** in parallel via OSD-to-OSD record
   resolution requests to their recording OSDs: object name plus
   *fresh* / *stale* / *unknown* (section 3) and the object's OSD-level
   version.
4. **Ensure C exists**: check `sha256:<hex>` in the content pool via the
   OSD's internal Objecter; if missing, copy its member's data there (OSD
   to OSD, never through the tool's host).
5. **Reference, then link**, per member (members in parallel):
   - add the member's entry to C's reference set (idempotent);
   - send `MAKE_LINK(ref, expected version)` to the member's primary;
   - if it fails, remove the reference again.
6. **Reply** per member: linked / stale / failed, with object names.

Rules: hold no locks across remote calls (the coordinator's PG lock only
while reading its own member); run `LINK_GROUP` on a dedicated,
size-limited queue (like the dedup listing finisher).

### 2.3 `MAKE_LINK` on the member's primary

One PG transaction:

- assert the object's version equals the expected one (like
  `assert_version()`), else fail;
- for unconfirmed members (or in paranoid mode): read the local data and
  check it hashes to the reference's digest;
- free the data, keep the logical size, store `object_ref_t`;
- PG log entry, so replicas, recovery and backfill apply the same change.

Data movement per group: one copy into C (only if it doesn't exist),
plus local reads only where a re-hash is needed.

### 2.4 Record resolution request

The same OSD-to-OSD request used by the coordinator is also offered to
clients, for report-only `search-dups`: it turns `hash@osd` into object
names and drops stale groups. Input: record keys, grouped per recording
OSD. Output per record: object name, size, verdict, OSD-level version.

## 3. Freshness

A record carries the onode's `<nid, data_version>` (see
`src/os/bluestore/DedupInfoCollection.md`). Both are **local to the
recording OSD's copy**: nids come from each BlueStore's own counter, and
data_version counts that copy's operations (a recovered or backfilled
copy has a different history). So freshness can only be checked on the
recording OSD, while it still holds the object:

- **confirmed fresh**: recording OSD holds the object and its onode
  matches. All copies in a PG receive the same writes, so all copies are
  unchanged;
- **stale**: onode differs, or a new nid (object recreated);
- **unconfirmed**: PG moved off the recording OSD, or it's down. The
  current primary must re-hash before linking, or the member is skipped.

**The check → link gap**: the check runs on the recording OSD,
`MAKE_LINK` on the current primary, possibly another OSD, and a client
write can land in between. Hence the recording OSD also returns the
object's **OSD-level version** (`object_info_t`, assigned by the PG log,
identical on all replicas), and `MAKE_LINK` asserts it atomically.

- `<nid, data_version>` proves the content still matches the recorded
  digest (checked once, on the recording OSD);
- the OSD version guarantees nothing changed between that check and the
  link (checked atomically on the primary).

## 4. Reading, writing, deleting links

- **Reads (decided: proxy for the current phase)**: L's primary reads
  the range from C on its own authority (internal Objecter) and returns
  it. Works with any client; clients need no caps on the content pool.
  Cost: an extra OSD-to-OSD hop and double network transfer. Client
  redirect (Objecter follows "read range X of C") is a later
  optimization needing a feature bit and content pool read caps.
  Stat/xattr/omap are served by L alone.
- **Partial write (copy-up)**: L's primary reads the data from C, makes
  it local again with the write applied and removes the link, in one PG
  transaction. Dropping the reference on C is logged as a pending intent
  in the same transaction, completed by a background step, retried after
  failures.
- **Full overwrite, truncate to 0, delete**: no copy; remove the link,
  queue the dereference the same way.
- **Snapshots**: a clone copies the link and holds its own reference;
  snap trim drops it like a delete.
- **xattr / omap writes**: don't affect the link.

## 5. Reference tracking and GC

- **Reference set** (suggested, not yet decided): one omap entry on C per
  referrer, keyed by pool, object, snap and version. Idempotent adds and
  removes make retries safe; GC can verify each entry against the actual
  link; you can list who uses a content object. Cost: per-referrer
  metadata (a million links = a million omap keys, fine for RocksDB).
  A plain counter is smaller but drifts on retries and can't be
  verified or repaired.
- **Deleting C**: after the last reference goes, with a grace period so
  a concurrent `MAKE_LINK` that already added its reference can't race
  with deletion.
- **GC of leaked references**: background pass checking each entry
  points to a link that points back at C. Leaks need a crash between
  "add reference" and `MAKE_LINK`, so this can run slowly.
- **Scrub**: deep scrub of C checks content against its name for free.
  Scrub of L compares link metadata across replicas, and occasionally
  checks C exists.

## 6. Proposed: SHA-256 as the dedup digest

Awaiting decision. Switch collection from MD5 to SHA-256
(`ceph::crypto::SHA256` in `DedupDigestBuilder`):

- **Grouping becomes identity**: MD5 collisions can be constructed, so
  an MD5 match is only a candidate; with SHA-256 equal digests mean
  equal content in practice.
- **The record digest is C's name**: no need to hash a member to name C.
- **`MAKE_LINK` becomes metadata-only for confirmed-fresh members**:
  version assert plus reference swap, no data read. Only unconfirmed
  members are re-hashed. Optional "paranoid" mode re-hashes always.
- Costs: SHA-256 is slower than MD5 in software (roughly 400–500 vs
  600–700 MB/s per core), faster with SHA CPU extensions (about
  1.5–2 GB/s); runs outside the collection lock, only on pools with
  collection. Records grow from ~40 to ~56 bytes (+~30 % listing traffic
  and merge memory). Format change: key decoding, digest size passed by
  the OSD, tool output, tests; existing records must be cleared. The
  `D(3,0-24)` sharding still covers enough digest bytes.
- Shift in responsibility: correctness then relies on every
  data-changing path bumping `data_version` (the explicit `bump_ver`
  parameter); previously the re-hash would catch a missed bump. Hence the
  paranoid mode.

## 7. Interactions

- **Recovery/backfill** of L moves only metadata; C recovers normally.
- **Dedup collection**: a link holds no BlueStore data, so it looks empty
  and isn't recorded. The content pool should have collection off.
- **Accounting**: pool stats need logical vs stored bytes and a dedup
  ratio; quotas probably on logical bytes (policy choice).
- **EC**: links are metadata and work in any pool; pointing replicated
  pools' links at an EC content pool is the main space win. Dedup info
  for EC pools themselves needs per-shard digests (not designed yet).

## 8. Phases

1. Formats and checks: `object_ref_t`, reference set on C, `MAKE_LINK`
   with its checks, record resolution request, proxy reads. Writes to a
   link rejected or copied up synchronously.
2. Lifecycle: copy-up with deferred dereference, delete, truncate,
   snapshots and trim, reference GC.
3. Tool integration: `LINK_GROUP` with coordinator retries,
   `search-dups --apply`; resolved names in report-only mode.
4. Performance: client redirect reads, statistics, chunk-level extents.

## 9. Decisions

Taken:

- the tool picks the coordinator (a member's primary, from the record
  hash);
- no separate client-side verification for linking; the coordinator
  checks its own member first and the tool retries with the next one;
- record resolution OSD-to-OSD inside `LINK_GROUP`, also exposed to
  clients for report-only search;
- coordinator: client caps check, no locks across remote calls,
  throttled queue;
- proxy reads only for the current phase;
- OSD-level version asserted by `MAKE_LINK` to close the check → link
  gap.

Open:

1. Whole-object vs chunk-level links (suggested: whole-object first).
2. Separate content pool vs content objects in the same pool (a
   dedicated pool allows EC and separate placement, at the cost of
   cross-pool operations).
3. Reference set vs counter (suggested: set).
4. SHA-256 as the dedup digest and content identity (section 6).
