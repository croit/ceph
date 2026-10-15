// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include "BlueAdmin.h"
#include "Compression.h"
#include "common/errno.h"
#include "common/pretty_binary.h"
#include "os/bluestore/BlueStore.h"
#include "os/bluestore/DedupStatsCollector.h"
#include "common/ceph_crypto.h"
#include "common/debug.h"
#include <asm-generic/errno-base.h>
#include <iostream>
#include <sstream>
#include <vector>
#include <limits>
#include <optional>

#define dout_subsys ceph_subsys_bluestore
#define dout_context store.cct

using ceph::bufferlist;
using ceph::Formatter;
using ceph::common::cmd_getval;

std::string bin2hex_str(const std::string& s); // DedupStatsCollector.cc

static bool hex2bin_str(const std::string& s, std::string* out)
{
  auto nibble = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  if (s.size() % 2) {
    return false;
  }
  out->clear();
  out->reserve(s.size() / 2);
  for (size_t i = 0; i < s.size(); i += 2) {
    int hi = nibble(s[i]);
    int lo = nibble(s[i + 1]);
    if (hi < 0 || lo < 0) {
      return false;
    }
    out->push_back(char((hi << 4) | lo));
  }
  return true;
}

static void dump_avg_latency(Formatter *f, const char *name, const BlueStore::CacheStatsSnapshot::tavg_t& tavg) {
  uint64_t sum_ns = std::get<0>(tavg);
  uint64_t count = std::get<1>(tavg);
  f->open_object_section(name);
  f->dump_unsigned("avgcount", count);
  f->dump_float("sum", sum_ns / 1000000000.0);
  if (count) {
    f->dump_float("avgtime", (sum_ns / (double)count) / 1000000000.0);
  } else {
    f->dump_float("avgtime", 0.0);
  }
  f->close_section();
}

static void dump_cache_section(Formatter *f, const char *name,
                               uint64_t hits, uint64_t misses,
                               const BlueStore::CacheStatsSnapshot::tavg_t& tavg,
                               bool is_byte_cache,
                               std::optional<double> duration = std::nullopt) {
  f->open_object_section(name);
  dump_avg_latency(f, is_byte_cache ? "buffer_miss_latency" : "miss_latency", tavg);

  f->dump_unsigned(is_byte_cache ? "hit_bytes" : "hits", hits);
  f->dump_unsigned(is_byte_cache ? "miss_bytes" : "misses", misses);
  uint64_t total = hits + misses;
  f->dump_unsigned(is_byte_cache ? "total_byte_accesses" : "total", total);
  if (total > 0) {
    f->dump_float(is_byte_cache ? "byte_hit_ratio" : "hit_ratio", (double)hits / (double)total);
  } else {
    f->dump_float(is_byte_cache ? "byte_hit_ratio" : "hit_ratio", 0.0);
  }

  if (duration && *duration > 0) {
    f->dump_float("accesses_per_second", (double)total / *duration);
  }

  f->close_section();
}

static void dump_snapshot_section(Formatter *f, const char *name,
                                  const BlueStore::CacheStatsSnapshot& snap,
                                  std::optional<double> duration = std::nullopt) {
  f->open_object_section(name);
  if (duration) {
    f->dump_float("seconds elapsed", *duration);
  }
  dump_cache_section(f, "onode_cache", snap.onode_hits, snap.onode_misses,
                     snap.onode_miss_latency,
                     false, duration);
  dump_cache_section(f, "onode_shard", snap.onode_shard_hits, snap.onode_shard_misses,
                     snap.onode_shard_miss_latency,
                     false, duration);
  dump_cache_section(f, "object_data_cache", snap.buffer_hit_bytes, snap.buffer_miss_bytes,
                     snap.buffer_miss_latency,
                     true, duration);
  f->close_section();
}

