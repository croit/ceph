// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

//#include <random>
#include "common/ceph_context.h"
#include "common/debug.h"
#include "common/ceph_crypto.h"
#include "common/pretty_binary.h"
#include "common/hobject.h"
#include "common/errno.h"

#include "include/intarith.h"
#include "include/ceph_assert.h"

#include "os/kv.h"

#include "kv/KeyValueDB.h"

#include "DedupStatsCollector.h"


const std::string PREFIX_DEDUP_CSUM = "D";  // pool + csum + ts + oid_hash -> (oid + some meta(?))

#define dout_context cct
#define dout_subsys ceph_subsys_bluestore
#undef dout_prefix
#define dout_prefix *_dout << "DedupStatsCollector::"

std::string bin2hex_str(const std::string& s)
{
  static constexpr char digits[] = "0123456789ABCDEF";

  std::string result(s.length() * 2, '\0');

  for (size_t i = 0; i < s.length(); ++i) {
    result[2 * i] = digits[(s[i] >> 4) & 0xf];
    result[2 * i + 1] = digits[s[i] & 0xf];
  }

  return result;
}

std::pair<std::string, std::string> range_from_msb(
  uint64_t digest_msb,
  size_t bits_to_use,
  size_t total_bits)
{
  std::string start, end;
  start.reserve(total_bits >> 3);
  end.reserve(total_bits >> 3);

  size_t shift_bits = p2nphase(bits_to_use, size_t(8));

  _key_encode_any_int(digest_msb << shift_bits, (bits_to_use + shift_bits) >> 3, &start);
  _key_encode_any_int((digest_msb << shift_bits) + (1 << shift_bits) - 1,
    (bits_to_use + shift_bits) >> 3, &end);

  start.append((total_bits - bits_to_use) >> 3, '\0');
  end.append((total_bits - bits_to_use) >> 3, '\xFF');
  return std::make_pair(start, end);
}

void DedupStatsCollector::get_dedup_pool_range(
  int64_t pool,
  std::string& range_start,
  std::string& range_end)
{
  range_start.reserve(sizeof(uint64_t) + 1);
  _key_encode_u64(pool + 0x8000000000000000ull, &range_start);
  range_start += ':';
  range_end = range_start;
  range_end.back() = '\xFF';
}

std::string DedupStatsCollector::get_dedup_entry_key(
  int64_t pool,
  const ghobject_t& oid, const std::string& digest,
  const mono_clock::time_point& ts)
{
  std::string key;
  key.reserve(128); //FIXME: fix max len

  _key_encode_u64(pool + 0x8000000000000000ull, &key);
  key += ':';
  key += digest; //bin2hex_str(digest); //FIXME: use bin value
  key += ':';
  _key_encode_u64(std::chrono::duration_cast<std::chrono::nanoseconds>(ts.time_since_epoch()).count(), &key);
  key += ':';
  _key_encode_u32(oid.hobj.get_bitwise_key_u32(), &key);

  return key;
}

void DedupStatsCollector::get_key_dedup_entry(const char* p, int64_t& pool,
  std::string& digest, size_t digest_len,
  ceph::mono_clock::time_point& ts,
  uint32_t oid_hash)
{
  p = _key_decode_u64(p, (uint64_t*)&pool);
  pool -= 0x8000000000000000ull;
  ++p; //':' delimiter

  digest = std::string(p, digest_len);
  p += digest_len + 1; // +1 for ':' delimiter

  uint64_t ns;
  p = _key_decode_u64(p, &ns) + 1; //+1 for ':' delimiter
  ts = ceph::mono_clock::time_point(std::chrono::nanoseconds(ns));

  uint32_t v;
  p = _key_decode_u32(p, &v);
  oid_hash = v;
}

