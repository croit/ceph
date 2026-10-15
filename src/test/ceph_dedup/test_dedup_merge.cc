// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

/*
 * Benchmark for dedup info merging as performed by ceph-dedup-tool's
 * search-dups op, with no OSD communication involved.
 *
 * Every generator simulates a single OSD: for each digest shard it provides
 * dedup items sorted by digest (as an OSD does) in fixed size batches.
 * Digests are taken from a grid shared by all the generators, hence some
 * of them are reported by multiple generators, i.e. duplicates exist.
 *
 * Similar to ceph-dedup-tool every thread processes its own subset of
 * digest shards. Every shard is processed by taking the next batch from
 * up to 4 not completed generators, chosen in a round-robin manner or by
 * the k-way merger (see CEPH_DEDUP_BENCH_MERGERS), until all the
 * generators are exhausted. Threads loop over their shards until the
 * benchmark time expires.
 *
 * Tunables (environment variables):
 *  CEPH_DEDUP_BENCH_SECONDS     - time per thread count iteration, def. 60
 *  CEPH_DEDUP_BENCH_THREADS     - comma separated thread counts, def. 1,2,4
 *  CEPH_DEDUP_BENCH_GENERATORS  - generators (OSDs), def. 1024
 *  CEPH_DEDUP_BENCH_BATCH       - items per batch, def. 1000
 *  CEPH_DEDUP_BENCH_BATCHES     - batches per generator per shard, def. 1
 *  CEPH_DEDUP_BENCH_SHARDS      - digest shards (power of 2), def. 16
 *  CEPH_DEDUP_BENCH_GRID_FACTOR - digest grid size relative to items per
 *                                 shard, controls duplicates rate, def. 4
 *  CEPH_DEDUP_BENCH_MERGERS     - comma separated mergers to benchmark:
 *                                 map (DedupShardMerger), kway
 *                                 (DedupKWayMerger fetching round-robin),
 *                                 kway_lf (DedupKWayMerger fetching from
 *                                 the lowest frontier OSDs first, as
 *                                 ceph-dedup-tool does),
 *                                 def. map,kway,kway_lf
 *
 * Besides throughput every iteration reports CPU consumption (user + system,
 * via getrusage) and resident memory (sampled every 100 ms): the baseline
 * at iteration start, the average and the peak. 'held' is the max number
 * of records held by a merger at once, summed over threads, and B/item is
 * the peak memory growth over the baseline per such record.
 */

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <thread>
#include <vector>

#include <malloc.h>
#include <sys/resource.h>
#include <unistd.h>

#include "gtest/gtest.h"

#include "tools/ceph_dedup/dedup_merge.h"

using librados::DedupItem;
using bench_clock = std::chrono::steady_clock;

namespace {

uint64_t env_u64(const char* name, uint64_t def)
{
  const char* v = getenv(name);
  return v && *v ? strtoull(v, nullptr, 10) : def;
}

std::vector<unsigned> env_threads()
{
  std::vector<unsigned> res;
  const char* v = getenv("CEPH_DEDUP_BENCH_THREADS");
  std::stringstream ss(v && *v ? v : "1,2,4");
  std::string t;
  while (std::getline(ss, t, ',')) {
    if (unsigned n = strtoul(t.c_str(), nullptr, 10); n) {
      res.push_back(n);
    }
  }
  return res;
}

std::vector<std::string> env_mergers()
{
  std::vector<std::string> res;
  const char* v = getenv("CEPH_DEDUP_BENCH_MERGERS");
  std::stringstream ss(v && *v ? v : "map,kway,kway_lf");
  std::string t;
  while (std::getline(ss, t, ',')) {
    if (t == "map" || t == "kway" || t == "kway_lf") {
      res.push_back(t);
    }
  }
  return res;
}

struct bench_params_t {
  double seconds = env_u64("CEPH_DEDUP_BENCH_SECONDS", 60);
  unsigned generators = env_u64("CEPH_DEDUP_BENCH_GENERATORS", 1024);
  unsigned batch = env_u64("CEPH_DEDUP_BENCH_BATCH", 1000);
  unsigned batches = env_u64("CEPH_DEDUP_BENCH_BATCHES", 1);
  unsigned shards = env_u64("CEPH_DEDUP_BENCH_SHARDS", 16);
  unsigned grid_factor = env_u64("CEPH_DEDUP_BENCH_GRID_FACTOR", 4);

