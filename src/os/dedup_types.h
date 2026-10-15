// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_OS_DEDUP_TYPES_H
#define CEPH_OS_DEDUP_TYPES_H

#include <string>

#include "common/ceph_time.h"
#include "common/hobject.h"

// A single deduplication info record as kept by ObjectStore,
// i.e. fields of its DB key except the pool
struct dedup_candidate_t {
  std::string digest;              // raw content digest
  ceph::real_clock::time_point ts; // the time the record has been made at
  uint32_t oid_hash = 0;           // object's hash in bitwise key form
};

#endif
