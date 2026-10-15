// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_OSD_ONODE_REFORMAT_H
#define CEPH_OSD_ONODE_REFORMAT_H

#include <string>
#include <memory>
#include "BlueStore.h"

class OnodeReformatEngine {
protected:
  std::string args;
public:
  OnodeReformatEngine(std::string_view args) {
  }
  virtual ~OnodeReformatEngine() {}
  /// Whether the engine might rewrite the object, hence the read needs
  /// exclusive access to the collection. Engines which don't are executed
  /// under shared collection lock and shouldn't perform lengthy operations
  /// in execute(): such work should be deferred via
  /// OnodeReformatContext::defer() to run once the lock has been released.
  virtual bool modifies_object() const {
    return true;
  }
  virtual bool validate(OnodeReformatContext& ctx);
  virtual bool execute(OnodeReformatContext& ctx,
    PerfCounters& _logger) = 0;
};

class OnodeReformatRecompressEngine : public OnodeReformatEngine {
  BlueStore::Collection* c;
public:
  OnodeReformatRecompressEngine(std::string_view args)
    : OnodeReformatEngine(args)
  {
  }
  bool execute(OnodeReformatContext& ctx,
    PerfCounters& _logger) override;
};

class OnodeReformatDefragmentEngine : public OnodeReformatEngine {
  BlueStore::Collection* c;
public:
  OnodeReformatDefragmentEngine(std::string_view args)
    : OnodeReformatEngine(args)
  {
  }
  bool execute(OnodeReformatContext& ctx,
    PerfCounters& _logger) override;
};

class OnodeChecksumCollectEngine : public OnodeReformatEngine {
  BlueStore::Collection* c;
public:
  OnodeChecksumCollectEngine(std::string_view args)
    : OnodeReformatEngine(args)
  {
  }
  bool modifies_object() const override {
    return false; // only records dedup info
  }
  bool validate(OnodeReformatContext& ctx) override;
  bool execute(OnodeReformatContext& ctx,
    PerfCounters& _logger) override;
};

class OnodeReformatContext : public BlueStore::read_context_t {

private:

  size_t enabled_engines_count = 0;
  reformat_engines_t engines;
  std::vector<std::function<void()>> deferred;

  bool to_be_applied = false;
  BlueStore::WriteContext wctx;
  span_stat_t span_stat;
  Allocator* alloc = nullptr;
  PExtentVectorSlicer* prealloc_slicer = nullptr; // pointer to the one from wctx if configured
public:

  /// 'exclusive' indicates the collection is locked exclusively, engines
  /// modifying the object are enabled only then
  OnodeReformatContext(const BlueStore::read_context_t& _rctx,
    const reformat_engines_t& _engines, bool exclusive);
  ~OnodeReformatContext();

  bool maybe_allocate(size_t need, size_t min_alloc_size,
    std::function<bool(int64_t, size_t)> acceptor);

  bool is_enabled() const { return enabled_engines_count > 0; }
  bool is_applied() const { return to_be_applied; }
  BlueStore::WriteContext& get_write_context() { return wctx; }
  span_stat_t& access_span_stats() {
    return span_stat;
  }
  const span_stat_t& get_span_stats() const {
    return span_stat;
  }

  /// executes enabled engines, to be called under collection lock:
  /// the ones modifying the object until the first one indicating success,
  /// then all the others
  void exec_engines(PerfCounters& _logger);
  /// defers work to be done once the collection lock is released.
  /// Such work must not access onode, collection or this context, hence
  /// should capture everything it needs.
  void defer(std::function<void()>&& f) {
    deferred.emplace_back(std::move(f));
  }
  /// runs deferred work, to be called with no collection lock held
  void run_deferred();
  /// releases resources preallocated by the engines modifying the object
  void release_preallocated();
  void clear();
};

#endif
