// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_OSD_DEDUP_LIST_TYPES_H
#define CEPH_OSD_DEDUP_LIST_TYPES_H

#include <string>
#include <vector>

#include "include/encoding.h"
#include "common/hobject.h"

// Deduplication info record as reported by an OSD. Along with the pool
// these are the fields of the record's DB key at the OSD.
// Not versioned on its own, see osd_dedup_list_result_t.
struct osd_dedup_list_entry_t {
  std::string digest;     // raw content digest
  uint64_t timestamp = 0; // when the record has been made, ns, OSD's clock
  uint32_t oid_hash = 0;  // object's hash in bitwise key form

  void encode(ceph::buffer::list& bl) const {
    using ceph::encode;
    encode(digest, bl);
    encode(timestamp, bl);
    encode(oid_hash, bl);
  }
  void decode(ceph::buffer::list::const_iterator& p) {
    using ceph::decode;
    decode(digest, p);
    decode(timestamp, p);
    decode(oid_hash, p);
  }
};
WRITE_CLASS_ENCODER(osd_dedup_list_entry_t)

// A portion of deduplication info listing as reported by an OSD
struct osd_dedup_list_result_t {
  bool completed = false; // nothing more to list beyond 'next'
  std::string next;       // opaque cursor to continue listing from
  uint64_t now = 0;       // OSD's current time, same clock as timestamps
  std::vector<osd_dedup_list_entry_t> entries;

  void encode(ceph::buffer::list& bl) const {
    ENCODE_START(1, 1, bl);
    encode(completed, bl);
    encode(next, bl);
    encode(now, bl);
    encode(entries, bl);
    ENCODE_FINISH(bl);
  }
  void decode(ceph::buffer::list::const_iterator& p) {
    DECODE_START(1, p);
    decode(completed, p);
    decode(next, p);
    decode(now, p);
    decode(entries, p);
    DECODE_FINISH(p);
  }
};
WRITE_CLASS_ENCODER(osd_dedup_list_result_t)

#endif