void DedupStatsCollector::get_dedup_range(int64_t pool, uint64_t digest_msb,
  size_t msb_bits, size_t all_bits,
  std::string& start, std::string& end)
{
  _key_encode_u64(pool + 0x8000000000000000ull, &start);
  _key_encode_u64(pool + 0x8000000000000000ull, &end);
  auto range = range_from_msb(digest_msb, msb_bits, all_bits);
  start += range.first;
  end += range.second;
  end += '\xFF';
}

void DedupStatsCollector::record_candidate(
  CephContext* cct,
  KeyValueDB& kv,
  int64_t pool,
  const ghobject_t& oid,
  const bufferlist& bl)
{
  ceph_assert(cct);
  ceph::crypto::MD5 h;
  for (auto& p : bl.buffers()) {
    h.Update((const unsigned char*)p.c_str(), p.length());
  }
  std::string digest(CEPH_CRYPTO_MD5_DIGESTSIZE, 0);
  h.Final((unsigned char*)digest.data());

  auto ts = mono_clock::now();

  auto key = get_dedup_entry_key(pool, oid, digest, ts);

  dout(10) << __func__ << " "
    << " pool: " << pool
    << " oid: " << oid
    << " csum: " << bin2hex_str(digest)
    << " ts: " << std::chrono::duration_cast<std::chrono::nanoseconds>(ts.time_since_epoch())
    << " key: " << key
    << dendl;

  auto txn = kv.get_transaction();

  bufferlist tmp_bl;
  encode(oid, tmp_bl); //FIXME: make in a more extensible manner

  txn->set(PREFIX_DEDUP_CSUM, key, tmp_bl);
  int r = kv.submit_transaction(txn); // Don't care about flushing this data immediately,
                                      // relying on BlueStore regular traffic instead.
				      // And even failing to flush txc-s at all (e.g. due to crash)
				      // is not a bit deal - we loose some minor fraction of
				      // dedup candidates only. Will retrieve them next time.
				      // The benefit is simplified and faster submit path here,
				      // plus much less write amplification for KV's WAL.
  if (r < 0) {
    derr << __func__ << " failed to submit txc:" << cpp_strerror(r)
      << dendl;
  }
}

int DedupStatsCollector::list_candidates(
  KeyValueDB& db,
  int64_t pool,
  uint64_t digest_msb,
  size_t digest_msb_bits,
  size_t digest_all_bits,
  const ceph::mono_clock::time_point& retrieve_after_btime,
  const ceph::mono_clock::time_point& remove_before_btime,
  int max_return,
  int max_deletion,
  double max_duration,
  std::vector<std::string>& ls,
  std::string* pnext)
{
  auto start_time = mono_clock::now();
  std::shared_lock l(coll_lock);
  if (stopped) {
    return -ECANCELED;
  }

  std::string static_next;
  if (!pnext)
    pnext = &static_next;

  std::string range_start, range_end;
  get_dedup_range(pool,
    digest_msb, digest_msb_bits, digest_all_bits,
    range_start, range_end);
  dout(15) << __func__
    << " range " << pretty_binary_string(range_start)
    << " to " << pretty_binary_string(range_end)
    << " pnext '" << *pnext
    << "' max ret " << max_return
    << " max del " << max_deletion
    << " max dur " << max_duration
    << dendl;
  ls.reserve(ls.size() + max_return);
  auto t = db.get_transaction();
  KeyValueDB::Iterator it =
    db.get_iterator(PREFIX_DEDUP_CSUM, 0,
      KeyValueDB::IteratorBounds{ range_start, range_end });
  it->upper_bound(*pnext);
  int count = 0;
  int eliminate_count = 0;
  bool interrupted = false; // allow at least one iteration
  while (it->valid() && count < max_return && !interrupted && !stopped) {
    int64_t pool = 0;
    std::string digest;
    mono_clock::time_point ts;
    uint32_t oid_hash = 0;

    get_key_dedup_entry(it->key().data(), pool,
      digest, CEPH_CRYPTO_MD5_DIGESTSIZE,
      ts, oid_hash);
    if (ts >= retrieve_after_btime) {
      dout(25) << __func__ << " taking " << pretty_binary_string(it->key()) << dendl;
      ls.emplace_back(it->key());
      ++count;
    }
    else if (ts < remove_before_btime) {
      dout(25) << __func__ << " erasing " << pretty_binary_string(it->key()) << dendl;
      t->rmkey(PREFIX_DEDUP_CSUM, it->key());
      ++eliminate_count;
      if (max_deletion && eliminate_count >= max_deletion) {
	auto r = db.submit_transaction_sync(t);
	if (r < 0) {
	  derr << __func__ << " failed to submit cleanup txc:" << cpp_strerror(r)
	    << dendl;
	}
	eliminate_count = 0;
	t = db.get_transaction();
      }
    }
    *pnext = it->key();
    it->next();

    interrupted = max_duration > 0.0 ?
      (mono_clock::now() - start_time) > make_timespan(max_duration) :
      false;
    if (interrupted) {
      dout(25) << __func__ << " interrupting due to exec time restriction" << dendl;
    }
  }
  if (eliminate_count) {
    auto r = db.submit_transaction_sync(t);
    if (r < 0) {
      derr << __func__ << " failed to submit cleanup txc:" << cpp_strerror(r)
	<< dendl;
    }
  }
  if (stopped)
    return -ECANCELED;
  return count > 0 ? count :
    (interrupted ? -EINTR : 0);
}

