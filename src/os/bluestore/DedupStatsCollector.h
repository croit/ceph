// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_OSD_DEDUP_STATS_COLLECTOR_H
#define CEPH_OSD_DEDUP_STATS_COLLECTOR_H

#include <string>
#include <vector>
#include <memory>
#include <mutex>
#include <shared_mutex>

#include "common/ceph_mutex.h"
#include "common/ceph_time.h"

class CephContext;
class KeyValueDB;
struct ghobject_t;

extern const std::string PREFIX_DEDUP_CSUM; // = "D";

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
    const mono_clock::time_point& ts);
  static void get_key_dedup_entry(
    const char* p, int64_t& pool,
    std::string& digest, size_t digest_len,
    ceph::mono_clock::time_point& ts,
    uint32_t oid_hash);
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
    const bufferlist& bl);

  // returns:
  // >0 - amount of retrieved entries, more entries to be retrieved
  //  0 - interrupted, more entries to be retrieved,
  // -1 - end of list, no more entries
  int list_candidates(
    KeyValueDB& db,
    int64_t pool,
    uint64_t digest_msb,
    size_t digest_msb_bits,
    size_t all_digest_bits,
    const ceph::mono_clock::time_point& retrieve_after_btime,
    const ceph::mono_clock::time_point& remove_before_btime,
    int max_return,
    int max_deletion,
    double max_duration,
    std::vector<std::string>& ls,
    std::string* pnext);

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