  unsigned shard_bits() const {
    unsigned b = 0;
    while ((1u << b) < shards) {
      ++b;
    }
    return b;
  }
  uint64_t items_per_generator() const {
    return uint64_t(batch) * batches;
  }
  uint64_t grid_size() const {
    return items_per_generator() * generators * grid_factor;
  }
};

uint64_t mix64(uint64_t x)
{
  // splitmix64 finalizer
  x += 0x9e3779b97f4a7c15ull;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ull;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebull;
  return x ^ (x >> 31);
}

void put_be64(char* p, uint64_t v)
{
  for (int i = 7; i >= 0; --i) {
    p[i] = char(v & 0xff);
    v >>= 8;
  }
}

// Provides a single shard's dedup items of a single simulated OSD
// sorted by digest.
class Generator {
  const bench_params_t& p;
  int osd;
  unsigned shard = 0;
  uint64_t rng = 0;
  uint64_t pos = 0;       // current digest grid position
  uint64_t produced = 0;
  uint64_t max_step = 1;  // average step is half of it

  uint64_t next_rand() {
    rng = mix64(rng);
    return rng;
  }

public:
  Generator(const bench_params_t& _p, int _osd) : p(_p), osd(_osd) {}

  int get_osd() const {
    return osd;
  }
  void reset(unsigned _shard, uint64_t round) {
    shard = _shard;
    rng = mix64((uint64_t(osd) << 32) ^ (uint64_t(shard) << 16) ^ round);
    pos = 0;
    produced = 0;
    max_step = std::max<uint64_t>(
      2, 2 * p.grid_size() / p.items_per_generator());
  }
  bool done() const {
    return produced >= p.items_per_generator();
  }

