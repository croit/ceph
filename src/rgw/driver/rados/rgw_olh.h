// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab ft=cpp

#pragma once

#include <cerrno>
#include <map>
#include <set>
#include <utility>
#include <vector>

#include "cls/rgw/cls_rgw_types.h"

namespace rgw::rados_olh {

using Log = std::map<uint64_t, std::vector<rgw_bucket_olh_log_entry>>;

// Read the complete batch before planning destructive work. A LINK on a later
// page must be able to cancel an earlier REMOVE_INSTANCE for the same key.
template <typename Read> int read_log(Read &&read, Log &log) {
  log.clear();
  uint64_t marker = 0;
  bool truncated = false;
  do {
    Log page;
    int r = read(marker, page, truncated);
    if (r < 0) {
      return r;
    }
    if (page.empty()) {
      return truncated ? -EIO : 0;
    }
    if (page.begin()->first <= marker) {
      return -EIO; // overlapping pages or no cursor progress
    }
    marker = page.rbegin()->first;
    log.merge(page);
  } while (truncated);
  return 0;
}

struct Plan {
private:
  const uint64_t applied_epoch;

public:
  cls_rgw_obj_key target;
  bool delete_marker;
  bool link = false;
  bool remove = false;
  std::set<cls_rgw_obj_key> remove_instances;

  Plan(uint64_t applied_epoch, cls_rgw_obj_key target, bool delete_marker)
      : applied_epoch(applied_epoch), target(std::move(target)),
        delete_marker(delete_marker) {}

  // The CLS has already selected the head. Consume its local log in order,
  // including vector order within an epoch; don't repeat remote SID selection.
  bool apply(const rgw_bucket_olh_log_entry &entry) {
    switch (entry.op) {
    case CLS_RGW_OLH_OP_REMOVE_INSTANCE:
      remove_instances.insert(entry.key);
      break;
    case CLS_RGW_OLH_OP_LINK_OLH:
      remove_instances.erase(entry.key);
      // Cleanup intent follows the final head operation, even on a replay.
      remove = false;
      if (entry.epoch > applied_epoch) {
        target = entry.key;
        delete_marker = entry.delete_marker;
        link = true;
      }
      break;
    case CLS_RGW_OLH_OP_UNLINK_OLH:
      if (entry.epoch > applied_epoch) {
        link = false;
        remove = true;
      }
      break;
    case CLS_RGW_OLH_OP_STALE:
      break;
    default:
      return false;
    }
    return true;
  }
};

} // namespace rgw::rados_olh
