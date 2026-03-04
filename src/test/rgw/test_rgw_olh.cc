// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab ft=cpp

#include <gtest/gtest.h>

#include "rgw/driver/rados/rgw_olh.h"

using rgw::rados_olh::Log;
using rgw::rados_olh::Plan;
using rgw::rados_olh::read_log;

namespace {

rgw_bucket_olh_log_entry entry(uint64_t epoch, OLHLogOp op,
                               const cls_rgw_obj_key &key) {
  rgw_bucket_olh_log_entry result;
  result.epoch = epoch;
  result.op = op;
  result.key = key;
  result.op_tag = std::to_string(epoch);
  return result;
}

struct Reader {
  Log pending;
  unsigned calls = 0;

  int operator()(uint64_t marker, Log &page, bool &truncated) {
    ++calls;
    auto i = pending.upper_bound(marker);
    for (unsigned n = 0; i != pending.end() && n < 1000; ++i, ++n) {
      page.insert(*i);
    }
    truncated = i != pending.end();
    return 0;
  }
};

} // namespace

TEST(rgw_olh, later_page_relink_cancels_removal) {
  cls_rgw_obj_key key("obj", "version");
  Reader reader;
  reader.pending[1] = {entry(1, CLS_RGW_OLH_OP_UNLINK_OLH, key),
                       entry(1, CLS_RGW_OLH_OP_REMOVE_INSTANCE, key)};
  for (uint64_t epoch = 2; epoch <= 1000; ++epoch) {
    reader.pending[epoch] = {entry(epoch, CLS_RGW_OLH_OP_STALE, key)};
  }
  reader.pending[1001] = {entry(1001, CLS_RGW_OLH_OP_LINK_OLH, key)};

  Log log;
  ASSERT_EQ(0, read_log(reader, log));
  ASSERT_EQ(2u, reader.calls);
  ASSERT_EQ(1001u, log.size());
  Plan plan(0, key, false);
  for (const auto &[epoch, entries] : log) {
    for (const auto &e : entries) {
      ASSERT_TRUE(plan.apply(e));
    }
  }
  EXPECT_TRUE(plan.remove_instances.empty());
  EXPECT_TRUE(plan.link);
  EXPECT_FALSE(plan.remove);
  EXPECT_EQ(key, plan.target);
}

TEST(rgw_olh, noncurrent_relink_preserves_authoritative_head) {
  // The restored noncurrent SID sorts before the head. The CLS's last LINK
  // in the epoch must win rather than repeating a SID tie-break here.
  cls_rgw_obj_key restored("obj", "a");
  cls_rgw_obj_key head("obj", "z");
  Plan plan(10, head, false);
  ASSERT_TRUE(plan.apply(entry(11, CLS_RGW_OLH_OP_REMOVE_INSTANCE, restored)));
  ASSERT_TRUE(plan.apply(entry(12, CLS_RGW_OLH_OP_LINK_OLH, restored)));
  ASSERT_TRUE(plan.apply(entry(12, CLS_RGW_OLH_OP_LINK_OLH, head)));
  EXPECT_TRUE(plan.remove_instances.empty());
  EXPECT_EQ(head, plan.target);
  EXPECT_TRUE(plan.link);
  EXPECT_FALSE(plan.remove);
}

TEST(rgw_olh, acknowledgment_does_not_restore_payload) {
  cls_rgw_obj_key key("obj", "version");
  Plan plan(0, key, false);
  ASSERT_TRUE(plan.apply(entry(1, CLS_RGW_OLH_OP_REMOVE_INSTANCE, key)));
  ASSERT_TRUE(plan.apply(entry(2, CLS_RGW_OLH_OP_STALE, key)));
  EXPECT_EQ(1u, plan.remove_instances.count(key));
  EXPECT_FALSE(plan.link);
  EXPECT_FALSE(plan.remove);
}

TEST(rgw_olh, replay_cannot_roll_back_applied_head) {
  cls_rgw_obj_key old("obj", "a");
  cls_rgw_obj_key head("obj", "b");
  Plan plan(20, head, false);
  ASSERT_TRUE(plan.apply(entry(10, CLS_RGW_OLH_OP_LINK_OLH, old)));
  ASSERT_TRUE(plan.apply(entry(15, CLS_RGW_OLH_OP_UNLINK_OLH, old)));
  ASSERT_TRUE(plan.apply(entry(20, CLS_RGW_OLH_OP_LINK_OLH, head)));
  EXPECT_EQ(head, plan.target);
  EXPECT_FALSE(plan.link);
  EXPECT_FALSE(plan.remove);
  ASSERT_TRUE(plan.apply(entry(21, CLS_RGW_OLH_OP_LINK_OLH, old)));
  EXPECT_EQ(old, plan.target);
  EXPECT_TRUE(plan.link);
}

TEST(rgw_olh, removal_after_relink_is_not_canceled) {
  cls_rgw_obj_key key("obj", "version");
  Plan plan(0, key, false);
  ASSERT_TRUE(plan.apply(entry(1, CLS_RGW_OLH_OP_LINK_OLH, key)));
  ASSERT_TRUE(plan.apply(entry(2, CLS_RGW_OLH_OP_UNLINK_OLH, key)));
  ASSERT_TRUE(plan.apply(entry(2, CLS_RGW_OLH_OP_REMOVE_INSTANCE, key)));
  EXPECT_EQ(1u, plan.remove_instances.count(key));
  EXPECT_FALSE(plan.link);
  EXPECT_TRUE(plan.remove);
}

TEST(rgw_olh, empty_log_is_not_truncated) {
  Reader reader;
  Log log;
  ASSERT_EQ(0, read_log(reader, log));
  EXPECT_EQ(1u, reader.calls);
  EXPECT_TRUE(log.empty());
}

TEST(rgw_olh, truncated_empty_page_is_rejected) {
  Log log;
  EXPECT_EQ(-EIO, read_log(
                      [](uint64_t, Log &, bool &truncated) {
                        truncated = true;
                        return 0;
                      },
                      log));
}

TEST(rgw_olh, overlapping_page_is_rejected) {
  cls_rgw_obj_key key("obj", "version");
  unsigned calls = 0;
  Log log;
  EXPECT_EQ(-EIO, read_log(
                      [&](uint64_t, Log &page, bool &truncated) {
                        page[1] = {entry(1, CLS_RGW_OLH_OP_STALE, key)};
                        truncated = ++calls == 1;
                        return 0;
                      },
                      log));
  EXPECT_EQ(2u, calls);
}

TEST(rgw_olh, later_page_error_is_propagated_before_application) {
  cls_rgw_obj_key key("obj", "version");
  unsigned calls = 0;
  Log log;
  EXPECT_EQ(-ENOENT,
            read_log(
                [&](uint64_t, Log &page, bool &truncated) {
                  if (++calls == 2) {
                    return -ENOENT;
                  }
                  page[1] = {entry(1, CLS_RGW_OLH_OP_REMOVE_INSTANCE, key)};
                  truncated = true;
                  return 0;
                },
                log));
  EXPECT_EQ(2u, calls);
}

TEST(rgw_olh, unknown_operation_is_rejected) {
  cls_rgw_obj_key key("obj", "version");
  Plan plan(0, key, false);
  EXPECT_FALSE(plan.apply(entry(1, CLS_RGW_OLH_OP_UNKNOWN, key)));
}
