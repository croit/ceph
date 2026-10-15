// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include <random>
#include <thread>
#include "common/ceph_context.h"
#include "common/dout.h"

#include "include/intarith.h"
#include "include/ceph_assert.h"

#include "os/kv.h"
#include "OnodeReformat.h"
#include "Allocator.h"

#define dout_subsys ceph_subsys_bluestore

/////////////////////////////////////////
/// OnodeReformatEngine
/////////////////////////////////////////
#undef dout_prefix
#define dout_prefix *_dout << "OnodeReformatEngine "

bool OnodeReformatEngine::validate(OnodeReformatContext& ctx)
{
  ceph_assert(ctx.store.cct);
  bool will_reformat = false;
  auto min_alloc_size = ctx.store.get_min_alloc_size();
  if (ctx.op_flags & CEPH_OSD_OP_FLAG_SCRUB) {
    // for the sake of simplicity do not apply data reformatting to reads
    // unaligned at the beginning. Having unaligned tail is OK.
    will_reformat = p2nphase(ctx.offset, min_alloc_size) == 0;
    if (!will_reformat) {
      ldout(ctx.store.cct, 15) << "reformat '" << args << "'"
	<< " skipped due to unaligned read bounds "
	<< p2nphase(ctx.offset, min_alloc_size) << " "
	<< p2nphase(ctx.offset + ctx.length, min_alloc_size)
	<< dendl;
    } else {
      ldout(ctx.store.cct, 15) << "reformat '" << args << "'"
	<< " enabled "
	<< dendl;
    }
  }
  return will_reformat;
}

/////////////////////////////////////////
/// OnodeReformatRecompressEngine
/////////////////////////////////////////
#undef dout_prefix
#define dout_prefix *_dout << "OnodeReformatRecompress "

bool OnodeReformatRecompressEngine::execute(OnodeReformatContext& ctx,
  PerfCounters& logger)
{
  ceph_assert(ctx.store.cct);
  auto* c = static_cast<BlueStore::Collection*>(ctx.ch.get());
  ceph_assert(c);

  const auto& span_stat = ctx.get_span_stats();
  auto& wctx = ctx.get_write_context();
  auto min_alloc_size = ctx.store.get_min_alloc_size();
  // do reformat if
  // - compression enabled and compressor is available (its plugin might
  //   have failed to load),
  // - object isn't cached (meaning it's not being written at the moment),
  // - and there are no shared blobs within the span as this might increase
  //   used space.
  bool will_do = wctx.compress && wctx.compressor &&
                 span_stat.allocated > 0 &&
                 span_stat.cached == 0 && span_stat.allocated_shared ==0;
  if (will_do) {
    uint64_t need = 0;
    auto bl_it = ctx.bl.begin();
    uint64_t offs = ctx.offset;
    uint64_t old_allocated = span_stat.allocated + span_stat.allocated_compressed;
    while (bl_it != ctx.bl.end()) {

      BlueStore::BlobRef blob = c->new_blob();
      bufferlist from_bl;
      uint64_t l = std::min(wctx.target_blob_size, uint64_t(bl_it.get_remaining()));
      uint64_t res_len = l;
      if (l >= min_alloc_size) {
	l = p2align(l, min_alloc_size);
	bl_it.copy(l, from_bl);

	//FIXME: add zero detection
	auto& wi = wctx.write(offs, blob, l, 0, from_bl, 0, l, false, true);

	res_len = from_bl.length();
	if (l > min_alloc_size &&
	  wctx.compressor->compress(from_bl, wi.compressed_bl, wi.compressor_message) == 0) {

	  res_len = wi.compressed_bl.length();
	  // don't set wi.compress_len and wi.compressed as this is redundant
	  // at this point, to be assigned in _do_alloc_write if needed.
	}
	ldout(ctx.store.cct, 20) << " reformat: " << " precompress : 0x"
	  << std::hex << offs << "~" << l << "->" << res_len
	  << std::dec << " " << *blob
	  << dendl;
      } else {
	bl_it += l;
	ldout(ctx.store.cct, 20) << " reformat: " << " precompress : 0x"
	  << std::hex << offs << "~" << l << "-> remaining tail"
	  << dendl;
      }
      need += p2roundup(res_len, min_alloc_size);
      offs += l;
      will_do = need < old_allocated;
    }

    // At this point will_do indicates if we definitely want recompression,
    // will skip the remaining reformatting then.

    // Keep compressed blobs until final processing no matter if we decided to
    // enforce recompression or not. In the latter case they can be chosen
    // for different engine optimization(s) or be rejected prior to writing out.
    wctx.precompressed = true;
    ldout(ctx.store.cct, 10) << " reformat:'" << args << "'"
      << " need 0x"
      << std::hex << need << " vs. old_allocated 0x" << old_allocated << std::dec
      << " apply: " << will_do
      << dendl;

    logger.inc(l_bluestore_reformat_compress_attempted);
    if (!will_do)
      logger.inc(l_bluestore_reformat_compress_omitted);
  } else {
    ldout(ctx.store.cct, 10) << " reformat:'" << args << "'"
      << " omitted, compress " << wctx.compress
      << " compressor " << (wctx.compressor ? "available" : "unavailable")
      << " alloc " << span_stat.allocated
      << " shared alloc " << span_stat.allocated_shared
      << " cached " << span_stat.cached
      << dendl;
  }
  return will_do;
}