  // append the next batch to 'out'
  void next_batch(std::vector<DedupItem>* out) {
    unsigned bits = p.shard_bits();
    uint64_t shard_prefix = bits ? uint64_t(shard) << (64 - bits) : 0;
    uint64_t pos_mask = bits ? (~0ull >> bits) : ~0ull;
    for (unsigned i = 0; i < p.batch && !done(); ++i, ++produced) {
      pos += 1 + next_rand() % max_step;
      // the same grid position results in the same digest for all
      // the generators
      char d[16];
      put_be64(d, shard_prefix | (pos & pos_mask));
      put_be64(d + 8, mix64(pos));
      DedupItem& item = out->emplace_back();
      item.digest.assign(d, sizeof(d));
      item.hash = uint32_t(next_rand());
      item.timestamp = produced;
      item.osd = osd;
    }
  }
};

struct thread_stats_t {
  uint64_t items = 0;
  uint64_t shards = 0;
  uint64_t digests = 0;
  uint64_t dup_groups = 0;
  uint64_t dup_objects = 0;
  uint64_t max_held = 0; // max records held by the merger at once
  bench_clock::duration merge_time{};
};

constexpr size_t MAX_TARGETS = 4; // OSDs queried per round by the tool

// Take the next batch from up to MAX_TARGETS not completed generators,
// round-robin, and pass each one to 'consume(generator, batch)'.
// Returns false if all the generators are completed.
template <typename C>
bool fetch_round(std::vector<Generator>& gens, size_t& next_target,
                 size_t& completed, C&& consume)
{
  if (completed >= gens.size()) {
    return false;
  }
  size_t submitted = 0;
  for (size_t i = 0; i < gens.size() && submitted < MAX_TARGETS; ++i) {
    auto& g = gens[next_target];
    next_target = (next_target + 1) % gens.size();
    if (!g.done()) {
      std::vector<DedupItem> batch;
      g.next_batch(&batch);
      ++submitted;
      completed += g.done();
      consume(g, std::move(batch));
    }
  }
  return true;
}

// Mimics SearchForDuplicates::process_shard()
void process_shard_map(const bench_params_t& p, std::vector<Generator>& gens,
                       unsigned shard, uint64_t round,
                       const bench_clock::time_point& deadline,
                       thread_stats_t& st)
{
  for (auto& g : gens) {
    g.reset(shard, round);
  }
  DedupShardMerger merger;
  size_t next_target = 0;
  size_t completed = 0;
  uint64_t held = 0;
  std::vector<DedupItem> result;
  while (true) {
    if (bench_clock::now() >= deadline) {
      return; // partial shard, merged items are accounted already
    }
    // librados concatenates batches of all the targets
    result.clear();
    if (!fetch_round(gens, next_target, completed,
                     [&](Generator&, std::vector<DedupItem>&& batch) {
                       result.insert(result.end(),
                                     std::make_move_iterator(batch.begin()),
                                     std::make_move_iterator(batch.end()));
                     })) {
      break;
    }
    auto t0 = bench_clock::now();
    for (auto& i : result) {
      merger.add(i);
    }
    st.merge_time += bench_clock::now() - t0;
    st.items += result.size();
    held += result.size();
    st.max_held = std::max(st.max_held, held);
  }
  auto t0 = bench_clock::now();
  merger.for_each_duplicate(
    [&st](const std::string&, const DedupShardMerger::objects_t& objs) {
      ++st.dup_groups;
      st.dup_objects += objs.size() - 1;
    });
  st.merge_time += bench_clock::now() - t0;
  st.digests += merger.digest_count();
  ++st.shards;
}

void process_shard_kway(const bench_params_t& p, std::vector<Generator>& gens,
                        unsigned shard, uint64_t round,
                        const bench_clock::time_point& deadline,
                        thread_stats_t& st, bool lowest_first)
{
  std::vector<int> osds;
  osds.reserve(gens.size());
  for (auto& g : gens) {
    g.reset(shard, round);
    osds.push_back(g.get_osd());
  }
  DedupKWayMerger merger(osds);
  size_t next_target = 0;
  size_t completed = 0;
  // accounted on shard completion only, as digests are
  uint64_t dup_groups = 0;
  uint64_t dup_objects = 0;
  auto on_dup = [&](const std::string&,
                    const DedupKWayMerger::objects_t& objs) {
    ++dup_groups;
    dup_objects += objs.size() - 1;
  };
  while (true) {
    if (bench_clock::now() >= deadline) {
      st.max_held = std::max<uint64_t>(st.max_held, merger.get_max_buffered());
      return; // partial shard, merged items are accounted already
    }
    bench_clock::duration t{};
    auto consume = [&](Generator& g, std::vector<DedupItem>&& batch) {
      st.items += batch.size();
      auto t0 = bench_clock::now();
      merger.add_batch(g.get_osd(), std::move(batch), g.done());
      t += bench_clock::now() - t0;
    };
    if (lowest_first) {
      // fetch from the OSDs holding back merging (generator == OSD index)
      auto t0 = bench_clock::now();
      auto targets = merger.next_targets(MAX_TARGETS);
      t += bench_clock::now() - t0;
      if (targets.empty()) {
        break;
      }
      for (int osd : targets) {
        std::vector<DedupItem> batch;
        gens[osd].next_batch(&batch);
        consume(gens[osd], std::move(batch));
      }
    } else if (!fetch_round(gens, next_target, completed, consume)) {
      break;
    }
    auto t0 = bench_clock::now();
    merger.emit(on_dup);
    st.merge_time += t + (bench_clock::now() - t0);
  }
  if (!merger.done()) {
    ADD_FAILURE() << "k-way merge incomplete for shard " << shard;
  }
  st.max_held = std::max<uint64_t>(st.max_held, merger.get_max_buffered());
  st.digests += merger.digest_count();
  st.dup_groups += dup_groups;
  st.dup_objects += dup_objects;
  ++st.shards;
}

uint64_t rss_bytes()
{
  std::ifstream f("/proc/self/statm");
  uint64_t size = 0, resident = 0;
  f >> size >> resident;
  return resident * sysconf(_SC_PAGESIZE);
}

double cpu_seconds()
{
  struct rusage ru;
  getrusage(RUSAGE_SELF, &ru);
  auto tv = [](const timeval& t) { return t.tv_sec + t.tv_usec / 1e6; };
  return tv(ru.ru_utime) + tv(ru.ru_stime);
}

// Samples process' resident memory periodically
class RssMonitor {
  std::atomic<bool> stop{false};
  std::thread sampler;
  uint64_t peak = 0;
  uint64_t sum = 0;
  uint64_t samples = 0;

public:
  RssMonitor() {
    sampler = std::thread([this] {
      while (!stop) {
        uint64_t r = rss_bytes();
        peak = std::max(peak, r);
        sum += r;
        ++samples;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
    });
  }
  // stops sampling, returns <average, peak>
  std::pair<uint64_t, uint64_t> finish() {
    stop = true;
    sampler.join();
    return {samples ? sum / samples : 0, peak};
  }
};

// digest -> object hashes of duplicate groups
using groups_t = std::map<std::string, std::set<uint32_t>>;

DedupItem make_item(char d, uint32_t hash, int osd)
{
  DedupItem i;
  i.digest = std::string(16, d);
  i.hash = hash;
  i.osd = osd;
  return i;
}

} // anonymous namespace

TEST(DedupMerge, KWayHandcrafted)
{
  groups_t groups;
  auto on_dup = [&groups](const std::string& d,
                          const DedupKWayMerger::objects_t& objs) {
    for (auto& [hash, osd] : objs) {
      groups[d].insert(hash);
    }
  };
  DedupKWayMerger m({1, 2, 3});

  // nothing is emitted until every OSD delivered something
  m.add_batch(1, {make_item(1, 11, 1), make_item(1, 12, 1)}, false);
  EXPECT_EQ(0u, m.emit(on_dup));
  // the same object (11) reported by another OSD as well
  m.add_batch(2, {make_item(1, 11, 2), make_item(2, 24, 2)}, true);
  EXPECT_EQ(0u, m.emit(on_dup));
  m.add_batch(3, {make_item(2, 35, 3)}, false);
  // OSD 1 might still deliver more records with digest 1
  EXPECT_EQ(0u, m.emit(on_dup));
  m.add_batch(1, {make_item(3, 13, 1)}, true);
  // digest 1 is complete now, digest 2 still might come from OSD 3
  EXPECT_EQ(1u, m.emit(on_dup));
  EXPECT_EQ(groups_t({{std::string(16, 1), {11, 12}}}), groups);
  m.add_batch(3, {make_item(3, 36, 3)}, true);
  EXPECT_EQ(2u, m.emit(on_dup));
  EXPECT_TRUE(m.done());
  EXPECT_EQ(groups_t({{std::string(16, 1), {11, 12}},
                      {std::string(16, 2), {24, 35}},
                      {std::string(16, 3), {13, 36}}}),
            groups);
  EXPECT_EQ(3u, m.digest_count());
}

TEST(DedupMerge, KWayExplicitFrontier)
{
  groups_t groups;
  auto on_dup = [&groups](const std::string& d,
                          const DedupKWayMerger::objects_t& objs) {
    for (auto& [hash, osd] : objs) {
      groups[d].insert(hash);
    }
  };
  DedupKWayMerger m({1, 2, 3});
  m.add_batch(1, {make_item(1, 11, 1)}, false);
  m.add_batch(2, {make_item(1, 21, 2)}, true);
  // OSD 3 delivered records up to digest 5, all filtered out by the caller
  m.add_batch(3, {}, false, std::string(16, 5));
  // OSD 1 still might deliver digest 1
  EXPECT_EQ(0u, m.emit(on_dup));
  m.add_batch(1, {make_item(2, 12, 1)}, false);
  // digest 1 is complete as no OSD can deliver it anymore
  EXPECT_EQ(1u, m.emit(on_dup));
  EXPECT_EQ(groups_t({{std::string(16, 1), {11, 21}}}), groups);
  m.add_batch(1, {}, true);
  m.add_batch(3, {}, true);
  EXPECT_EQ(0u, m.emit(on_dup));
  EXPECT_TRUE(m.done());
}

TEST(DedupMerge, KWayNextTargets)
{
  auto noop = [](const std::string&, const DedupKWayMerger::objects_t&) {};
  DedupKWayMerger m({1, 2, 3});
  // nothing delivered yet, all are equally behind
  EXPECT_EQ(std::vector<int>({1, 2, 3}), m.next_targets(5));
  EXPECT_EQ(std::vector<int>({1, 2}), m.next_targets(2));
  m.add_batch(1, {make_item(5, 11, 1)}, false);
  m.add_batch(2, {make_item(2, 21, 2)}, false);
  // OSD 3 hasn't delivered anything, OSD 2 is behind OSD 1
  EXPECT_EQ(std::vector<int>({3, 2, 1}), m.next_targets(5));
  m.add_batch(3, {make_item(4, 31, 3)}, false);
  EXPECT_EQ(std::vector<int>({2, 3}), m.next_targets(2));
  m.emit(noop);
  // completed OSDs aren't suggested
  m.add_batch(2, {}, true);
  EXPECT_EQ(std::vector<int>({3, 1}), m.next_targets(5));
  m.add_batch(3, {}, true);
  m.add_batch(1, {}, true);
  EXPECT_TRUE(m.next_targets(5).empty());
  m.emit(noop);
  EXPECT_TRUE(m.done());
}

TEST(DedupMerge, KWayMatchesMap)
{
  bench_params_t p;
  p.generators = 64;
  p.batch = 50;
  p.batches = 5;
  p.shards = 4;
  p.grid_factor = 1; // lots of duplicates
  std::vector<Generator> gens;
  std::vector<int> osds;
  for (unsigned g = 0; g < p.generators; ++g) {
    gens.emplace_back(p, g);
    osds.push_back(g);
  }
  for (unsigned shard = 0; shard < p.shards; ++shard) {
    // all the batches of every generator
    std::vector<std::vector<std::vector<DedupItem>>> batches(gens.size());
    for (auto& g : gens) {
      g.reset(shard, 0);
      while (!g.done()) {
        auto& b = batches[g.get_osd()].emplace_back();
        g.next_batch(&b);
        for (auto& i : b) {
          // make some objects reported by multiple OSDs
          if (i.hash % 3 == 0) {
            i.hash = std::hash<std::string>()(i.digest);
          }
        }
      }
    }
    DedupShardMerger map_merger;
    for (auto& gb : batches) {
      for (auto& b : gb) {
        for (auto& i : b) {
          map_merger.add(i);
        }
      }
    }
    groups_t expected;
    map_merger.for_each_duplicate(
      [&expected](const std::string& d,
                  const DedupShardMerger::objects_t& objs) {
        for (auto& [hash, osd] : objs) {
          expected[d].insert(hash);
        }
      });
    ASSERT_FALSE(expected.empty());

    // feed k-way merger batch by batch, OSDs in a varying order,
    // then fetching from the OSDs suggested by the merger
    groups_t actual_lf;
    {
      DedupKWayMerger kway(osds);
      std::vector<size_t> next(gens.size(), 0);
      auto on_dup = [&actual_lf](const std::string& d,
                                 const DedupKWayMerger::objects_t& objs) {
        for (auto& [hash, o] : objs) {
          actual_lf[d].insert(hash);
        }
      };
      for (auto targets = kway.next_targets(4); !targets.empty();
           targets = kway.next_targets(4)) {
        for (int osd : targets) {
          bool last = next[osd] + 1 == batches[osd].size();
          // keep batches intact for the next pass
          auto b = batches[osd][next[osd]++];
          kway.add_batch(osd, std::move(b), last);
        }
        kway.emit(on_dup);
      }
      kway.emit(on_dup);
      EXPECT_TRUE(kway.done());
    }
    EXPECT_EQ(expected, actual_lf) << "lowest first, shard " << shard;

    groups_t actual;
    DedupKWayMerger kway(osds);
    std::vector<size_t> next(gens.size(), 0);
    size_t remaining = 0;
    for (auto& gb : batches) {
      remaining += gb.size();
    }
    for (size_t step = 0; remaining; ++step) {
      size_t osd = (step * 7 + step / gens.size()) % gens.size();
      if (next[osd] == batches[osd].size()) {
        continue;
      }
      bool last = next[osd] + 1 == batches[osd].size();
      kway.add_batch(osd, std::move(batches[osd][next[osd]++]), last);
      --remaining;
      kway.emit([&actual](const std::string& d,
                          const DedupKWayMerger::objects_t& objs) {
        for (auto& [hash, o] : objs) {
          actual[d].insert(hash);
        }
      });
    }
    EXPECT_TRUE(kway.done());
    EXPECT_EQ(map_merger.digest_count(), kway.digest_count());
    EXPECT_EQ(expected, actual) << "shard " << shard;
  }
}

TEST(DedupMerge, Benchmark)
{
  bench_params_t p;
  auto thread_counts = env_threads();
  auto mergers = env_mergers();
  ASSERT_FALSE(thread_counts.empty());
  ASSERT_FALSE(mergers.empty());
  ASSERT_GT(p.generators, 0u);
  ASSERT_GT(p.batch, 0u);
  ASSERT_EQ(p.shards & (p.shards - 1), 0u) << "shards must be a power of 2";

  std::cout << "generators " << p.generators
            << ", batch " << p.batch
            << ", batches/generator/shard " << p.batches
            << ", shards " << p.shards
            << ", items/shard " << p.items_per_generator() * p.generators
            << ", grid factor " << p.grid_factor
            << ", " << p.seconds << "s per iteration" << std::endl;
  std::cout << std::setw(6) << "merger"
            << std::setw(8) << "threads"
            << std::setw(12) << "items"
            << std::setw(8) << "shards"
            << std::setw(12) << "items/s"
            << std::setw(14) << "merge items/s"
            << std::setw(8) << "dup %"
            << std::setw(8) << "cores"
            << std::setw(12) << "cpu s/Mitem"
            << std::setw(11) << "RSS base"
            << std::setw(10) << "RSS avg"
            << std::setw(11) << "RSS peak"
            << std::setw(10) << "held"
            << std::setw(8) << "B/item"
            << std::endl;
  auto mb = [](uint64_t b) {
    std::ostringstream os;
    os << b / (1024 * 1024) << "M";
    return os.str();
  };

  for (auto& merger : mergers) {
  auto process_shard =
    [&merger](const bench_params_t& p, std::vector<Generator>& gens,
              unsigned shard, uint64_t round,
              const bench_clock::time_point& deadline, thread_stats_t& st) {
      if (merger == "map") {
        process_shard_map(p, gens, shard, round, deadline, st);
      } else {
        process_shard_kway(p, gens, shard, round, deadline, st,
                           merger == "kway_lf");
      }
    };
  for (unsigned threads : thread_counts) {
    // return memory freed by the previous iteration to the OS
    malloc_trim(0);
    uint64_t rss_base = rss_bytes();
    double cpu_start = cpu_seconds();
    RssMonitor rss_mon;

    std::vector<thread_stats_t> stats(threads);
    auto start = bench_clock::now();
    auto deadline = start + std::chrono::duration_cast<bench_clock::duration>(
      std::chrono::duration<double>(p.seconds));
    std::vector<std::thread> workers;
    for (unsigned t = 0; t < threads; ++t) {
      workers.emplace_back([&, t] {
        std::vector<Generator> gens;
        gens.reserve(p.generators);
        for (unsigned g = 0; g < p.generators; ++g) {
          gens.emplace_back(p, g);
        }
        // thread t handles shards t, t + threads, ...
        for (uint64_t round = 0; bench_clock::now() < deadline; ++round) {
          for (unsigned shard = t; shard < p.shards &&
                 bench_clock::now() < deadline; shard += threads) {
            process_shard(p, gens, shard, round, deadline, stats[t]);
          }
          if (t >= p.shards) {
            break; // no shards for this thread
          }
        }
      });
    }
    for (auto& w : workers) {
      w.join();
    }
    double elapsed =
      std::chrono::duration<double>(bench_clock::now() - start).count();
    double cpu = cpu_seconds() - cpu_start;
    auto [rss_avg, rss_peak] = rss_mon.finish();

    thread_stats_t total;
    double merge_rate = 0.0; // sum of per thread merge-only rates
    for (auto& s : stats) {
      total.max_held += s.max_held;
      total.items += s.items;
      total.shards += s.shards;
      total.digests += s.digests;
      total.dup_groups += s.dup_groups;
      total.dup_objects += s.dup_objects;
      double mt = std::chrono::duration<double>(s.merge_time).count();
      if (mt > 0.0) {
        merge_rate += s.items / mt;
      }
    }
    std::cout << std::setw(6) << merger
              << std::setw(8) << threads
              << std::setw(12) << total.items
              << std::setw(8) << total.shards
              << std::setw(12) << uint64_t(total.items / elapsed)
              << std::setw(14) << uint64_t(merge_rate)
              << std::setw(7) << std::fixed << std::setprecision(2)
              << (total.digests ? 100.0 * total.dup_groups / total.digests : 0.0)
              << "%"
              << std::setw(8) << cpu / elapsed
              << std::setw(12)
              << (total.items ? cpu / (total.items / 1e6) : 0.0)
              << std::defaultfloat
              << std::setw(11) << mb(rss_base)
              << std::setw(10) << mb(rss_avg)
              << std::setw(11) << mb(rss_peak)
              << std::setw(10) << total.max_held
              << std::setw(8)
              << (rss_peak > rss_base && total.max_held ?
                    (rss_peak - rss_base) / total.max_held : 0)
              << std::endl;

    EXPECT_GT(total.items, 0u);
    if (total.shards) {
      EXPECT_GT(total.dup_groups, 0u);
    }
  }
  }
}
