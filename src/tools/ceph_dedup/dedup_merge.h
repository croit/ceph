// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_DEDUP_MERGE_H
#define CEPH_DEDUP_MERGE_H

#include <algorithm>
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "include/rados/librados.hpp"

/**
 * Groups deduplication info records of a single digest shard by digest.
 *
 * Records might come from multiple OSDs in any order. The same object
 * might be reported multiple times, e.g. by different OSDs after primary
 * change or after repeated deep scrubs, hence objects are deduplicated
 * within a group. Objects are identified by their hashes for now.
 */
class DedupShardMerger {
public:
  /// object hash -> OSD keeping the record
  using objects_t = std::map<uint32_t, int>;

private:
  std::map<std::string, objects_t> by_digest;

public:
  void add(const librados::DedupItem& i) {
    by_digest[i.digest].emplace(i.hash, i.osd);
  }

  /// call f(digest, objects) for every group having more than one object
  template <typename F>
  void for_each_duplicate(F&& f) const {
    for (auto& [digest, objs] : by_digest) {
      if (objs.size() > 1) {
        f(digest, objs);
      }
    }
  }

  size_t digest_count() const {
    return by_digest.size();
  }
  void clear() {
    by_digest.clear();
  }
};

/**
 * Streaming k-way merge of deduplication info records of a single digest
 * shard.
 *
 * Every OSD (source) provides its records sorted by digest, in batches.
 * Records are merged in digest order and duplicate groups are emitted as
 * soon as no more records with the same digest might arrive, i.e. once the
 * digest is below the last digest delivered by every not yet completed
 * source. Hence only records ahead of the slowest source are kept in
 * memory rather than the whole shard.
 */
class DedupKWayMerger {
public:
  using objects_t = DedupShardMerger::objects_t;

private:
  struct source_t {
    std::vector<librados::DedupItem> buf; // pending records
    size_t pos = 0;                       // first pending record in buf
    bool completed = false;
    // last digest delivered, valid while not completed
    std::multiset<std::string>::iterator frontier;
  };
  std::vector<source_t> sources;
  std::unordered_map<int, size_t> osd_to_source;
  // last digests delivered by not completed sources, empty string for ones
  // which haven't delivered anything yet
  std::multiset<std::string> frontiers;
  // min-heap of sources having pending records, by their head digest
  std::vector<size_t> heap;

  // the group being accumulated: <object hash, osd>
  std::string group_digest;
  std::vector<std::pair<uint32_t, int>> group;

  size_t buffered = 0;
  size_t max_buffered = 0;
  size_t digests = 0;

  const std::string& head(size_t s) const {
    return sources[s].buf[sources[s].pos].digest;
  }
  bool heap_cmp(size_t a, size_t b) const {
    return head(a) > head(b); // makes std heap functions a min-heap
  }
  void heap_push(size_t s) {
    heap.push_back(s);
    std::push_heap(heap.begin(), heap.end(),
                   [this](size_t a, size_t b) { return heap_cmp(a, b); });
  }
  void heap_pop() {
    std::pop_heap(heap.begin(), heap.end(),
                  [this](size_t a, size_t b) { return heap_cmp(a, b); });
    heap.pop_back();
  }

  template <typename F>
  size_t flush_group(F& f) {
    size_t res = 0;
    digests += !group.empty();
    if (group.size() > 1) {
      std::sort(group.begin(), group.end());
      auto e = std::unique(group.begin(), group.end(),
                           [](auto& a, auto& b) { return a.first == b.first; });
      if (e - group.begin() > 1) {
        objects_t objs(group.begin(), e);
        f(group_digest, objs);
        res = 1;
      }
    }
    group.clear();
    return res;
  }

public:
  explicit DedupKWayMerger(const std::vector<int>& osds) {
    sources.resize(osds.size());
    for (size_t i = 0; i < osds.size(); ++i) {
      osd_to_source[osds[i]] = i;
      sources[i].frontier = frontiers.insert(std::string());
    }
  }

  /// Add the next batch of records from the OSD, sorted by digest.
  /// 'completed' indicates the OSD has no more records.
  /// 'last_digest', if not empty, is the last digest delivered by the OSD,
  /// for the case when some records (or all) have been filtered out of
  /// the batch by the caller, otherwise the last record's digest is used.
  void add_batch(int osd, std::vector<librados::DedupItem>&& items,
                 bool completed, const std::string& last_digest = {}) {
    auto& s = sources[osd_to_source.at(osd)];
    if (s.completed) {
      return;
    }
    const std::string* frontier = !last_digest.empty() ? &last_digest :
      !items.empty() ? &items.back().digest : nullptr;
    if (completed) {
      frontiers.erase(s.frontier);
      s.completed = true;
    } else if (frontier) {
      std::string f = *frontier; // might refer to items being moved below
      frontiers.erase(s.frontier);
      s.frontier = frontiers.insert(std::move(f));
    }
    if (items.empty()) {
      return;
    }
    buffered += items.size();
    max_buffered = std::max(max_buffered, buffered);
    bool was_idle = s.pos == s.buf.size();
    if (was_idle) {
      s.buf = std::move(items);
      s.pos = 0;
      heap_push(&s - sources.data());
    } else {
      s.buf.erase(s.buf.begin(), s.buf.begin() + s.pos);
      s.pos = 0;
      s.buf.insert(s.buf.end(), std::make_move_iterator(items.begin()),
                   std::make_move_iterator(items.end()));
    }
  }

  /// Merge whatever is safe to merge and call f(digest, objects) for every
  /// complete group having more than one object. Returns the number of
  /// such groups.
  template <typename F>
  size_t emit(F&& f) {
    size_t res = 0;
    while (!heap.empty()) {
      size_t si = heap.front();
      if (!frontiers.empty() && !(head(si) < *frontiers.begin())) {
        break; // more records with this digest might come
      }
      heap_pop();
      auto& s = sources[si];
      auto& item = s.buf[s.pos];
      if (!group.empty() && group_digest != item.digest) {
        res += flush_group(f);
      }
      if (group.empty()) {
        group_digest = item.digest;
      }
      group.emplace_back(item.hash, item.osd);
      ++s.pos;
      --buffered;
      if (s.pos < s.buf.size()) {
        heap_push(si);
      } else {
        s.buf = {};
        s.pos = 0;
      }
    }
    // Everything still pending or yet to come has a digest not below
    // the lowest frontier, hence the current group is complete.
    res += flush_group(f);
    return res;
  }

  /// all the sources completed and all the records merged
  bool done() const {
    return frontiers.empty() && heap.empty();
  }
  size_t get_max_buffered() const {
    return max_buffered;
  }
  /// number of distinct digests merged so far
  size_t digest_count() const {
    return digests;
  }
};

#endif
