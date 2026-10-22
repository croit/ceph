# Dedup info listing: OSD request and librados API

BlueStore records a content digest for every object deep-scrubbed by its
primary (see `src/os/bluestore/DedupInfoCollection.md`). The records stay
on the OSD that made them. This document describes how clients retrieve
them: the `MOSDDedupList` request served by every OSD, the Objecter and
librados APIs on top of it, and the PG deep scrub start stamp clients use
to tell fresh records from stale ones. The consumer is
`ceph-dedup-tool search-dups`, see `src/tools/ceph_dedup/SearchDups.md`.

Code references use function/class names rather than line numbers.

## 1. Model

- Records are kept **per OSD**, not per PG: they stay on the OSD after
  the PG moves away, and a PG's records might be spread over several
  OSDs (primary changes). Hence listing is addressed to OSDs, not PGs,
  and a client has to query every OSD to see a pool's records.
- Each OSD returns its records **sorted by digest** within the pool
  (that's the KV key order), so a client can merge OSD streams in order
  and see all records of a digest together.
- The digest space is split into **shards** by the digest's most
  significant bits (`shard` out of `2^shard_bits`). Records of the same
  digest are always in one shard, so shards can be processed
  independently and in parallel, each with bounded memory.
- Records carry the digest, the wall clock timestamp and the object
  hash, **not the object name**. The hash is enough to compute PG and
  primary; names stay on the OSD (resolution is a separate, planned
  request).
- Dedup info is best effort: down OSDs are skipped, lost records are
  re-created by the next deep scrub.

## 2. OSD side

### 2.1 Messages (`src/messages/MOSDDedupList.h`, `src/osd/dedup_list_types.h`)

`MOSDDedupList` (`MSG_OSD_DEDUP_LIST` = 137, v1):

| Field | Meaning |
|---|---|
| `fsid`, `epoch` | cluster check / client's map epoch |
| `pool` | pool id |
| `shard`, `shard_bits` | digest shard, `shard < 2^shard_bits`, `shard_bits <= 32` |
| `after` | opaque cursor (raw KV key), empty to start |
| `max` | max entries to return, > 0 |
| `newer_than` | seconds; skip records older than that (0: no limit) |
| `prune_older_than` | seconds; delete records older than that while iterating (0: none) |

`MOSDDedupListReply` (`MSG_OSD_DEDUP_LIST_REPLY` = 138, v1): `r` and
`osd_dedup_list_result_t { completed, next, now, entries }` (versioned
ENCODE v1); entries are `osd_dedup_list_entry_t { digest, timestamp (ns),
oid_hash (bitwise key form) }`. `now` is the OSD's clock, so the client
can compute record ages without trusting its own clock.

Both messages are registered in `ceph-dencoder`.

Feature bit `OSD_DEDUP_LIST` (51) marks OSDs that understand the request.
Clients check it in the OSD's `osd_xinfo_t` features before sending.

### 2.2 Handling (`OSD::handle_fast_dedup_list`, `OSD::process_dedup_list`)

- Fast-dispatched. The fast path only validates: session, fsid, shard
  args, `max`, pool existence and **caps**: pool-wide read (records
  cover all namespaces), plus write if `prune_older_than > 0`. Errors
  reply right away (`-EACCES`, `-EINVAL`, `-ENOENT`, `-EPERM`).
- The work is queued to a dedicated `Finisher` (`dedup_list_finisher`,
  thread `dedup_list`), so requests are processed **sequentially**, never
  on the messenger or op threads, and can't starve client I/O.
- `process_dedup_list()` converts the `newer_than` / `prune_older_than`
  ages to absolute times on the OSD clock and calls
  `ObjectStore::dedup_candidate_list()` (BlueStore:
  `DedupStatsCollector::list_candidates()`), bounded by
  `osd_dedup_list_max_duration` (default 1 s, 0 = unlimited). A
  time-limited partial result (`-EINTR`) is a success with fewer
  entries and `completed == false`.
- While stopping, the request is dropped without a reply: the client
  resends to the OSD once it's back (or fails the op when it's down).
- Perf counter `bluestore.dedup_list_lat`; slow listings are logged.

### 2.3 PG deep scrub start stamp

PG stats used to provide only the deep scrub *completion* time. Records
made by a deep scrub have timestamps between its start and completion,
so the start time is what tells whether a record comes from the latest
deep scrub of its PG. Added:

- `pg_history_t::last_deep_scrub_start_stamp` (encoding v11) and
  `pg_stat_t::last_deep_scrub_start_stamp` (v32), dumped by
  `pg ls-by-pool` etc.;
