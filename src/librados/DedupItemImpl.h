// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

/*
 * Ceph - scalable distributed file system
 *
 * This is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License version 2.1, as published by the Free Software
 * Foundation.  See file COPYING.
 *
 */

#ifndef CEPH_LIBRADOS_DEDUPITEM_IMPL_H
#define CEPH_LIBRADOS_DEDUPITEM_IMPL_H

#include <cstdint>
#include <string>

namespace librados {
// Deduplication info record as reported by an OSD
struct DedupItemImpl {
  std::string digest;     // raw content digest
  uint32_t hash = 0;      // object hash
  uint64_t timestamp = 0; // when the record has been made, ns since epoch
  double age = 0.0;       // seconds since the record has been made
  int osd = -1;         // OSD the record has been retrieved from

  auto operator<=>(const DedupItemImpl&) const = default;
};
}
#endif