int DedupStatsCollector::clear_all_candidates(
  KeyValueDB& db,
  int64_t pool,
  bool async_compact)
{
  std::shared_lock l(coll_lock);
  if (stopped) {
    return -ECANCELED;
  }

  std::string range_start, range_end;
  get_dedup_pool_range(pool, range_start, range_end);

  dout(15) << __func__
    << " pool " << pool
    << " range " << pretty_binary_string(range_start)
    << " to " << pretty_binary_string(range_end)
    << dendl;

  auto t = db.get_transaction();
  t->rm_range_keys(PREFIX_DEDUP_CSUM, range_start, range_end, true); //enforce ranged delete
  auto r = db.submit_transaction_sync(t);
  if (r < 0) {
    derr << __func__ << " failed to submit cleanup txc:" << cpp_strerror(r)
         << dendl;
  } else if (async_compact) {
    db.compact_range_async(PREFIX_DEDUP_CSUM, range_start, range_end);
  } else {
    db.compact_range(PREFIX_DEDUP_CSUM, range_start, range_end);
  }

  dout(15) << __func__ << " return:" << r << dendl;
  return r;
}

int64_t DedupStatsCollector::estimate_size(
  KeyValueDB& db,
  int64_t pool,
  uint64_t* entry_count)
{
  std::shared_lock l(coll_lock);
  if (stopped) {
    return -ECANCELED;
  }

  std::string range_start, range_end;
  get_dedup_pool_range(pool, range_start, range_end);

  dout(15) << __func__
    << " pool " << pool
    << " range " << pretty_binary_string(range_start)
    << " to " << pretty_binary_string(range_end)
    << dendl;

  int64_t res = db.estimate_range_size(PREFIX_DEDUP_CSUM, range_start, range_end);
  if (entry_count && !stopped) {
    KeyValueDB::Iterator it =
      db.get_iterator(PREFIX_DEDUP_CSUM, 0,
	KeyValueDB::IteratorBounds{ range_start, range_end });
    it->upper_bound(std::string());
    while (it->valid() && !stopped) {
      (*entry_count)++;
      it->next();
    }
  }
  dout(5) << __func__ << " return: " << (stopped ? -ECANCELED : res)
          << dendl;
  if (stopped)
    return -ECANCELED;
  return res;
}

void DedupStatsCollector::start()
{
  stopped = false;
  std::unique_lock l(coll_lock);
}

void DedupStatsCollector::stop()
{
  stopped = true;
  std::unique_lock l(coll_lock);
}
