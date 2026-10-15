// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#pragma once

#include "msg/Message.h"
#include "osd/dedup_list_types.h"

/*
 * List deduplication info collected by a specific OSD for a pool.
 *
 * Only records for the digest shard specified by 'shard' are returned,
 * i.e. ones whose 'shard_bits' most significant digest bits are equal
 * to 'shard'. Listing starts after the 'after' cursor (empty to start from
 * the beginning) and returns up to 'max' entries.
 * Records made more than 'newer_than' seconds ago are skipped
 * (0 - no restriction), records older than 'prune_older_than' seconds
 * are removed (0 - remove nothing).
 */
class MOSDDedupList final : public Message {
public:
  static constexpr int HEAD_VERSION = 1;
  static constexpr int COMPAT_VERSION = 1;

  uuid_d fsid;
  epoch_t epoch = 0;
  int64_t pool = -1;
  uint64_t shard = 0;
  uint8_t shard_bits = 0;
  std::string after;
  uint32_t max = 0;
  uint32_t newer_than = 0;
  uint32_t prune_older_than = 0;

  MOSDDedupList() : Message{MSG_OSD_DEDUP_LIST, HEAD_VERSION, COMPAT_VERSION} {}
  MOSDDedupList(const uuid_d& f, epoch_t e, int64_t pool,
                uint64_t shard, uint8_t shard_bits,
                const std::string& after, uint32_t max,
                uint32_t newer_than, uint32_t prune_older_than) :
    Message{MSG_OSD_DEDUP_LIST, HEAD_VERSION, COMPAT_VERSION},
    fsid(f), epoch(e), pool(pool), shard(shard), shard_bits(shard_bits),
    after(after), max(max), newer_than(newer_than),
    prune_older_than(prune_older_than) {}
private:
  ~MOSDDedupList() final {}

public:
  std::string_view get_type_name() const override { return "osd_dedup_list"; }
  void print(std::ostream& out) const override {
    out << "osd_dedup_list(tid " << get_tid()
        << " pool " << pool
        << " shard " << shard << "/" << (1ull << shard_bits)
        << " max " << max;
    if (!after.empty())
      out << " cont";
    if (newer_than)
      out << " newer_than " << newer_than;
    if (prune_older_than)
      out << " prune_older_than " << prune_older_than;
    out << " e" << epoch << ")";
  }

  void encode_payload(uint64_t features) override {
    using ceph::encode;
    encode(fsid, payload);
    encode(epoch, payload);
    encode(pool, payload);
    encode(shard, payload);
    encode(shard_bits, payload);
    encode(after, payload);
    encode(max, payload);
    encode(newer_than, payload);
    encode(prune_older_than, payload);
  }
  void decode_payload() override {
    using ceph::decode;
    auto p = payload.cbegin();
    decode(fsid, p);
    decode(epoch, p);
    decode(pool, p);
    decode(shard, p);
    decode(shard_bits, p);
    decode(after, p);
    decode(max, p);
    decode(newer_than, p);
    decode(prune_older_than, p);
  }
private:
  template<class T, typename... Args>
  friend boost::intrusive_ptr<T> ceph::make_message(Args&&... args);
};

class MOSDDedupListReply final : public Message {
public:
  static constexpr int HEAD_VERSION = 1;
  static constexpr int COMPAT_VERSION = 1;

  int32_t r = 0;
  osd_dedup_list_result_t result;

  MOSDDedupListReply()
    : Message{MSG_OSD_DEDUP_LIST_REPLY, HEAD_VERSION, COMPAT_VERSION} {}
  MOSDDedupListReply(ceph_tid_t tid, int r, osd_dedup_list_result_t&& res)
    : Message{MSG_OSD_DEDUP_LIST_REPLY, HEAD_VERSION, COMPAT_VERSION},
      r(r), result(std::move(res)) {
    set_tid(tid);
  }
private:
  ~MOSDDedupListReply() final {}

public:
  std::string_view get_type_name() const override {
    return "osd_dedup_list_reply";
  }
  void print(std::ostream& out) const override {
    out << "osd_dedup_list_reply(tid " << get_tid()
        << " r " << r
        << " entries " << result.entries.size();
    if (result.completed)
      out << " completed";
    out << ")";
  }

  void encode_payload(uint64_t features) override {
    using ceph::encode;
    encode(r, payload);
    encode(result, payload);
  }
  void decode_payload() override {
    using ceph::decode;
    auto p = payload.cbegin();
    decode(r, p);
    decode(result, p);
  }
private:
  template<class T, typename... Args>
  friend boost::intrusive_ptr<T> ceph::make_message(Args&&... args);
};