/////////////////////////////////////////
/// OnodeReformatDefragmentEnging
/////////////////////////////////////////
#undef dout_prefix
#define dout_prefix *_dout << "OnodeReformatDefragment "

bool OnodeReformatDefragmentEngine::execute(OnodeReformatContext& ctx,
  PerfCounters& logger)
{
  ceph_assert(ctx.store.cct);
  auto *c = static_cast<BlueStore::Collection*>(ctx.ch.get());
  ceph_assert(c);

  auto min_alloc_size = ctx.store.get_min_alloc_size();

  bool will_do = false;
  const auto& span_stat = ctx.get_span_stats();
  auto need = p2roundup(ctx.length, min_alloc_size);
  size_t frags = 0;
  int64_t allocated = 0;
  if (span_stat.frags > 1 &&
      span_stat.cached == 0 && span_stat.allocated_shared ==0) {
    logger.inc(l_bluestore_reformat_defragment_attempted);
    will_do = ctx.maybe_allocate(need, min_alloc_size,
      [&](int64_t num_bytes, size_t num_frags) {
	allocated = num_bytes;
	frags = num_frags;
	return allocated >= (int64_t)need && frags < span_stat.frags;
      });
    if (!will_do) {
      logger.inc(l_bluestore_reformat_defragment_omitted);
    }
  }
  ldout(ctx.store.cct, 10) << " reformat:'" << args << "'"
    << " preallocated: 0x" << std::hex << allocated << std::dec
    << " old frags:" << span_stat.frags
    << " new frags:" << frags
    << " shared alloc " << span_stat.allocated_shared
    << " cached " << span_stat.cached
    << " apply: " << will_do
    << dendl;
  return will_do;
};

/////////////////////////////////////////
/// OnodeChecksumCollectEngine
/////////////////////////////////////////
#include "DedupStatsCollector.h"

std::string bin2hex_str(const std::string& s);

/*std::string get_dedup_entry_key(int64_t pool,
                                const ghobject_t& oid, const std::string& digest,
                                const mono_clock::time_point& ts);*/

#undef dout_prefix
#define dout_prefix *_dout << "OnodeChecksumCollectEngine"

bool OnodeChecksumCollectEngine::validate(OnodeReformatContext& ctx)
{
  auto kv = ctx.store.get_kv();
  if (!kv)
    return false;

  ceph_assert(ctx.store.cct);
  bool will_handle = false;
  uint64_t want_flags = CEPH_OSD_OP_FLAG_SCRUB | CEPH_OSD_OP_FLAG_PRIMARY;
  if ((ctx.op_flags & want_flags) == want_flags) {
    // Deep scrub might read an object in multiple chunks, hence the digest
    // is built from consecutive reads. Empty objects aren't recorded.
    auto* c = static_cast<BlueStore::Collection*>(ctx.ch.get());
    will_handle = ctx.length > 0 && ctx.o->onode.size > 0 &&
      c->dedup_digest_builder->accepts(ctx.o->oid, ctx.o->onode.nid,
                                       ctx.o->onode.data_version, ctx.offset);
    ldout(ctx.store.cct, 15) << "collect_csum '" << args << "'"
      << (will_handle ? " enabled " : " skipped ")
      << ctx.offset << "~" << ctx.length << " of 0~" << ctx.o->onode.size
      << dendl;
  }
  return will_handle;
}