- classic OSD: the scrub session start (`m_session_started_at`), stored
  in history when the deep scrub completes; crimson: the time the scrub
  is started;
- new PGs get their creation time, like the other scrub stamps;
- `pg_history_t::merge()` takes the start stamp along with a newer
  completion stamp (or a newer start stamp for the same completion).

## 3. Client side

### 3.1 Objecter (`Objecter::enumerate_dedup_items<T>`)

Sends one `MOSDDedupList` to one OSD and returns
`(ec, entries, completed, next)` to an async callback. Built on the
command machinery: `osd_request_()` creates a `CommandOp` with a
`make_request` factory instead of an `MCommand`, so the request is
tracked like a command (resent on session reset with the same tid,
failed when the OSD goes down/DNE or on timeout). The reply handler
`handle_dedup_list_reply()` re-encodes the result into the command
reply path (`_handle_command_reply()`).

Precondition errors (`osd < 0`, `max < 1`, bad shard) and missing pool
fail immediately; OSDs lacking the feature fail with
`operation_not_supported`. Entries get the hash converted back from the
bitwise key form (`hobject_t::_reverse_bits`) and an age computed from
the OSD's `now`.

### 3.2 librados (`librados.hpp`, `librados_cxx.cc`)

Types:

- `DedupItem { digest (raw MD5), hash, timestamp (ns), age (s), osd }`;
- `DedupItemBatch { osd, completed, items }`: one OSD's reply, sorted by
  digest, continuing that OSD's previous batch;
- `DedupItemCursor`: shard, the OSD list, per-OSD position and
  completion, round-robin position; `is_end()`, `get_osds()`.

Calls (`IoCtx`):

```cpp
int dedup_candidate_list_slice(size_t n, size_t m, DedupItemCursor* c);
// shard n of m (m a power of 2, <= 2^32); targets = up OSDs with OSD_DEDUP_LIST

int dedup_candidate_list(DedupItemCursor& c, size_t max_targets,
                         size_t per_target_max_items,
                         std::vector<DedupItem>* result, newer_than, prune_older_than);
int dedup_candidate_list(DedupItemCursor& c, size_t max_targets, ...,
                         std::vector<DedupItemBatch>* result, ...);
// up to max_targets incomplete OSDs, chosen round-robin

int dedup_candidate_list(DedupItemCursor& c, const std::vector<int>& osds, ...,
                         std::vector<DedupItemBatch>* result, ...);
// exactly these OSDs (must be the cursor's ones, -EINVAL otherwise)

bool dedup_candidate_list_is_end(const DedupItemCursor& c);
```

Requests to the chosen OSDs are sent in parallel and all awaited even if
some fail. The cursor advances only for OSDs that replied; the first
error is returned, otherwise the record count. The flat `DedupItem`
variant loses per-OSD ordering and is fine for small listings only; the
batch variants exist so a caller can merge in digest order (the tool
does), and the explicit-OSD variant lets the merger choose who to fetch
from next.

The OSD set is fixed when the cursor is created (OSD map at that time).

## 4. Design decisions

- **Dedicated message rather than a PG op or an `MCommand`**: records
  aren't per PG and a PG op would need a PG; `MCommand` goes through the
  admin command path with JSON in/out and its own caps semantics. A
  typed message is compact (millions of records) and checks pool caps.
- **Cursor is opaque** (the raw KV key): the OSD resumes exactly where it
  stopped even with records added or removed meanwhile.
- **Time-bounded requests** instead of only count-bounded: pruning and
  `newer_than` skipping may iterate many records returning none.
- **Ages instead of absolute times** in the request, converted with the
  OSD's own clock: no client/OSD clock skew in filtering. Timestamps in
  replies are still absolute (needed to compare with PG stamps).
- **Pruning piggybacks on listing**: no separate maintenance request,
  records are only read by listing anyway.
- **Command machinery reuse** (`CommandOp::make_request`): gets resend,
  OSD-down and timeout handling without a new op type.

## 5. Pitfalls

- A new message type needs the feature bit and the client-side check:
  older OSDs can't decode it, so the request would never be answered.
- Records of a PG may sit on OSDs that no longer serve it: always query
  all up OSDs, not just acting sets.
- Resend after session reset relies on `make_request` being invoked
  again with the original arguments: not exercised by tests yet.
- Feature-bit downgrade race: an OSD restarted with an older binary
  between the check and the send gets an undecodable message (open).

## 6. Open items

- No C API, no librados test stub (`librados_test_stub`) support.
- Object name resolution request (records → names, freshness check via
  nid/data_version on the recording OSD).
- Crimson OSD doesn't serve `MOSDDedupList`.