void BlueStore::get_snapshot_windows(BlueStore::CacheStatsSnapshot& current,
                                     BlueStore::CacheStatsSnapshot& most_recent,
                                     BlueStore::CacheStatsSnapshot& oldest) {
  std::lock_guard l(cache_stats_lock);

  // Get current values
  current.timestamp = ceph::mono_clock::now();
  current.onode_hits = logger->get(l_bluestore_onode_hits);
  current.onode_misses = logger->get(l_bluestore_onode_misses);
  current.onode_miss_latency = logger->get_tavg_ns(l_bluestore_onode_miss_lat);
  current.onode_shard_hits = logger->get(l_bluestore_onode_shard_hits);
  current.onode_shard_misses = logger->get(l_bluestore_onode_shard_misses);
  current.onode_shard_miss_latency = logger->get_tavg_ns(l_bluestore_onode_shard_miss_lat);
  current.buffer_hit_bytes = logger->get(l_bluestore_buffer_hit_bytes);
  current.buffer_miss_bytes = logger->get(l_bluestore_buffer_miss_bytes);
  current.buffer_miss_latency = logger->get_tavg_ns(l_bluestore_buffer_miss_lat);

  cache_stats_snapshots.push_back(current);
  while (cache_stats_snapshots.size() > MAX_CACHE_SNAPSHOTS) {
    cache_stats_snapshots.pop_front();
  }

  if (cache_stats_snapshots.size() >= 2) {
    most_recent = cache_stats_snapshots[cache_stats_snapshots.size() - 2];
  } else {
    most_recent = cache_stats_snapshots.back();
  }
  oldest = cache_stats_snapshots.front();
}


BlueStore::SocketHook::SocketHook(BlueStore& store)
  : store(store)
{
  AdminSocket *admin_socket = store.cct->get_admin_socket();
  if (admin_socket) {
    int r = admin_socket->register_command(
      "bluestore collections",
      this,
      "list all collections");
    if (r != 0) {
      dout(1) << __func__ << " cannot register SocketHook" << dendl;
      return;
    }
    r = admin_socket->register_command(
      "bluestore list "
      "name=collection,type=CephString,req=true "
      "name=start,type=CephString,req=false "
      "name=max,type=CephInt,req=false",
      this,
      "list objects in specific collection");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore onode metadata "
      "name=object_name,type=CephString,req=true",
      this,
      "print object internals");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore compression stats "
      "name=collection,type=CephString,req=false",
      this,
      "print compression stats, per collection");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore runtime frag score "
      "name=collection,type=CephString,req=false",
      this,
      "print runtime fragmentation score, per collection");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore clear runtime frag "
      "name=collection,type=CephString,req=false",
      this,
      "clear runtime fragmentation score, per collection");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore static frag score "
      "name=collection,type=CephString,req=false",
      this,
      "print static fragmentation score, per collection");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore clear static frag "
      "name=collection,type=CephString,req=false",
      this,
      "clear static fragmentation score, per collection");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore cache stats",
      this,
      "print cache performance stats");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore show sharding ",
      this,
      "print RocksDB sharding");
    ceph_assert(r == 0);
    r = admin_socket->register_command("bluestore bluefs-bdev-expand",
                                       this,
                                       "Instruct BlueFS to check the size of its block devices"
                                       " and, if they have expanded, make use of the additional space.");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore dedup stats "
      "name=pool,type=CephInt,req=true",
      this,
      "print statistics for accumulated deduplication info, per pool");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore dedup clear "
      "name=pool,type=CephInt,req=true",
      this,
      "clear accumulated deduplication info, per pool");
    ceph_assert(r == 0);
    r = admin_socket->register_command(
      "bluestore dedup list "
      "name=pool,type=CephInt,req=true "
      "name=shard,type=CephInt,range=0,req=false "
      "name=shard_bits,type=CephInt,range=0|32,req=false "
      "name=after,type=CephString,req=false "
      "name=max,type=CephInt,range=1,req=false "
      "name=newer_than,type=CephInt,range=0,req=false "
      "name=prune_older_than,type=CephInt,range=0,req=false "
      "name=max_duration,type=CephFloat,range=0.0,req=false",
      this,
      "list accumulated deduplication info for a pool, "
      "limited to the digest shard specified by shard_bits most significant "
      "digest bits being equal to shard. 'after' is an opaque cursor "
      "returned as 'next' by the previous call. Optionally lists records "
      "made within the last 'newer_than' seconds only and removes ones "
      "older than 'prune_older_than' seconds.");
    ceph_assert(r == 0);
  }
}

BlueStore::SocketHook::~SocketHook()
{
  AdminSocket *admin_socket = store.cct->get_admin_socket();
  if (admin_socket) {
    admin_socket->unregister_commands(this);
  }
}