bool OnodeChecksumCollectEngine::execute(OnodeReformatContext& ctx,
  PerfCounters& logger)
{
  auto kv = ctx.store.get_kv();
  ceph_assert(kv);
  auto* c = static_cast<BlueStore::Collection*>(ctx.ch.get());
  // Capture everything needed while under collection lock, digest
  // calculation and recording are deferred until the lock is released.
  ctx.defer(
    [cct = ctx.store.cct, kv, pool = c->pool(), oid = ctx.o->oid,
     size = ctx.o->onode.size, nid = ctx.o->onode.nid,
     data_version = ctx.o->onode.data_version,
     offset = ctx.offset,
     builder = c->dedup_digest_builder,
     bl = ctx.bl,  // shares the read buffers, no data copy
     delay = ctx.store.cct->_conf->bluestore_debug_collect_csum_delay] {
      if (delay > 0) {
        std::this_thread::sleep_for(ceph::make_timespan(delay));
      }
      auto digest = builder->feed(oid, size, nid, data_version, offset, bl);
      if (digest) {
        DedupStatsCollector::record_candidate(cct, *kv, pool, oid,
                                              nid, data_version, *digest);
      }
    });
  return false; // let other engines work too, FIXME: what happens if rewrite occurs? Shouldn't we omit mtime change then?
};

/////////////////////////////////////////
/// OnodeReformatContext
/////////////////////////////////////////
#undef dout_prefix
#define dout_prefix *_dout << "OnodeReformatContext "

OnodeReformatContext::OnodeReformatContext(const BlueStore::read_context_t& _ctx,
					   const reformat_engines_t& _engines,
					   bool exclusive)
  : read_context_t(_ctx)
{
  // Nothing to reformat if the object doesn't exist, moreover engines
  // are allowed to access the onode when validating.
  if (!_ctx.o || !_ctx.o->exists) {
    return;
  }
  // Choose the engines that should be offered to execution
  for (size_t i = 0; i < _engines.size(); i++) {
    if (_engines[i] && (exclusive || !_engines[i]->modifies_object()) &&
        _engines[i]->validate(*this)) {
      engines[i] = _engines[i];
      enabled_engines_count++;
    }
  }
}
OnodeReformatContext::~OnodeReformatContext()
{
  clear();
}
bool OnodeReformatContext::maybe_allocate(size_t need, size_t min_alloc_size,
  std::function<bool(int64_t, size_t)> acceptor)
{
  if (prealloc_slicer) {
    return false; // repetitive assignments aren't allowed
  }
  ceph_assert(store.cct);
  alloc = store.get_allocator();
  ceph_assert(alloc);

  PExtentVector alloc_vector;
  alloc_vector.reserve(need / min_alloc_size + 1);
  auto start = mono_clock::now();
  auto allocated = alloc->allocate(
    need, min_alloc_size, need,
    0, &alloc_vector);
  store.log_latency("allocator@_prepare_reformat",
    l_bluestore_allocator_lat,
    mono_clock::now() - start,
    store.cct->_conf->bluestore_log_op_age);
  bool will_do = acceptor(allocated, alloc_vector.size());
  if (will_do) {
    prealloc_slicer = &wctx.prealloc_slicer;
    prealloc_slicer->setup(std::move(alloc_vector), allocated);
  } else {
    alloc->release(alloc_vector);
  }
  return will_do;
}

void OnodeReformatContext::exec_engines(
  PerfCounters& logger)
{
  // Enumerate all the validated engines modifying the object and try to
  // execute them in their priority order until the first success indicated
  for (auto& e : engines) {
    if (e.get() && e->modifies_object() && e->execute(*this, logger)) {
      to_be_applied = true;
      break;
    }
  }
  // Engines not modifying the object are always executed
  for (auto& e : engines) {
    if (e.get() && !e->modifies_object()) {
      e->execute(*this, logger);
    }
  }
}

void OnodeReformatContext::run_deferred()
{
  auto fns = std::move(deferred);
  deferred.clear();
  for (auto& f : fns) {
    f();
  }
}
void OnodeReformatContext::release_preallocated()
{
  PExtentVector to_release;
  if (prealloc_slicer && alloc && !prealloc_slicer->end() && prealloc_slicer->slice(to_release) > 0) {
    alloc->release(to_release);
  }
}

void OnodeReformatContext::clear()
{
  release_preallocated();
  deferred.clear();
  for (auto& e : engines) {
    e.reset();
  }
  enabled_engines_count = 0;
  to_be_applied = false;
  wctx.reset();
  alloc = nullptr;
}
