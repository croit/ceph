// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_OSD_DEDUP_STATS_COLLECTOR_H
#define CEPH_OSD_DEDUP_STATS_COLLECTOR_H

#include <string>
#include <vector>
#include <memory>
#include <optional>
#include <mutex>
#include <shared_mutex>

#include "common/ceph_crypto.h"
#include "common/ceph_mutex.h"
#include "common/ceph_time.h"
#include "common/hobject.h"
#include "os/dedup_types.h"

class CephContext;
class KeyValueDB;

extern const std::string PREFIX_DEDUP_CSUM; // = "D";

/**
 * Builds an object's content digest from consecutive reads of the object,
 * e.g. made by deep scrub in osd_deep_scrub_stride chunks.
 *
 * A digest is started by a read at offset 0 and is complete once the reads
 * reached the object size. Reads of another object, non-consecutive reads
 * or an object size change abandon the digest in progress, hence it might
 * be lost but is never built from wrong data, except for the case below.
 *
 * Relies on deep scrub behavior, as scrub's own data digest does:
 * - a single scrub per PG at a time, processing PG's objects one by one,
 *   i.e. all the chunks of an object are read before the next object,
 *   hence a single builder per collection suffices. Otherwise digests
 *   would be lost;
 * - writes to the objects being scrubbed are blocked, hence an object
 *   isn't modified between its chunk reads. Otherwise a same size
 *   overwrite in between would result in a wrong digest.
 */
class DedupDigestBuilder
{
  ceph::mutex lock = ceph::make_mutex("DedupDigestBuilder::lock");
  ghobject_t oid;
  uint64_t size = 0;
  uint64_t next = 0; // offset the next read is expected at
  std::unique_ptr<ceph::crypto::MD5> md5; // null if no digest in progress

public:
  /// whether a read of the object at the offset might contribute to
  /// a digest, i.e. starts a new one or continues the one in progress
  bool accepts(const ghobject_t& _oid, uint64_t offset);

  /// feeds data read from the object of 'size' bytes at the offset,
  /// returns the digest once the object is complete
  std::optional<std::string> feed(const ghobject_t& _oid, uint64_t size,
                                  uint64_t offset, const bufferlist& bl);
};

class DedupStatsCollector
{
  CephContext* cct;

  ceph::shared_mutex coll_lock =
    ceph::make_shared_mutex("DedupStatsCollector::lock");
  bool stopped = true;

  static void get_dedup_pool_range(
    int64_t pool,
    std::string& range_start,
    std::string& range_end);
  static std::string get_dedup_entry_key(
    int64_t pool,
    const ghobject_t& oid, const std::string& digest,
    const ceph::real_clock::time_point& ts);
  static void get_key_dedup_entry(
    const char* p, int64_t& pool,
    std::string& digest, size_t digest_len,
    ceph::real_clock::time_point& ts,
    uint32_t& oid_hash);
  static void get_dedup_range(
    int64_t pool, uint64_t digest_msb,
    size_t msb_bits, size_t all_bits,
    std::string& start, std::string& end);

public:
  DedupStatsCollector(CephContext* _cct) : cct(_cct) {
  }
  static void record_candidate(
    CephContext* cct,
    KeyValueDB& kv,
    int64_t pool,
    const ghobject_t& oid,
    const std::string& digest);

  // Lists records whose digests have digest_msb_bits most significant bits
  // equal to digest_msb, i.e. digest shard (digest_msb) out of
  // (1 << digest_msb_bits) total shards.
  // returns:
  // >0 - amount of retrieved entries
  //  0 - end of list, no more entries
  // -EINTR - interrupted due to max_duration, more entries to be retrieved
  // -ECANCELED - collector is stopped
  // Optional pcompleted is set to true when there are no more entries
  // beyond *pnext.
  int list_candidates(
    KeyValueDB& db,
    int64_t pool,
    uint64_t digest_msb,
    size_t digest_msb_bits,
    size_t all_digest_bits,
    const ceph::real_clock::time_point& retrieve_after_btime,
    const ceph::real_clock::time_point& remove_before_btime,
    int max_return,
    int max_deletion,
    double max_duration,
    std::vector<dedup_candidate_t>& ls,
    std::string* pnext,
    bool* pcompleted = nullptr);

  int clear_all_candidates(
    KeyValueDB& db,
    int64_t pool,
    bool async_compact);

  int64_t estimate_size(
    KeyValueDB& db,
    int64_t pool,
    uint64_t* entry_count);

  void start();
  void stop();
};

#endif