int BlueStore::SocketHook::call(
  std::string_view command,
  const cmdmap_t& cmdmap,
  const bufferlist& inbl,
  Formatter *f,
  std::ostream& ss,
  bufferlist& out)
{
  int r = 0;
  if (command == "bluestore collections") {
    std::vector<coll_t> collections;
    store.list_collections(collections);
    std::stringstream result;
    for (const auto& c : collections) {
      result << c << std::endl;
    }
    out.append(result.str());
    return 0;
  } else if (command == "bluestore list") {
    std::string coll;
    std::string start;
    int64_t max;
    cmd_getval(cmdmap, "collection", coll);
    cmd_getval(cmdmap, "start", start);
    if (!cmd_getval(cmdmap, "max", max)) {
      max = 100;
    }
    if (max == 0) {
      max = std::numeric_limits<int>::max();
    }
    coll_t c;
    if (c.parse(coll) == false) {
      ss << "Cannot parse collection" << std::endl;
      return -EINVAL;
    }
    BlueStore::CollectionRef col = store._get_collection(c);
    if (!col) {
      ss << "No such collection" << std::endl;
      return -ENOENT;
    }
    ghobject_t start_object;
    if (start.length() > 0) {
      if (start_object.parse(start) == false) {
        ss << "Cannot parse start object";
	return -EINVAL;
      }
    }
    std::vector<ghobject_t> list;
    {
      std::shared_lock l(col->lock);
      r = store._collection_list(col.get(), start_object, ghobject_t::get_max(),
        max, false, &list, nullptr);
    }
    if (r != 0) {
      return 0;
    }
    std::stringstream result;
    for (auto& obj : list) {
      result << obj << std::endl;
    }
    out.append(result.str());
    return 0;
  } else if (command == "bluestore onode metadata") {
    std::string object_name;
    cmd_getval(cmdmap, "object_name", object_name);
    ghobject_t object;
    if (!object.parse(object_name)) {
      ss << "Cannot parse object" << std::endl;
      return -EINVAL;
    }
    std::shared_lock l(store.coll_lock);
    for (const auto& cp : store.coll_map) {
      if (cp.second->contains(object)) {
        std::shared_lock l(cp.second->lock);
        OnodeRef o = cp.second->get_onode(object, false);
        if (!o || !o->exists) {
          ss << "Object not found" << std::endl;
          return -ENOENT;
        }
        o->extent_map.fault_range(store.db, 0, 0xffffffff);
        using P = BlueStore::printer;
        std::stringstream result;
        result << o->print(P::PTR + P::DISK + P::USE + P::BUF + P::CHK + P::ATTRS) << std::endl;
        out.append(result.str());
        return 0;
      }
    }
    r = -ENOENT;
    ss << "No collection that can hold such object" << std::endl;
  } else if (command == "bluestore compression stats") {
    std::vector<CollectionRef> copied;
    {
      std::shared_lock l(store.coll_lock);
      copied.reserve(store.coll_map.size());
      for (const auto& c : store.coll_map) {
        copied.push_back(c.second);
      }
    }
    std::string coll;
    cmd_getval(cmdmap, "collection", coll);
    f->open_array_section("compression");
    for (const auto& c : copied) {
      std::shared_lock l(c->lock);
      if ((coll.empty() && bool(c->estimator))
        || coll == c->get_cid().c_str()) {
        f->open_object_section("collection");
        f->dump_string("cid", c->get_cid().c_str());
        f->open_object_section("estimator");
        if (c->estimator) {
          c->estimator->dump(f);
        }
        f->close_section();
        f->close_section();
      }
    }
    f->close_section();
    return 0;
  } else if (command == "bluestore show sharding") {
    int r = 0;
    std::string sharding;
    if (store.get_db_sharding(sharding)) {
      out.append(sharding + '\n');
    } else {
      r = -EFAULT;
      ss << "Failed to get sharding" << std::endl;
    }
    return r;
  } else if (command == "bluestore runtime frag score") {
    std::shared_lock l(store.coll_lock);
    std::string coll;
    cmd_getval(cmdmap, "collection", coll);
    f->open_array_section("runtime_frag_score");
    for (const auto& it : store.coll_map) {
      auto c = it.second;
      std::shared_lock l(c->lock);
      if (coll.empty() || coll == c->get_cid().c_str()) {
        f->open_object_section("collection");
        f->dump_string("cid", c->get_cid().c_str());
        auto samples = c->runtime_read_samples.load(std::memory_order_relaxed);
        auto sum = c->runtime_frag_count.load(std::memory_order_relaxed);
        f->dump_unsigned("object_read_samples", samples);
        f->dump_unsigned("runtime_frag_count", sum);
        if (samples == 0) {
          f->dump_int("runtime_frag_score", 0);
        } else {
          f->dump_float("runtime_frag_score", (float)sum / samples);
        }
        f->close_section();
      }
    }
    f->close_section();
    return 0;
  } else if (command == "bluestore clear runtime frag") {
    std::shared_lock l(store.coll_lock);
    std::string coll;
    cmd_getval(cmdmap, "collection", coll);
    for (const auto& it : store.coll_map) {
      auto c = it.second;
      std::shared_lock l(c->lock);
      if (coll.empty() || coll == c->get_cid().c_str()) {
        c->runtime_frag_count.store(0, std::memory_order_relaxed);
        c->runtime_read_samples.store(0, std::memory_order_relaxed);
      }
    }
    return 0;
  } else if (command == "bluestore static frag score") {
    std::shared_lock l(store.coll_lock);
    std::string coll;
    cmd_getval(cmdmap, "collection", coll);
    f->open_array_section("static_frag_score");
    for (const auto& it : store.coll_map) {
      auto c = it.second;
      std::shared_lock l(c->lock);
      if (coll.empty() || coll == c->get_cid().c_str()) {
        f->open_object_section("collection");
        f->dump_string("cid", c->get_cid().c_str());
        auto score = c->static_frag_score.load(std::memory_order_relaxed);
        auto count = c->object_read_samples.load(std::memory_order_relaxed);
        f->dump_unsigned("static_frag_score", score);
        f->dump_unsigned("object_read_samples", count);
        f->close_section();
      }
    }
    f->close_section();
    return 0;
  } else if (command == "bluestore cache stats") {
    f->open_object_section("cache_stats");

    BlueStore::CacheStatsSnapshot current, most_recent, oldest;
    store.get_snapshot_windows(current, most_recent, oldest);
    dump_snapshot_section(f, "since_startup", current);

    double recent_duration = std::chrono::duration<double>(
      current.timestamp - most_recent.timestamp).count();
    if (recent_duration > 0) {
      dump_snapshot_section(f, "since_most_recent_snapshot",
                            current.delta(most_recent), recent_duration);
    }

    double oldest_duration = std::chrono::duration<double>(
      current.timestamp - oldest.timestamp).count();
    if (oldest_duration > 0) {
      dump_snapshot_section(f, "since_oldest_snapshot",
                            current.delta(oldest), oldest_duration);
    }

    // RocksDB performance statistics (I think the hits and misses is useful, latency is not, commented out for now)
    //f->open_object_section("rocksdb_perf_stats");
    
    //auto collection = store.cct->get_perfcounters_collection();
    
    //if (collection) {
    //  collection->dump_formatted(f, false, static_cast<select_labeled_t>(0), "rocksdb", "");
    //} else {
    //  f->dump_string("status", "perf counters collection not available");
    //}
    
    //f->close_section(); // rocksdb_perf_stats
    
    f->close_section(); // cache_stats
    
    return 0;
  } else if (command == "bluestore clear static frag") {
    std::shared_lock l(store.coll_lock);
    std::string coll;
    cmd_getval(cmdmap, "collection", coll);
    for (const auto& it : store.coll_map) {
      auto c = it.second;
      std::shared_lock l(c->lock);
      if (coll.empty() || coll == c->get_cid().c_str()) {
        c->static_frag_score.store(0, std::memory_order_relaxed);
        c->object_read_samples.store(0, std::memory_order_relaxed);
      }
    }
    return 0;
  } else if (command == "bluestore bluefs-bdev-expand"){
    std::stringstream result;
    int ret = store.expand_devices(result);
    if (ret < 0) {
      ss << "expand device failed: " << cpp_strerror(ret) << std::endl;
    } else {
      out.append(result.str());
    }
    return ret;
  } else if (command == "bluestore dedup stats") {
    int64_t pool = -1;
    cmd_getval(cmdmap, "pool", pool);
    if (pool < 0) {
      ss << "Invalid pool id:" << pool << std::endl;
      r = -EINVAL;
    } else {
      uint64_t entries = 0;
      auto sz = store.estimate_dedup_info(pool, &entries);
      if (sz >= 0) {
        f->open_object_section("deduplication stats");
        f->dump_unsigned("pool", pool);
        f->dump_unsigned("bytes", (uint64_t)sz);
        f->dump_unsigned("entries", entries);
        f->close_section();
      } else {
        ss << "Execution error" << std::endl;
        r = -EIO;
      }
    }
    return r;
  } else if (command == "bluestore dedup list") {
    int64_t pool = -1;
    int64_t shard = 0;
    int64_t shard_bits = 0;
    std::string after;
    int64_t max = 1000;
    int64_t newer_than = 0;       // seconds
    int64_t prune_older_than = 0; // seconds
    double max_duration = 1.0;
    cmd_getval(cmdmap, "pool", pool);
    cmd_getval(cmdmap, "shard", shard);
    cmd_getval(cmdmap, "shard_bits", shard_bits);
    cmd_getval(cmdmap, "after", after);
    cmd_getval(cmdmap, "max", max);
    cmd_getval(cmdmap, "newer_than", newer_than);
    cmd_getval(cmdmap, "prune_older_than", prune_older_than);
    cmd_getval(cmdmap, "max_duration", max_duration);

    std::string next;
    if (pool < 0) {
      ss << "Invalid pool id:" << pool << std::endl;
      return -EINVAL;
    }
    if (shard_bits < 0 || shard_bits > 32 ||
        shard < 0 || (uint64_t)shard >= (1ull << shard_bits)) {
      ss << "Invalid shard " << shard << " for shard_bits " << shard_bits
         << std::endl;
      return -EINVAL;
    }
    if (max <= 0 || max > std::numeric_limits<int>::max()) {
      ss << "Invalid max:" << max << std::endl;
      return -EINVAL;
    }
    if (!hex2bin_str(after, &next)) {
      ss << "Invalid cursor:" << after << std::endl;
      return -EINVAL;
    }
    auto now = ceph::real_clock::now();
    auto time_ago = [&](int64_t secs) {
      auto span = std::chrono::seconds(secs);
      return now.time_since_epoch() > span ?
        now - span : ceph::real_clock::time_point();
    };
    // take everything when no restriction is provided
    auto retrieve_after = newer_than > 0 ?
      time_ago(newer_than) : ceph::real_clock::time_point();
    // remove nothing when no restriction is provided
    auto remove_before = prune_older_than > 0 ?
      time_ago(prune_older_than) : ceph::real_clock::time_point();

    std::vector<dedup_candidate_t> ls;
    bool completed = false;
    r = store.dedup_candidate_list(
      pool,
      shard,
      shard_bits,
      CEPH_CRYPTO_MD5_DIGESTSIZE * 8,
      retrieve_after,
      remove_before,
      max,
      0, // no deletion count restriction
      max_duration,
      ls,
      &next,
      &completed);
    if (r == -EINTR) {
      // interrupted due to time restriction, more to retrieve
      r = 0;
    } else if (r < 0) {
      ss << "Execution error:" << cpp_strerror(r) << std::endl;
      return r;
    } else {
      r = 0;
    }
    f->open_object_section("deduplication list");
    f->dump_unsigned("pool", pool);
    f->dump_unsigned("shard", shard);
    f->dump_unsigned("shard_bits", shard_bits);
    f->dump_bool("completed", completed);
    f->dump_string("next", bin2hex_str(next));
    f->open_array_section("entries");
    for (auto& e : ls) {
      f->open_object_section("entry");
      f->dump_string("digest", bin2hex_str(e.digest));
      f->dump_unsigned("hash", hobject_t::_reverse_bits(e.oid_hash));
      f->dump_unsigned("timestamp",
        std::chrono::duration_cast<std::chrono::nanoseconds>(
          e.ts.time_since_epoch()).count());
      f->dump_float("age",
        std::chrono::duration<double>(now - e.ts).count());
      f->close_section();
    }
    f->close_section();
    f->close_section();
    return r;
  } else if (command == "bluestore dedup clear") {
    int64_t pool = -1;
    cmd_getval(cmdmap, "pool", pool);
    if (pool < 0) {
      ss << "Invalid pool id:" << pool << std::endl;
      r = -EINVAL;
    } else {
      auto err = store.clear_dedup_info(pool, true);
      if (err < 0) {
        ss << "Execution error:" << cpp_strerror(err) << std::endl;
        r = -EIO;
      }
    }
    return r;
  } else {
    ss << "Invalid command" << std::endl;
    r = -ENOSYS;
  }
  return r;
}
