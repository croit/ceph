// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab

#include "cls/rgw/cls_rgw_client.h"
#include "cls/rgw/cls_rgw_const.h"
#include "cls/rgw/cls_rgw_ops.h"
#include "include/types.h"

#include "gtest/gtest.h"
#include "test/librados/test_cxx.h"
#include "global/global_context.h"
#include "common/ceph_context.h"

#include <errno.h>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <vector>

using namespace std;
using namespace librados;

// creates a temporary pool and initializes an IoCtx shared by all tests
class cls_rgw : public ::testing::Test {
  static librados::Rados rados;
  static std::string pool_name;
 protected:
  static librados::IoCtx ioctx;

  static void SetUpTestCase() {
    pool_name = get_temp_pool_name();
    /* create pool */
    ASSERT_EQ("", create_one_pool_pp(pool_name, rados));
    ASSERT_EQ(0, rados.ioctx_create(pool_name.c_str(), ioctx));
  }
  static void TearDownTestCase() {
    /* remove pool */
    ioctx.close();
    ASSERT_EQ(0, destroy_one_pool_pp(pool_name, rados));
  }
};
librados::Rados cls_rgw::rados;
std::string cls_rgw::pool_name;
librados::IoCtx cls_rgw::ioctx;


string str_int(string s, int i)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "-%d", i);
  s.append(buf);

  return s;
}

void test_stats(librados::IoCtx& ioctx, string& oid, RGWObjCategory category, uint64_t num_entries, uint64_t total_size)
{
  map<int, struct rgw_cls_list_ret> results;
  map<int, string> oids;
  oids[0] = oid;
  ASSERT_EQ(0, CLSRGWIssueGetDirHeader(ioctx, oids, results, 8)());

  uint64_t entries = 0;
  uint64_t size = 0;
  map<int, struct rgw_cls_list_ret>::iterator iter = results.begin();
  for (; iter != results.end(); ++iter) {
    entries += (iter->second).dir.header.stats[category].num_entries;
    size += (iter->second).dir.header.stats[category].total_size;
  }
  ASSERT_EQ(total_size, size);
  ASSERT_EQ(num_entries, entries);
}

void index_prepare(librados::IoCtx& ioctx, string& oid, RGWModifyOp index_op,
                   string& tag, const cls_rgw_obj_key& key, string& loc,
                   uint16_t bi_flags = 0, bool log_op = true)
{
  ObjectWriteOperation op;
  rgw_zone_set zones_trace;
  cls_rgw_bucket_prepare_op(op, index_op, tag, key, loc, log_op, bi_flags, zones_trace);
  ASSERT_EQ(0, ioctx.operate(oid, &op));
}

void index_complete(librados::IoCtx& ioctx, string& oid, RGWModifyOp index_op,
                    string& tag, int epoch, const cls_rgw_obj_key& key,
                    rgw_bucket_dir_entry_meta& meta, uint16_t bi_flags = 0,
                    bool log_op = true)
{
  ObjectWriteOperation op;
  rgw_bucket_entry_ver ver;
  ver.pool = ioctx.get_id();
  ver.epoch = epoch;
  meta.accounted_size = meta.size;
  cls_rgw_bucket_complete_op(op, index_op, tag, ver, key, meta, nullptr, log_op, bi_flags, nullptr);
  ASSERT_EQ(0, ioctx.operate(oid, &op));
  if (!key.instance.empty()) {
    bufferlist olh_tag;
    olh_tag.append(tag);
    rgw_zone_set zone_set;
    ASSERT_EQ(0, cls_rgw_bucket_link_olh(ioctx, oid, key, olh_tag,
                                         false, tag, &meta, epoch,
                                         ceph::real_time{}, true, true, zone_set));
  }
}

TEST_F(cls_rgw, index_basic)
{
  string bucket_oid = str_int("bucket", 0);

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  uint64_t epoch = 1;

  uint64_t obj_size = 1024;

#define NUM_OBJS 10
  for (int i = 0; i < NUM_OBJS; i++) {
    cls_rgw_obj_key obj = str_int("obj", i);
    string tag = str_int("tag", i);
    string loc = str_int("loc", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, i, obj_size * i);

    rgw_bucket_dir_entry_meta meta;
    meta.category = RGWObjCategory::None;
    meta.size = obj_size;
    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, epoch, obj, meta);
  }

  test_stats(ioctx, bucket_oid, RGWObjCategory::None, NUM_OBJS,
	     obj_size * NUM_OBJS);
}

TEST_F(cls_rgw, index_multiple_obj_writers)
{
  string bucket_oid = str_int("bucket", 1);

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  uint64_t obj_size = 1024;

  cls_rgw_obj_key obj = str_int("obj", 0);
  string loc = str_int("loc", 0);
  /* multi prepare on a single object */
  for (int i = 0; i < NUM_OBJS; i++) {
    string tag = str_int("tag", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, 0, 0);
  }

  for (int i = NUM_OBJS; i > 0; i--) {
    string tag = str_int("tag", i - 1);

    rgw_bucket_dir_entry_meta meta;
    meta.category = RGWObjCategory::None;
    meta.size = obj_size * i;

    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, i, obj, meta);

    /* verify that object size doesn't change, as we went back with epoch */
    test_stats(ioctx, bucket_oid, RGWObjCategory::None, 1,
	       obj_size * NUM_OBJS);
  }
}

TEST_F(cls_rgw, index_remove_object)
{
  string bucket_oid = str_int("bucket", 2);

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  uint64_t obj_size = 1024;
  uint64_t total_size = 0;

  int epoch = 0;

  /* prepare multiple objects */
  for (int i = 0; i < NUM_OBJS; i++) {
    cls_rgw_obj_key obj = str_int("obj", i);
    string tag = str_int("tag", i);
    string loc = str_int("loc", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, i, total_size);

    rgw_bucket_dir_entry_meta meta;
    meta.category = RGWObjCategory::None;
    meta.size = i * obj_size;
    total_size += i * obj_size;

    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, ++epoch, obj, meta);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, i + 1, total_size);
  }

  int i = NUM_OBJS / 2;
  string tag_remove = "tag-rm";
  string tag_modify = "tag-mod";
  cls_rgw_obj_key obj = str_int("obj", i);
  string loc = str_int("loc", i);

  /* prepare both removal and modification on the same object */
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag_remove, obj, loc);
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag_modify, obj, loc);

  test_stats(ioctx, bucket_oid, RGWObjCategory::None, NUM_OBJS, total_size);

  rgw_bucket_dir_entry_meta meta;

  /* complete object removal */
  index_complete(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag_remove, ++epoch, obj, meta);

  /* verify stats correct */
  total_size -= i * obj_size;
  test_stats(ioctx, bucket_oid, RGWObjCategory::None, NUM_OBJS - 1, total_size);

  meta.size = 512;
  meta.category = RGWObjCategory::None;

  /* complete object modification */
  index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag_modify, ++epoch, obj, meta);

  /* verify stats correct */
  total_size += meta.size;
  test_stats(ioctx, bucket_oid, RGWObjCategory::None, NUM_OBJS, total_size);


  /* prepare both removal and modification on the same object, this time we'll
   * first complete modification then remove*/
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag_remove, obj, loc);
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag_modify, obj, loc);

  /* complete modification */
  total_size -= meta.size;
  meta.size = i * obj_size * 2;
  meta.category = RGWObjCategory::None;

  /* complete object modification */
  index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag_modify, ++epoch, obj, meta);

  /* verify stats correct */
  total_size += meta.size;
  test_stats(ioctx, bucket_oid, RGWObjCategory::None, NUM_OBJS, total_size);

  /* complete object removal */
  index_complete(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag_remove, ++epoch, obj, meta);

  /* verify stats correct */
  total_size -= meta.size;
  test_stats(ioctx, bucket_oid, RGWObjCategory::None, NUM_OBJS - 1,
	     total_size);
}

TEST_F(cls_rgw, index_suggest)
{
  string bucket_oid = str_int("suggest", 1);
  {
    ObjectWriteOperation op;
    cls_rgw_bucket_init_index(op);
    ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));
  }
  uint64_t total_size = 0;

  int epoch = 0;

  int num_objs = 100;

  uint64_t obj_size = 1024;

  /* create multiple objects */
  for (int i = 0; i < num_objs; i++) {
    cls_rgw_obj_key obj = str_int("obj", i);
    string tag = str_int("tag", i);
    string loc = str_int("loc", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, i, total_size);

    rgw_bucket_dir_entry_meta meta;
    meta.category = RGWObjCategory::None;
    meta.size = obj_size;
    total_size += meta.size;

    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, ++epoch, obj, meta);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, i + 1, total_size);
  }

  /* prepare (without completion) some of the objects */
  for (int i = 0; i < num_objs; i += 2) {
    cls_rgw_obj_key obj = str_int("obj", i);
    string tag = str_int("tag-prepare", i);
    string loc = str_int("loc", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, num_objs, total_size);
  }

  int actual_num_objs = num_objs;
  /* remove half of the objects */
  for (int i = num_objs / 2; i < num_objs; i++) {
    cls_rgw_obj_key obj = str_int("obj", i);
    string tag = str_int("tag-rm", i);
    string loc = str_int("loc", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);

    test_stats(ioctx, bucket_oid, RGWObjCategory::None, actual_num_objs, total_size);

    rgw_bucket_dir_entry_meta meta;
    index_complete(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag, ++epoch, obj, meta);

    total_size -= obj_size;
    actual_num_objs--;
    test_stats(ioctx, bucket_oid, RGWObjCategory::None, actual_num_objs, total_size);
  }

  bufferlist updates;

  for (int i = 0; i < num_objs; i += 2) { 
    cls_rgw_obj_key obj = str_int("obj", i);
    string tag = str_int("tag-rm", i);
    string loc = str_int("loc", i);

    rgw_bucket_dir_entry dirent;
    dirent.key.name = obj.name;
    dirent.locator = loc;
    dirent.exists = (i < num_objs / 2); // we removed half the objects
    dirent.meta.size = 1024;
    dirent.meta.accounted_size = 1024;

    char suggest_op = (i < num_objs / 2 ? CEPH_RGW_UPDATE : CEPH_RGW_REMOVE);
    cls_rgw_encode_suggestion(suggest_op, dirent, updates);
  }

  map<int, string> bucket_objs;
  bucket_objs[0] = bucket_oid;
  int r = CLSRGWIssueSetTagTimeout(ioctx, bucket_objs, 8 /* max aio */, 1)();
  ASSERT_EQ(0, r);

  sleep(1);

  /* suggest changes! */
  {
    ObjectWriteOperation op;
    cls_rgw_suggest_changes(op, updates);
    ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));
  }
  /* suggest changes twice! */
  {
    ObjectWriteOperation op;
    cls_rgw_suggest_changes(op, updates);
    ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));
  }
  test_stats(ioctx, bucket_oid, RGWObjCategory::None, num_objs / 2, total_size);
}

static void list_entries(librados::IoCtx& ioctx,
                         const std::string& oid,
                         uint32_t num_entries,
                         std::map<int, rgw_cls_list_ret>& results)
{
  std::map<int, std::string> oids = { {0, oid} };
  cls_rgw_obj_key start_key;
  string empty_prefix;
  string empty_delimiter;
  ASSERT_EQ(0, CLSRGWIssueBucketList(ioctx, start_key, empty_prefix,
                                     empty_delimiter, num_entries,
                                     true, oids, results, 1)());
}

TEST_F(cls_rgw, index_suggest_complete)
{
  string bucket_oid = str_int("suggest", 2);
  {
    ObjectWriteOperation op;
    cls_rgw_bucket_init_index(op);
    ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));
  }

  cls_rgw_obj_key obj = str_int("obj", 0);
  string tag = str_int("tag-prepare", 0);
  string loc = str_int("loc", 0);

  // prepare entry
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);

  // list entry before completion
  rgw_bucket_dir_entry dirent;
  {
    std::map<int, rgw_cls_list_ret> listing;
    list_entries(ioctx, bucket_oid, 1, listing);
    ASSERT_EQ(1, listing.size());
    const auto& entries = listing.begin()->second.dir.m;
    ASSERT_EQ(1, entries.size());
    dirent = entries.begin()->second;
    ASSERT_EQ(obj, dirent.key);
  }
  // complete entry
  {
    rgw_bucket_dir_entry_meta meta;
    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, 1, obj, meta);
  }
  // suggest removal of listed entry
  {
    bufferlist updates;
    cls_rgw_encode_suggestion(CEPH_RGW_REMOVE, dirent, updates);

    ObjectWriteOperation op;
    cls_rgw_suggest_changes(op, updates);
    ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));
  }
  // list entry again, verify that suggested removal was not applied
  {
    std::map<int, rgw_cls_list_ret> listing;
    list_entries(ioctx, bucket_oid, 1, listing);
    ASSERT_EQ(1, listing.size());
    const auto& entries = listing.begin()->second.dir.m;
    ASSERT_EQ(1, entries.size());
    EXPECT_TRUE(entries.begin()->second.exists);
  }
}

/*
 * This case is used to test whether get_obj_vals will
 * return all validate utf8 objnames and filter out those
 * in BI_PREFIX_CHAR private namespace.
 */
TEST_F(cls_rgw, index_list)
{
  string bucket_oid = str_int("bucket", 4);

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  uint64_t epoch = 1;
  uint64_t obj_size = 1024;
  const int num_objs = 4;
  const string keys[num_objs] = {
    /* single byte utf8 character */
    { static_cast<char>(0x41) },
    /* double byte utf8 character */
    { static_cast<char>(0xCF), static_cast<char>(0x8F) },
    /* treble byte utf8 character */
    { static_cast<char>(0xDF), static_cast<char>(0x8F), static_cast<char>(0x8F) },
    /* quadruble byte utf8 character */
    { static_cast<char>(0xF7), static_cast<char>(0x8F), static_cast<char>(0x8F), static_cast<char>(0x8F) },
  };

  for (int i = 0; i < num_objs; i++) {
    string obj = keys[i];
    string tag = str_int("tag", i);
    string loc = str_int("loc", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc,
		  0 /* bi_flags */, false /* log_op */);

    rgw_bucket_dir_entry_meta meta;
    meta.category = RGWObjCategory::None;
    meta.size = obj_size;
    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, epoch, obj, meta,
		   0 /* bi_flags */, false /* log_op */);
  }

  map<string, bufferlist> entries;
  /* insert 998 omap key starts with BI_PREFIX_CHAR,
   * so bucket list first time will get one key before 0x80 and one key after */
  for (int i = 0; i < 998; ++i) {
    char buf[10];
    snprintf(buf, sizeof(buf), "%c%s%d", 0x80, "1000_", i);
    entries.emplace(string{buf}, bufferlist{});
  }
  ioctx.omap_set(bucket_oid, entries);

  test_stats(ioctx, bucket_oid, RGWObjCategory::None,
	     num_objs, obj_size * num_objs);

  map<int, string> oids = { {0, bucket_oid} };
  map<int, struct rgw_cls_list_ret> list_results;
  cls_rgw_obj_key start_key("", "");
  string empty_prefix;
  string empty_delimiter;
  int r = CLSRGWIssueBucketList(ioctx, start_key,
				empty_prefix, empty_delimiter,
				1000, true, oids, list_results, 1)();
  ASSERT_EQ(r, 0);
  ASSERT_EQ(1u, list_results.size());

  auto it = list_results.begin();
  auto m = (it->second).dir.m;

  ASSERT_EQ(4u, m.size());
  int i = 0;
  for(auto it2 = m.cbegin(); it2 != m.cend(); it2++, i++) {
    ASSERT_EQ(it2->first.compare(keys[i]), 0);
  }
}


/*
 * This case is used to test when bucket index list that includes a
 * delimiter can handle the first chunk ending in a delimiter.
 */
TEST_F(cls_rgw, index_list_delimited)
{
  string bucket_oid = str_int("bucket", 7);

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  uint64_t epoch = 1;
  uint64_t obj_size = 1024;
  const int file_num_objs = 5;
  const int dir_num_objs = 1005;

  std::vector<std::string> file_prefixes =
    { "a", "c", "e", "g", "i", "k", "m", "o", "q", "s", "u" };
  std::vector<std::string> dir_prefixes =
    { "b/", "d/", "f/", "h/", "j/", "l/", "n/", "p/", "r/", "t/" };

  rgw_bucket_dir_entry_meta meta;
  meta.category = RGWObjCategory::None;
  meta.size = obj_size;

  // create top-level files
  for (const auto& p : file_prefixes) {
    for (int i = 0; i < file_num_objs; i++) {
      string tag = str_int("tag", i);
      string loc = str_int("loc", i);
      const string obj = str_int(p, i);

      index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc,
		    0 /* bi_flags */, false /* log_op */);

      index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, epoch, obj, meta,
		     0 /* bi_flags */, false /* log_op */);
    }
  }

  // create large directories
  for (const auto& p : dir_prefixes) {
    for (int i = 0; i < dir_num_objs; i++) {
      string tag = str_int("tag", i);
      string loc = str_int("loc", i);
      const string obj = p + str_int("f", i);

      index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc,
		    0 /* bi_flags */, false /* log_op */);

      index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, epoch, obj, meta,
		     0 /* bi_flags */, false /* log_op */);
    }
  }

  map<int, string> oids = { {0, bucket_oid} };
  map<int, struct rgw_cls_list_ret> list_results;
  cls_rgw_obj_key start_key("", "");
  const string empty_prefix;
  const string delimiter = "/";
  int r = CLSRGWIssueBucketList(ioctx, start_key,
				empty_prefix, delimiter,
				1000, true, oids, list_results, 1)();
  ASSERT_EQ(r, 0);
  ASSERT_EQ(1u, list_results.size()) <<
    "Because we only have one bucket index shard, we should "
    "only get one list_result.";

  auto it = list_results.begin();
  auto id_entry_map = it->second.dir.m;
  bool truncated = it->second.is_truncated;

  // the cls code will make 4 tries to get 1000 entries; however
  // because each of the subdirectories is so large, each attempt will
  // only retrieve the first part of the subdirectory

  ASSERT_EQ(48u, id_entry_map.size()) <<
    "We should get 40 top-level entries and the tops of 8 \"subdirectories\".";
  ASSERT_EQ(true, truncated) << "We did not get all entries.";

  ASSERT_EQ("a-0", id_entry_map.cbegin()->first);
  ASSERT_EQ("p/", id_entry_map.crbegin()->first);

  // now let's get the rest of the entries

  list_results.clear();
  
  cls_rgw_obj_key start_key2("p/", "");
  r = CLSRGWIssueBucketList(ioctx, start_key2,
			    empty_prefix, delimiter,
			    1000, true, oids, list_results, 1)();
  ASSERT_EQ(r, 0);

  it = list_results.begin();
  id_entry_map = it->second.dir.m;
  truncated = it->second.is_truncated;

  ASSERT_EQ(17u, id_entry_map.size()) <<
    "We should get 15 top-level entries and the tops of 2 \"subdirectories\".";
  ASSERT_EQ(false, truncated) << "We now have all entries.";

  ASSERT_EQ("q-0", id_entry_map.cbegin()->first);
  ASSERT_EQ("u-4", id_entry_map.crbegin()->first);
}


TEST_F(cls_rgw, bi_list)
{
  string bucket_oid = str_int("bucket", 5);

  CephContext *cct = reinterpret_cast<CephContext *>(ioctx.cct());

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  const std::string empty_name_filter;
  uint64_t max = 10;
  std::list<rgw_cls_bi_entry> entries;
  bool is_truncated;
  std::string marker;

  int ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, max,
			    &entries, &is_truncated);
  ASSERT_EQ(ret, 0);
  ASSERT_EQ(entries.size(), 0u) <<
    "The listing of an empty bucket as 0 entries.";
  ASSERT_EQ(is_truncated, false) <<
    "The listing of an empty bucket is not truncated.";

  uint64_t epoch = 1;
  uint64_t obj_size = 1024;
  const uint64_t num_objs = 35;

  for (uint64_t i = 0; i < num_objs; i++) {
    string obj = str_int(i % 4 ? "obj" : "об'єкт", i);
    string tag = str_int("tag", i);
    string loc = str_int("loc", i);
    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc,
		  RGW_BILOG_FLAG_VERSIONED_OP);

    rgw_bucket_dir_entry_meta meta;
    meta.category = RGWObjCategory::None;
    meta.size = obj_size;
    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, epoch, obj, meta,
		   RGW_BILOG_FLAG_VERSIONED_OP);
  }

  ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, num_objs + 10,
			&entries, &is_truncated);
  ASSERT_EQ(ret, 0);
  if (is_truncated) {
    ASSERT_LT(entries.size(), num_objs);
  } else {
    ASSERT_EQ(entries.size(), num_objs);
  }

  uint64_t num_entries = 0;

  is_truncated = true;
  marker.clear();
  while(is_truncated) {
    ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, max,
			  &entries, &is_truncated);
    ASSERT_EQ(ret, 0);
    if (is_truncated) {
      ASSERT_LT(entries.size(), num_objs - num_entries);
    } else {
      ASSERT_EQ(entries.size(), num_objs - num_entries);
    }
    num_entries += entries.size();
    marker = entries.back().idx;
  }

  // try with marker as final entry
  ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, max,
			&entries, &is_truncated);
  ASSERT_EQ(ret, 0);
  ASSERT_EQ(entries.size(), 0u);
  ASSERT_EQ(is_truncated, false);

  if (cct->_conf->osd_max_omap_entries_per_request < 15) {
    num_entries = 0;
    max = 15;
    is_truncated = true;
    marker.clear();
    while(is_truncated) {
      ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, max,
			    &entries, &is_truncated);
      ASSERT_EQ(ret, 0);
      if (is_truncated) {
	ASSERT_LT(entries.size(), num_objs - num_entries);
      } else {
	ASSERT_EQ(entries.size(), num_objs - num_entries);
      }
      num_entries += entries.size();
      marker = entries.back().idx;
    }

    // try with marker as final entry
    ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, max,
			  &entries, &is_truncated);
    ASSERT_EQ(ret, 0);
    ASSERT_EQ(entries.size(), 0u);
    ASSERT_EQ(is_truncated, false);
  }

  // test with name filters; pairs contain filter and expected number of elements returned
  const std::list<std::pair<const std::string,unsigned>> filters_results =
    { { str_int("obj", 9), 1 },
      { str_int("об'єкт", 8), 1 },
      { str_int("obj", 8), 0 } };
  for (const auto& filter_result : filters_results) {
    is_truncated = true;
    entries.clear();
    marker.clear();

    ret = cls_rgw_bi_list(ioctx, bucket_oid, filter_result.first, marker, max,
			  &entries, &is_truncated);

    ASSERT_EQ(ret, 0) << "bi list test with name filters should succeed";
    ASSERT_EQ(entries.size(), filter_result.second) <<
      "bi list test with filters should return the correct number of results";
    ASSERT_EQ(is_truncated, false) <<
      "bi list test with filters should return correct truncation indicator";
  }

  // test whether combined segment count is correcgt
  is_truncated = false;
  entries.clear();
  marker.clear();

  ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, num_objs - 1,
			&entries, &is_truncated);
  ASSERT_EQ(ret, 0) << "combined segment count should succeed";
  ASSERT_EQ(entries.size(), num_objs - 1) <<
    "combined segment count should return the correct number of results";
  ASSERT_EQ(is_truncated, true) <<
    "combined segment count should return correct truncation indicator";


  marker = entries.back().idx; // advance marker
  ret = cls_rgw_bi_list(ioctx, bucket_oid, empty_name_filter, marker, num_objs - 1,
			&entries, &is_truncated);
  ASSERT_EQ(ret, 0) << "combined segment count should succeed";
  ASSERT_EQ(entries.size(), 1) <<
    "combined segment count should return the correct number of results";
  ASSERT_EQ(is_truncated, false) <<
    "combined segment count should return correct truncation indicator";
}

static const string olh_test_tag = "olh-test-tag";

static int init_olh_test_index(IoCtx &ioctx, const string &oid) {
  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  return ioctx.operate(oid, &op);
}

template <typename T>
static int put_bi_test_entry(IoCtx &ioctx, const string &oid, BIIndexType type,
                             const string &idx, const T &data) {
  rgw_cls_bi_entry entry;
  entry.type = type;
  entry.idx = idx;
  encode(data, entry.data);
  ObjectWriteOperation op;
  cls_rgw_bi_put(op, oid, entry);
  return ioctx.operate(oid, &op);
}

template <typename T>
static int get_bi_test_entry(IoCtx &ioctx, const string &oid, BIIndexType type,
                             const cls_rgw_obj_key &key, T &data) {
  rgw_cls_bi_entry entry;
  int r = cls_rgw_bi_get(ioctx, oid, type, key, &entry);
  if (r < 0) {
    return r;
  }
  auto p = entry.data.cbegin();
  decode(data, p);
  return 0;
}

static string olh_test_index_key(const string &name) {
  return string("\x80"
                "1001_",
                6) +
         name;
}

static int
put_olh_test_instance(IoCtx &ioctx, const string &oid,
                      const cls_rgw_obj_key &key,
                      ceph::real_time mtime = ceph::real_clock::now()) {
  rgw_bucket_dir_entry entry;
  entry.key = key;
  entry.exists = true;
  entry.meta.mtime = mtime;
  const string idx = string("\x80"
                            "1000_",
                            6) +
                     key.name + string("\0i", 2) + key.instance;
  return put_bi_test_entry(ioctx, oid, BIIndexType::Instance, idx, entry);
}

static int put_olh_test_version(IoCtx &ioctx, const string &oid,
                                const rgw_bucket_dir_entry &entry,
                                const string &list_idx) {
  const string instance_idx = string("\x80"
                                     "1000_",
                                     6) +
                              entry.key.name + string("\0i", 2) +
                              entry.key.instance;
  int ret =
      put_bi_test_entry(ioctx, oid, BIIndexType::Instance, instance_idx, entry);
  if (ret < 0) {
    return ret;
  }
  return put_bi_test_entry(ioctx, oid, BIIndexType::Plain, list_idx, entry);
}

static void list_olh_test_versions(IoCtx &ioctx, const string &oid,
                                   const string &name, uint32_t max_entries,
                                   map<int, rgw_cls_list_ret> &results) {
  map<int, string> oids = {{0, oid}};
  cls_rgw_obj_key start;
  const string prefix = name + string("\0", 1);
  string delimiter;
  ASSERT_EQ(0, CLSRGWIssueBucketList(ioctx, start, prefix, delimiter,
                                     max_entries, true, oids, results, 1)());
}

static int
link_olh_test_instance(IoCtx &ioctx, const string &oid,
                       const cls_rgw_obj_key &key, bool delete_marker,
                       uint64_t epoch, const string &op_tag,
                       ceph::real_time unmod_since = ceph::real_time{}) {
  bufferlist tag;
  tag.append(olh_test_tag);
  rgw_bucket_dir_entry_meta meta;
  meta.mtime = ceph::real_clock::now();
  return cls_rgw_bucket_link_olh(ioctx, oid, key, tag, delete_marker, op_tag,
                                 &meta, epoch, unmod_since, true, false,
                                 rgw_zone_set{});
}

static int read_olh_test_log(IoCtx &ioctx, const string &oid,
                             const cls_rgw_obj_key &key, uint64_t marker,
                             rgw_cls_read_olh_log_ret &result,
                             bool get_stales = false) {
  rgw_cls_read_olh_log_op call;
  call.olh = key;
  call.olh_tag = olh_test_tag;
  call.ver_marker = marker;
  call.get_stales = get_stales;
  bufferlist in, out;
  encode(call, in);
  int r = ioctx.exec(oid, RGW_CLASS, RGW_BUCKET_READ_OLH_LOG, in, out);
  if (r < 0) {
    return r;
  }
  auto p = out.cbegin();
  decode(result, p);
  return 0;
}

TEST(cls_rgw_ops, read_olh_log_v1_decode) {
  rgw_cls_read_olh_log_op call;
  EXPECT_FALSE(call.get_stales);
  call.olh.name = "obj";
  call.ver_marker = 123;
  call.olh_tag = olh_test_tag;
  bufferlist bl;
  {
    ENCODE_START(1, 1, bl);
    encode(call.olh, bl);
    encode(call.ver_marker, bl);
    encode(call.olh_tag, bl);
    ENCODE_FINISH(bl);
  }
  // Decoding v1 must reset the flag even when the object is reused.
  call.get_stales = true;
  auto p = bl.cbegin();
  decode(call, p);
  EXPECT_FALSE(call.get_stales);
  EXPECT_EQ("obj", call.olh.name);
  EXPECT_EQ(123u, call.ver_marker);
  EXPECT_EQ(olh_test_tag, call.olh_tag);

  call.get_stales = true;
  bl.clear();
  encode(call, bl);
  rgw_cls_read_olh_log_op decoded;
  p = bl.cbegin();
  decode(decoded, p);
  EXPECT_TRUE(decoded.get_stales);
}

TEST(cls_rgw_ops, olh_entry_v1_roundtrip) {
  rgw_bucket_olh_entry entry;
  entry.key = cls_rgw_obj_key("obj", "head");
  entry.epoch = 20;
  entry.tag = olh_test_tag;
  entry.exists = true;
  rgw_bucket_olh_log_entry log_entry;
  log_entry.epoch = 1800000000123456789ULL;
  log_entry.op = CLS_RGW_OLH_OP_LINK_OLH;
  log_entry.op_tag = "link-head";
  log_entry.key = entry.key;
  entry.pending_log[log_entry.epoch].push_back(log_entry);

  // Compare against the original Reef v1 schema, including its exact
  // version/compat header and field layout.
  bufferlist expected;
  {
    ENCODE_START(1, 1, expected);
    encode(entry.key, expected);
    encode(entry.delete_marker, expected);
    encode(entry.epoch, expected);
    encode(entry.pending_log, expected);
    encode(entry.tag, expected);
    encode(entry.exists, expected);
    encode(entry.pending_removal, expected);
    ENCODE_FINISH(expected);
  }
  bufferlist bl;
  encode(entry, bl);
  EXPECT_EQ(expected.to_str(), bl.to_str());

  rgw_bucket_olh_entry decoded;
  auto p = bl.cbegin();
  decode(decoded, p);
  EXPECT_EQ(entry.key, decoded.key);
  EXPECT_EQ(entry.epoch, decoded.epoch);
  EXPECT_EQ(entry.tag, decoded.tag);
  EXPECT_TRUE(decoded.exists);
  bufferlist roundtrip;
  encode(decoded, roundtrip);
  EXPECT_EQ(expected.to_str(), roundtrip.to_str());
}

TEST_F(cls_rgw, olh_unlink_noncurrent_delete_marker) {
  string oid = "olh-noncurrent-marker";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key marker("obj", "marker");
  cls_rgw_obj_key head("obj", "head");
  cls_rgw_obj_key olh_key("obj");
  ASSERT_EQ(
      0, link_olh_test_instance(ioctx, oid, marker, true, 10, "link-marker"));
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
  ASSERT_EQ(0,
            link_olh_test_instance(ioctx, oid, head, false, 20, "link-head"));

  rgw_cls_read_olh_log_ret result;
  ASSERT_EQ(0,
            cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
  ASSERT_FALSE(result.log.empty());
  const uint64_t watermark = result.log.rbegin()->first;
  {
    ObjectWriteOperation op;
    cls_rgw_trim_olh_log(op, olh_key, watermark, olh_test_tag);
    ASSERT_EQ(0, ioctx.operate(oid, &op));
  }

  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, marker,
                                              "unlink-marker", olh_test_tag, 0,
                                              false, rgw_zone_set{}));
  ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, watermark, olh_test_tag,
                                   result));
  ASSERT_EQ(1u, result.log.size());
  ASSERT_EQ(1u, result.log.begin()->second.size());
  const auto &entry = result.log.begin()->second.front();
  EXPECT_GT(result.log.begin()->first, watermark);
  EXPECT_EQ(result.log.begin()->first, entry.epoch);
  EXPECT_EQ(CLS_RGW_OLH_OP_STALE, entry.op);
  EXPECT_EQ("unlink-marker", entry.op_tag);
  EXPECT_EQ(marker, entry.key);
  EXPECT_TRUE(entry.delete_marker);
  EXPECT_FALSE(result.is_truncated);

  rgw_bucket_olh_entry olh;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(head, olh.key);
  EXPECT_EQ(20u, olh.epoch);
  rgw_cls_bi_entry removed;
  EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Instance, marker,
                                    &removed));

  // A legacy/default-false reader must not receive the new op.
  ASSERT_EQ(0, read_olh_test_log(ioctx, oid, olh_key, 0, result));
  EXPECT_TRUE(result.log.empty());
  EXPECT_FALSE(result.is_truncated);

  ObjectWriteOperation op;
  ASSERT_FALSE(olh.pending_log.empty());
  cls_rgw_trim_olh_log(op, olh_key, olh.pending_log.rbegin()->first,
                       olh_test_tag);
  ASSERT_EQ(0, ioctx.operate(oid, &op));
  ASSERT_EQ(0,
            cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
  EXPECT_TRUE(result.log.empty());
  EXPECT_FALSE(result.is_truncated);
}

static void test_olh_stale_unlink_preserves_null_variant(IoCtx &ioctx,
                                                         bool delete_marker) {
  string oid = delete_marker ? "olh-stale-unlink-null-marker"
                             : "olh-stale-unlink-null-data";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key null_key("obj");
  cls_rgw_obj_key explicit_null("obj", "null");
  cls_rgw_obj_key numbered("obj", "old");
  const string instance_idx = string("\x80"
                                     "1000_",
                                     6) +
                              null_key.name + string("\0i", 2);
  const BIIndexType instance_type =
      delete_marker ? BIIndexType::Instance : BIIndexType::Plain;
  const cls_rgw_obj_key instance_selector =
      delete_marker ? cls_rgw_obj_key(null_key.name, std::string("\0d", 2))
                    : cls_rgw_obj_key(instance_idx);
  rgw_bucket_dir_entry_meta meta;
  meta.mtime = ceph::real_time{ceph::timespan(1530000000123456789ULL)};
  bufferlist tag;
  tag.append(olh_test_tag);
  const auto link_null = [&](uint64_t epoch) {
    ASSERT_EQ(0, cls_rgw_bucket_link_olh(ioctx, oid, null_key, tag,
                                         delete_marker, "link-null", &meta,
                                         epoch, ceph::real_time{}, true, false,
                                         rgw_zone_set{}));
  };

  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, numbered));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, numbered, false, 50,
                                      "link-numbered"));
  if (!delete_marker) {
    ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, null_key, meta.mtime));
  }
  // Commit a newer version in the same NULL slot before replaying its unlink.
  ASSERT_NO_FATAL_FAILURE(link_null(100));
  ASSERT_NO_FATAL_FAILURE(link_null(200));
  rgw_bucket_dir_entry stored;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, instance_type, instance_selector,
                                 stored));
  EXPECT_EQ(null_key, stored.key);
  EXPECT_EQ(200u, stored.versioned_epoch);
  EXPECT_TRUE(stored.is_current());
  EXPECT_EQ(delete_marker, stored.is_delete_marker());
  EXPECT_EQ(!delete_marker, stored.exists);
  EXPECT_EQ(meta.mtime, stored.meta.mtime);
  bufferlist original_instance;
  encode(stored, original_instance);
  rgw_bucket_olh_entry olh;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, null_key, olh));
  EXPECT_EQ(null_key, olh.key);
  EXPECT_EQ(200u, olh.epoch);
  EXPECT_TRUE(olh.exists);
  EXPECT_EQ(delete_marker, olh.delete_marker);
  EXPECT_FALSE(olh.pending_removal);
  map<int, rgw_cls_list_ret> listing;
  list_olh_test_versions(ioctx, oid, null_key.name, 10, listing);
  ASSERT_EQ(1u, listing.size());
  ASSERT_EQ(2u, listing.begin()->second.dir.m.size());
  EXPECT_EQ(null_key, listing.begin()->second.dir.m.begin()->second.key);
  EXPECT_TRUE(listing.begin()->second.dir.m.begin()->second.is_current());
  EXPECT_EQ(delete_marker,
            listing.begin()->second.dir.m.begin()->second.is_delete_marker());
  EXPECT_EQ(meta.mtime,
            listing.begin()->second.dir.m.begin()->second.meta.mtime);

  unsigned stale_unlink_seq = 0;
  const auto check_stale_unlink = [&](const cls_rgw_obj_key &key,
                                      uint64_t epoch) {
    SCOPED_TRACE(key.instance);
    SCOPED_TRACE(epoch);
    const string op_tag = "stale-unlink-null-" + to_string(++stale_unlink_seq);
    SCOPED_TRACE(op_tag);
    rgw_cls_bi_entry instance_before;
    ASSERT_EQ(0, cls_rgw_bi_get(ioctx, oid, instance_type, instance_selector,
                                &instance_before));
    rgw_bucket_olh_entry olh_before;
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, null_key,
                                   olh_before));
    map<int, rgw_cls_list_ret> before;
    list_olh_test_versions(ioctx, oid, null_key.name, 10, before);
    ASSERT_EQ(1u, before.size());
    bufferlist expected_listing;
    encode(before.begin()->second.dir.m, expected_listing);

    ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, key, op_tag,
                                                olh_test_tag, epoch, false,
                                                rgw_zone_set{}));
    rgw_cls_bi_entry instance_after;
    ASSERT_EQ(0, cls_rgw_bi_get(ioctx, oid, instance_type, instance_selector,
                                &instance_after));
    EXPECT_EQ(instance_before.data.to_str(), instance_after.data.to_str());
    rgw_bucket_olh_entry olh_after;
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, null_key,
                                   olh_after));
    EXPECT_EQ(olh_before.key, olh_after.key);
    EXPECT_EQ(olh_before.delete_marker, olh_after.delete_marker);
    EXPECT_EQ(olh_before.epoch, olh_after.epoch);
    EXPECT_EQ(olh_before.tag, olh_after.tag);
    EXPECT_EQ(olh_before.exists, olh_after.exists);
    EXPECT_EQ(olh_before.pending_removal, olh_after.pending_removal);

    // Acknowledge this op_tag without queueing removal or changing the head.
    // Removing just that acknowledgment must recover every earlier log byte.
    auto earlier_log = olh_after.pending_log;
    ASSERT_FALSE(earlier_log.empty());
    auto newest = earlier_log.rbegin();
    ASSERT_FALSE(newest->second.empty());
    const auto ack = newest->second.back();
    EXPECT_EQ(CLS_RGW_OLH_OP_STALE, ack.op);
    EXPECT_EQ(op_tag, ack.op_tag);
    EXPECT_EQ(null_key, ack.key);
    EXPECT_TRUE(ack.key.instance.empty());
    EXPECT_EQ(delete_marker, ack.delete_marker);
    EXPECT_EQ(newest->first, ack.epoch);
    EXPECT_GT(ack.epoch, epoch);
    EXPECT_GT(ack.epoch, olh_before.epoch);
    if (!olh_before.pending_log.empty()) {
      EXPECT_GE(ack.epoch, olh_before.pending_log.rbegin()->first);
    }
    newest->second.pop_back();
    if (newest->second.empty()) {
      earlier_log.erase(newest->first);
    }
    bufferlist expected_log, actual_log;
    encode(olh_before.pending_log, expected_log);
    encode(earlier_log, actual_log);
    EXPECT_EQ(expected_log.to_str(), actual_log.to_str());
    map<int, rgw_cls_list_ret> after;
    list_olh_test_versions(ioctx, oid, null_key.name, 10, after);
    ASSERT_EQ(1u, after.size());
    bufferlist actual_listing;
    encode(after.begin()->second.dir.m, actual_listing);
    EXPECT_EQ(expected_listing.to_str(), actual_listing.to_str());
  };

  // Both spellings address the same mutable NULL slot; neither may remove
  // the newer data/marker, its listing, or its current OLH.
  ASSERT_NO_FATAL_FAILURE(check_stale_unlink(null_key, 100));
  ASSERT_NO_FATAL_FAILURE(check_stale_unlink(explicit_null, 100));

  // A numbered target is immutable and may still be removed even when both
  // its own epoch and the current NULL head are newer than the unlink.
  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, numbered,
                                              "unlink-numbered", olh_test_tag,
                                              25, false, rgw_zone_set{}));
  listing.clear();
  list_olh_test_versions(ioctx, oid, null_key.name, 10, listing);
  ASSERT_EQ(1u, listing.size());
  ASSERT_EQ(1u, listing.begin()->second.dir.m.size());
  EXPECT_EQ(null_key, listing.begin()->second.dir.m.begin()->second.key);
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, instance_type, instance_selector,
                                 stored));
  bufferlist actual_instance;
  encode(stored, actual_instance);
  EXPECT_EQ(original_instance.to_str(), actual_instance.to_str());
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, null_key, olh));
  EXPECT_EQ(null_key, olh.key);
  EXPECT_EQ(200u, olh.epoch);
  EXPECT_TRUE(olh.exists);
  EXPECT_EQ(delete_marker, olh.delete_marker);
  EXPECT_FALSE(olh.pending_removal);

  // Isolate the OLH guard: the instance epoch only equals the incoming
  // unlink, but the same NULL key is committed as the newer current head.
  olh.epoch = 300;
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                 olh_test_index_key(null_key.name), olh));
  ASSERT_NO_FATAL_FAILURE(check_stale_unlink(explicit_null, 200));

  // A fresh unlink must still remove the listing and clear the current OLH.
  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, null_key,
                                              "fresh-unlink-null", olh_test_tag,
                                              300, false, rgw_zone_set{}));
  listing.clear();
  list_olh_test_versions(ioctx, oid, null_key.name, 10, listing);
  ASSERT_EQ(1u, listing.size());
  EXPECT_TRUE(listing.begin()->second.dir.m.empty());
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, null_key, olh));
  EXPECT_EQ(null_key, olh.key);
  EXPECT_EQ(300u, olh.epoch);
  EXPECT_FALSE(olh.exists);
  EXPECT_FALSE(olh.delete_marker);
  EXPECT_TRUE(olh.pending_removal);
  ASSERT_FALSE(olh.pending_log.empty());
  const auto &fresh_log = olh.pending_log.rbegin()->second;
  ASSERT_GE(fresh_log.size(), 2u);
  EXPECT_EQ(CLS_RGW_OLH_OP_UNLINK_OLH, fresh_log[fresh_log.size() - 2].op);
  EXPECT_EQ("fresh-unlink-null", fresh_log[fresh_log.size() - 2].op_tag);
  EXPECT_EQ(delete_marker ? CLS_RGW_OLH_OP_STALE
                          : CLS_RGW_OLH_OP_REMOVE_INSTANCE,
            fresh_log.back().op);
  EXPECT_EQ("fresh-unlink-null", fresh_log.back().op_tag);
  EXPECT_EQ(null_key, fresh_log.back().key);
  EXPECT_EQ(delete_marker, fresh_log.back().delete_marker);
  rgw_cls_bi_entry raw;
  if (delete_marker) {
    EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, instance_type,
                                      instance_selector, &raw));
  } else {
    // Data remains until the queued REMOVE_INSTANCE is applied.
    ASSERT_EQ(
        0, cls_rgw_bi_get(ioctx, oid, instance_type, instance_selector, &raw));
    EXPECT_EQ(original_instance.to_str(), raw.data.to_str());
  }

  cls_rgw_obj_key new_head("obj", "new-head");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, new_head));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, new_head, false, 400,
                                      "link-new-head"));
  ASSERT_NO_FATAL_FAILURE(link_null(200));
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, instance_type, instance_selector,
                                 stored));
  EXPECT_EQ(200u, stored.versioned_epoch);
  EXPECT_FALSE(stored.is_current());
  EXPECT_EQ(delete_marker, stored.is_delete_marker());
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, null_key, olh));
  EXPECT_EQ(new_head, olh.key);
  EXPECT_EQ(400u, olh.epoch);
  listing.clear();
  list_olh_test_versions(ioctx, oid, null_key.name, 10, listing);
  ASSERT_EQ(1u, listing.size());
  ASSERT_EQ(2u, listing.begin()->second.dir.m.size());
  EXPECT_EQ(new_head, listing.begin()->second.dir.m.begin()->second.key);
  EXPECT_EQ(null_key, listing.begin()->second.dir.m.rbegin()->second.key);
  EXPECT_FALSE(listing.begin()->second.dir.m.rbegin()->second.is_current());

  // Isolate the instance guard under a numbered head, then accept an unlink
  // of that older NULL target at its own epoch despite the newer head.
  ASSERT_NO_FATAL_FAILURE(check_stale_unlink(explicit_null, 100));
  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, null_key,
                                              "unlink-old-null", olh_test_tag,
                                              200, false, rgw_zone_set{}));
  listing.clear();
  list_olh_test_versions(ioctx, oid, null_key.name, 10, listing);
  ASSERT_EQ(1u, listing.size());
  ASSERT_EQ(1u, listing.begin()->second.dir.m.size());
  EXPECT_EQ(new_head, listing.begin()->second.dir.m.begin()->second.key);
  EXPECT_TRUE(listing.begin()->second.dir.m.begin()->second.is_current());
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, null_key, olh));
  EXPECT_EQ(new_head, olh.key);
  EXPECT_EQ(400u, olh.epoch);
  EXPECT_TRUE(olh.exists);
  EXPECT_FALSE(olh.delete_marker);
  EXPECT_FALSE(olh.pending_removal);
}

TEST_F(cls_rgw, olh_stale_unlink_preserves_newer_null_data) {
  ASSERT_NO_FATAL_FAILURE(
      test_olh_stale_unlink_preserves_null_variant(ioctx, false));
}

TEST_F(cls_rgw, olh_stale_unlink_preserves_newer_null_delete_marker) {
  ASSERT_NO_FATAL_FAILURE(
      test_olh_stale_unlink_preserves_null_variant(ioctx, true));
}

TEST_F(cls_rgw, bi_get_null_delete_marker_binary_selector) {
  string oid = "bi-get-null-delete-marker";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key key("obj");
  rgw_bucket_dir_entry_meta meta;
  meta.mtime = ceph::real_time{ceph::timespan(1530000000123456789ULL)};
  meta.owner = "marker-owner";
  bufferlist tag;
  tag.append(olh_test_tag);
  ASSERT_EQ(0, cls_rgw_bucket_link_olh(
                   ioctx, oid, key, tag, true, "link-null-marker", &meta, 200,
                   ceph::real_time{}, true, false, rgw_zone_set{}));

  // The selector is the existing binary index suffix, not a version id.
  cls_rgw_obj_key selector(key.name, std::string("\0d", 2));
  rgw_cls_bi_entry raw;
  ASSERT_EQ(0,
            cls_rgw_bi_get(ioctx, oid, BIIndexType::Instance, selector, &raw));
  EXPECT_EQ(BIIndexType::Instance, raw.type);
  const string expected_idx = string("\x80"
                                     "1000_",
                                     6) +
                              key.name + string("\0i\0d", 4);
  EXPECT_EQ(expected_idx, raw.idx);
  rgw_bucket_dir_entry marker;
  auto p = raw.data.cbegin();
  decode(marker, p);
  EXPECT_EQ(key, marker.key);
  EXPECT_TRUE(marker.key.instance.empty());
  EXPECT_TRUE(marker.is_delete_marker());
  EXPECT_TRUE(marker.is_current());
  EXPECT_FALSE(marker.exists);
  EXPECT_EQ(200u, marker.versioned_epoch);
  EXPECT_EQ(meta.mtime, marker.meta.mtime);
  EXPECT_EQ(meta.owner, marker.meta.owner);
  bufferlist expected_meta, actual_meta;
  encode(meta, expected_meta);
  encode(marker.meta, actual_meta);
  EXPECT_EQ(expected_meta.to_str(), actual_meta.to_str());

  // BI get treats the ordinary spelling literally; it is not normalized.
  EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Instance,
                                    cls_rgw_obj_key(key.name, "null"), &raw));

  // An empty raw selector reads the plain version placeholder. The binary
  // selector above is required to retrieve the actual NULL marker metadata.
  ASSERT_EQ(0, cls_rgw_bi_get(ioctx, oid, BIIndexType::Instance, key, &raw));
  EXPECT_EQ(BIIndexType::Instance, raw.type);
  EXPECT_EQ(key.name, raw.idx);
  rgw_bucket_dir_entry placeholder;
  p = raw.data.cbegin();
  decode(placeholder, p);
  EXPECT_EQ(key, placeholder.key);
  EXPECT_TRUE(placeholder.flags & rgw_bucket_dir_entry::FLAG_VER_MARKER);
  EXPECT_FALSE(placeholder.is_delete_marker());
}

TEST_F(cls_rgw, olh_stale_ops_use_local_log_epochs) {
  string oid = "olh-stale-log-epochs";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key marker("obj", "marker");
  cls_rgw_obj_key head("obj", "head");
  cls_rgw_obj_key stale("obj", "stale");
  cls_rgw_obj_key olh_key("obj");
  ASSERT_EQ(
      0, link_olh_test_instance(ioctx, oid, marker, true, 10, "link-marker"));
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
  ASSERT_EQ(0,
            link_olh_test_instance(ioctx, oid, head, false, 20, "link-head"));

  // Use the actual earlier local log timestamp. A stale remote epoch must
  // not put the incoming op below this marker while the local clock advances.
  rgw_cls_read_olh_log_ret result;
  ASSERT_EQ(0,
            cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
  ASSERT_FALSE(result.log.empty());
  uint64_t watermark = result.log.rbegin()->first;
  EXPECT_GT(watermark, 20u);

  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, stale));
  ASSERT_EQ(0,
            link_olh_test_instance(ioctx, oid, stale, false, 5, "stale-link"));
  ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, watermark, olh_test_tag,
                                   result));
  ASSERT_EQ(1u, result.log.size());
  ASSERT_EQ(2u, result.log.begin()->second.size());
  EXPECT_GT(result.log.begin()->first, watermark);
  EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, result.log.begin()->second.front().op);
  EXPECT_EQ(stale, result.log.begin()->second.front().key);
  EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, result.log.begin()->second.back().op);
  EXPECT_EQ(head, result.log.begin()->second.back().key);
  EXPECT_EQ("stale-link", result.log.begin()->second.front().op_tag);
  EXPECT_EQ(result.log.begin()->first,
            result.log.begin()->second.front().epoch);
  watermark = result.log.rbegin()->first;

  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(
                   ioctx, oid, marker, "stale-unlink-marker", olh_test_tag, 5,
                   false, rgw_zone_set{}));
  ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, watermark, olh_test_tag,
                                   result));
  ASSERT_EQ(1u, result.log.size());
  ASSERT_EQ(1u, result.log.begin()->second.size());
  EXPECT_GT(result.log.begin()->first, watermark);
  EXPECT_EQ(CLS_RGW_OLH_OP_STALE, result.log.begin()->second.front().op);
  EXPECT_EQ("stale-unlink-marker", result.log.begin()->second.front().op_tag);
  watermark = result.log.rbegin()->first;

  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, stale,
                                              "stale-unlink-data", olh_test_tag,
                                              5, false, rgw_zone_set{}));
  ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, watermark, olh_test_tag,
                                   result));
  ASSERT_EQ(1u, result.log.size());
  ASSERT_EQ(1u, result.log.begin()->second.size());
  EXPECT_GT(result.log.begin()->first, watermark);
  EXPECT_EQ(CLS_RGW_OLH_OP_REMOVE_INSTANCE,
            result.log.begin()->second.front().op);
  EXPECT_EQ("stale-unlink-data", result.log.begin()->second.front().op_tag);
  watermark = result.log.rbegin()->first;

  // With an advancing local clock, a stale op after trimming is still
  // logged at the new local time above the previous read marker.
  ObjectWriteOperation trim;
  cls_rgw_trim_olh_log(trim, olh_key, watermark, olh_test_tag);
  ASSERT_EQ(0, ioctx.operate(oid, &trim));
  ASSERT_EQ(0,
            cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
  EXPECT_TRUE(result.log.empty());
  EXPECT_FALSE(result.is_truncated);
  cls_rgw_obj_key after_trim("obj", "after-trim");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, after_trim));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, after_trim, false, 5,
                                      "stale-after-trim"));
  ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, watermark, olh_test_tag,
                                   result));
  ASSERT_EQ(1u, result.log.size());
  ASSERT_EQ(2u, result.log.begin()->second.size());
  EXPECT_GT(result.log.begin()->first, watermark);
  EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, result.log.begin()->second.front().op);
  EXPECT_EQ(after_trim, result.log.begin()->second.front().key);
  EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, result.log.begin()->second.back().op);
  EXPECT_EQ(head, result.log.begin()->second.back().key);
  EXPECT_EQ("stale-after-trim", result.log.begin()->second.front().op_tag);

  rgw_bucket_olh_entry olh;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(head, olh.key);
  EXPECT_EQ(20u, olh.epoch);
}

TEST_F(cls_rgw, olh_unlink_promotes_original_epoch) {
  string oid = "olh-promotion-epoch";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key old("obj", "old");
  cls_rgw_obj_key head("obj", "head");
  cls_rgw_obj_key middle("obj", "middle");
  cls_rgw_obj_key olh_key("obj");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, old));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, old, false, 10, "link-old"));
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
  ASSERT_EQ(0,
            link_olh_test_instance(ioctx, oid, head, false, 20, "link-head"));
  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, head, "unlink-head",
                                              olh_test_tag, 30, false,
                                              rgw_zone_set{}));

  rgw_bucket_olh_entry olh;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(old, olh.key);
  EXPECT_EQ(10u, olh.epoch);
  rgw_bucket_dir_entry promoted;
  ASSERT_EQ(
      0, get_bi_test_entry(ioctx, oid, BIIndexType::Instance, old, promoted));
  EXPECT_EQ(10u, promoted.versioned_epoch);
  EXPECT_TRUE(promoted.is_current());

  // This version is newer than the promoted target but older than the
  // unlink. The unlink must not prevent it from becoming current.
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, middle));
  ASSERT_EQ(
      0, link_olh_test_instance(ioctx, oid, middle, false, 15, "link-middle"));
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(middle, olh.key);
  EXPECT_EQ(15u, olh.epoch);

  map<int, rgw_cls_list_ret> listing;
  list_entries(ioctx, oid, 10, listing);
  ASSERT_EQ(1u, listing.size());
  const auto &entries = listing.begin()->second.dir.m;
  ASSERT_EQ(2u, entries.size());
  EXPECT_EQ(middle, entries.begin()->second.key);
  EXPECT_EQ(10u, entries.rbegin()->second.versioned_epoch);
}

TEST_F(cls_rgw, olh_link_precondition_skipped_is_acknowledged) {
  string oid = "olh-precondition-ack";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key head("obj", "head");
  cls_rgw_obj_key olh_key("obj");
  const ceph::real_time mtime{ceph::timespan(1800000000123456789ULL)};
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head, mtime));
  ASSERT_EQ(0,
            link_olh_test_instance(ioctx, oid, head, false, 10, "link-head"));
  rgw_cls_read_olh_log_ret result;
  ASSERT_EQ(0,
            cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
  ASSERT_FALSE(result.log.empty());
  const uint64_t watermark = result.log.rbegin()->first;

  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, head, true, 20,
                                      "precondition", mtime));
  ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, watermark, olh_test_tag,
                                   result));
  ASSERT_EQ(1u, result.log.size());
  ASSERT_EQ(1u, result.log.begin()->second.size());
  EXPECT_EQ(CLS_RGW_OLH_OP_STALE, result.log.begin()->second.front().op);
  EXPECT_EQ("precondition", result.log.begin()->second.front().op_tag);
  rgw_bucket_olh_entry olh;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(head, olh.key);
  EXPECT_EQ(10u, olh.epoch);
  EXPECT_FALSE(olh.delete_marker);
}

TEST_F(cls_rgw, olh_link_precondition_skipped_plain_null_initializes_head) {
  for (const uint64_t epoch : {0ULL, 7ULL}) {
    SCOPED_TRACE(epoch);
    string oid = "olh-precondition-plain-null-" + to_string(epoch);
    ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
    cls_rgw_obj_key key("obj");
    rgw_bucket_dir_entry original;
    original.key = key;
    original.exists = true;
    original.meta.mtime =
        ceph::real_time{ceph::timespan(1530000000123456789ULL)};
    original.meta.size = original.meta.accounted_size = 4096;
    original.meta.etag = "existing-payload-etag";
    original.tag = "existing-write-tag";
    original.locator = "existing-locator";
    original.ver.pool = ioctx.get_id();
    original.ver.epoch = 42;
    original.versioned_epoch = epoch;
    if (epoch == 0) {
      ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, key.name,
                                     original));
    } else {
      original.flags =
          rgw_bucket_dir_entry::FLAG_VER | rgw_bucket_dir_entry::FLAG_CURRENT;
      rgw_bucket_dir_entry marker;
      marker.key = key;
      marker.flags = rgw_bucket_dir_entry::FLAG_VER_MARKER;
      ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, key.name,
                                     marker));
      const string instance_idx = string("\x80"
                                         "1000_",
                                         6) +
                                  key.name + string("\0i", 2);
      const string list_idx = key.name + string("\0v908\0i", 7);
      ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Instance,
                                     instance_idx, original));
      ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, list_idx,
                                     original));
    }
    rgw_cls_bi_entry missing;
    ASSERT_EQ(-ENOENT,
              cls_rgw_bi_get(ioctx, oid, BIIndexType::OLH, key, &missing));

    ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, key, true, 20,
                                        "skipped-null", original.meta.mtime));
    rgw_cls_read_olh_log_ret result;
    ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, key, 0, olh_test_tag, result));
    ASSERT_EQ(1u, result.log.size());
    const auto &log = result.log.begin()->second;
    ASSERT_EQ(2u, log.size());
    EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, log[0].op);
    EXPECT_EQ(key, log[0].key);
    EXPECT_FALSE(log[0].delete_marker);
    EXPECT_EQ(CLS_RGW_OLH_OP_STALE, log[1].op);
    for (const auto &entry : log) {
      EXPECT_EQ("skipped-null", entry.op_tag);
      EXPECT_EQ(result.log.begin()->first, entry.epoch);
    }

    const uint64_t expected_epoch =
        epoch ? epoch : original.meta.mtime.time_since_epoch().count();
    rgw_bucket_olh_entry olh;
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, key, olh));
    EXPECT_EQ(key, olh.key);
    EXPECT_EQ(olh_test_tag, olh.tag);
    EXPECT_EQ(expected_epoch, olh.epoch);
    EXPECT_TRUE(olh.exists);
    EXPECT_FALSE(olh.delete_marker);
    EXPECT_FALSE(olh.pending_removal);

    map<int, rgw_cls_list_ret> listing;
    list_entries(ioctx, oid, 10, listing);
    ASSERT_EQ(1u, listing.size());
    const auto &entries = listing.begin()->second.dir.m;
    ASSERT_EQ(1u, entries.size());
    const auto &preserved = entries.begin()->second;
    EXPECT_EQ(key, preserved.key);
    EXPECT_TRUE(preserved.exists);
    EXPECT_TRUE(preserved.is_current());
    EXPECT_FALSE(preserved.is_delete_marker());
    EXPECT_EQ(expected_epoch, preserved.versioned_epoch);
    EXPECT_EQ(original.meta.size, preserved.meta.size);
    EXPECT_EQ(original.meta.accounted_size, preserved.meta.accounted_size);
    EXPECT_EQ(original.meta.mtime, preserved.meta.mtime);
    EXPECT_EQ(original.meta.etag, preserved.meta.etag);
    EXPECT_EQ(original.tag, preserved.tag);
    EXPECT_EQ(original.locator, preserved.locator);
    EXPECT_EQ(original.ver.pool, preserved.ver.pool);
    EXPECT_EQ(original.ver.epoch, preserved.ver.epoch);

    // Legacy readers still receive the LINK that initializes the data OLH.
    ASSERT_EQ(0, read_olh_test_log(ioctx, oid, key, 0, result));
    ASSERT_EQ(1u, result.log.size());
    ASSERT_EQ(1u, result.log.begin()->second.size());
    EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, result.log.begin()->second.front().op);
  }
}

TEST_F(cls_rgw, olh_equal_epoch_noncurrent_link_is_acknowledged) {
  for (const bool delete_marker : {false, true}) {
    SCOPED_TRACE(delete_marker);
    string oid =
        delete_marker ? "olh-equal-epoch-marker" : "olh-equal-epoch-data";
    ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
    cls_rgw_obj_key head("obj", "a");
    cls_rgw_obj_key incoming("obj", "z");
    cls_rgw_obj_key olh_key("obj");
    ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
    ASSERT_EQ(0,
              link_olh_test_instance(ioctx, oid, head, false, 20, "link-head"));
    rgw_cls_read_olh_log_ret result;
    ASSERT_EQ(
        0, cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
    ASSERT_FALSE(result.log.empty());
    const uint64_t watermark = result.log.rbegin()->first;

    ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, incoming));
    ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, incoming, delete_marker, 20,
                                        "tie-loser"));
    ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, watermark,
                                     olh_test_tag, result));
    ASSERT_EQ(1u, result.log.size());
    const auto &log = result.log.begin()->second;
    ASSERT_EQ(2u, log.size());
    EXPECT_EQ(incoming, log.front().key);
    for (const auto &entry : log) {
      EXPECT_EQ("tie-loser", entry.op_tag);
      EXPECT_EQ(result.log.begin()->first, entry.epoch);
    }
    if (delete_marker) {
      EXPECT_EQ(CLS_RGW_OLH_OP_STALE, log.front().op);
      EXPECT_EQ(CLS_RGW_OLH_OP_REMOVE_INSTANCE, log.back().op);
      EXPECT_EQ(incoming, log.back().key);
    } else {
      EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, log.front().op);
      EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, log.back().op);
      EXPECT_EQ(head, log.back().key);
      EXPECT_FALSE(log.back().delete_marker);
    }

    rgw_bucket_olh_entry olh;
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
    EXPECT_EQ(head, olh.key);
    EXPECT_EQ(20u, olh.epoch);
    EXPECT_TRUE(olh.exists);
    EXPECT_FALSE(olh.delete_marker);
    rgw_bucket_dir_entry stored;
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::Instance, incoming,
                                   stored));
    EXPECT_EQ(20u, stored.versioned_epoch);
    EXPECT_FALSE(stored.is_current());
    EXPECT_EQ(delete_marker, stored.is_delete_marker());
    ASSERT_EQ(
        0, get_bi_test_entry(ioctx, oid, BIIndexType::Instance, head, stored));
    EXPECT_TRUE(stored.is_current());
    ASSERT_EQ(0, read_olh_test_log(ioctx, oid, olh_key, watermark, result));
    ASSERT_EQ(1u, result.log.size());
    ASSERT_EQ(delete_marker ? 1u : 2u, result.log.begin()->second.size());
    if (!delete_marker) {
      EXPECT_EQ(head, result.log.begin()->second.back().key);
    }
  }
}

TEST_F(cls_rgw, olh_noncurrent_data_relink_cancels_queued_removal) {
  for (const bool remove_current : {false, true}) {
    SCOPED_TRACE(remove_current);
    string oid =
        remove_current ? "olh-relink-former-head" : "olh-relink-noncurrent";
    ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
    cls_rgw_obj_key olh_key("obj");
    cls_rgw_obj_key b("obj", "B");
    cls_rgw_obj_key h("obj", "H");
    const uint64_t h_epoch = remove_current ? 10 : 20;
    const uint64_t b_epoch = remove_current ? 20 : 10;
    if (remove_current) {
      ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, h));
      ASSERT_EQ(
          0, link_olh_test_instance(ioctx, oid, h, false, h_epoch, "link-H"));
    }
    ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, b));
    ASSERT_EQ(0,
              link_olh_test_instance(ioctx, oid, b, false, b_epoch, "link-B"));
    if (!remove_current) {
      ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, h));
      ASSERT_EQ(
          0, link_olh_test_instance(ioctx, oid, h, false, h_epoch, "link-H"));
    }
    rgw_cls_read_olh_log_ret result;
    ASSERT_EQ(
        0, cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
    ASSERT_FALSE(result.log.empty());
    uint64_t marker = result.log.rbegin()->first;
    ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, b, "remove-B",
                                                olh_test_tag, b_epoch, false,
                                                rgw_zone_set{}));
    ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, marker, olh_test_tag,
                                     result));
    ASSERT_EQ(1u, result.log.size());
    ASSERT_FALSE(result.log.begin()->second.empty());
    EXPECT_EQ(CLS_RGW_OLH_OP_REMOVE_INSTANCE,
              result.log.begin()->second.back().op);
    EXPECT_EQ(b, result.log.begin()->second.back().key);
    const uint64_t removal_epoch = result.log.rbegin()->first;

    // Relink B before the pending removal is applied. Its stale remote epoch
    // must not discard restoration intent or make B the current head.
    ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, b, false, 5, "restore-B"));
    ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, removal_epoch,
                                     olh_test_tag, result));
    ASSERT_EQ(1u, result.log.size());
    const auto &links = result.log.begin()->second;
    ASSERT_EQ(2u, links.size());
    EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, links[0].op);
    EXPECT_EQ(b, links[0].key);
    EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, links[1].op);
    EXPECT_EQ(h, links[1].key);
    for (const auto &entry : links) {
      EXPECT_EQ("restore-B", entry.op_tag);
      EXPECT_EQ(result.log.begin()->first, entry.epoch);
      EXPECT_FALSE(entry.delete_marker);
    }
    rgw_bucket_olh_entry olh;
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
    EXPECT_EQ(h, olh.key);
    EXPECT_EQ(h_epoch, olh.epoch);
    rgw_bucket_dir_entry stored;
    ASSERT_EQ(0,
              get_bi_test_entry(ioctx, oid, BIIndexType::Instance, b, stored));
    EXPECT_EQ(5u, stored.versioned_epoch);
    EXPECT_FALSE(stored.is_current());
    map<int, rgw_cls_list_ret> listing;
    list_olh_test_versions(ioctx, oid, olh_key.name, 10, listing);
    ASSERT_EQ(1u, listing.size());
    ASSERT_EQ(2u, listing.begin()->second.dir.m.size());
    EXPECT_EQ(h, listing.begin()->second.dir.m.begin()->second.key);
    EXPECT_EQ(b, listing.begin()->second.dir.m.rbegin()->second.key);
    // The legacy read gate must retain both known LINK operations, in order.
    ASSERT_EQ(0, read_olh_test_log(ioctx, oid, olh_key, removal_epoch, result));
    ASSERT_EQ(1u, result.log.size());
    ASSERT_EQ(2u, result.log.begin()->second.size());
    EXPECT_EQ(b, result.log.begin()->second.front().key);
    EXPECT_EQ(h, result.log.begin()->second.back().key);
  }
}

TEST_F(cls_rgw, olh_noncurrent_data_relink_preserves_absent_head) {
  string oid = "olh-relink-absent-head";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key olh_key("obj");
  cls_rgw_obj_key b("obj", "B");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, b));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, b, false, 20, "link-B"));
  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, b, "remove-B",
                                              olh_test_tag, 20, false,
                                              rgw_zone_set{}));
  rgw_cls_read_olh_log_ret result;
  ASSERT_EQ(0,
            cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
  ASSERT_FALSE(result.log.empty());
  uint64_t marker = result.log.rbegin()->first;
  // Exercise both the older-epoch and accepted-equal-epoch nonpromotion paths.
  for (const uint64_t epoch : {5ULL, 20ULL}) {
    SCOPED_TRACE(epoch);
    ASSERT_EQ(0,
              link_olh_test_instance(ioctx, oid, b, false, epoch, "restore-B"));
    ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, marker, olh_test_tag,
                                     result));
    ASSERT_EQ(1u, result.log.size());
    const auto &log = result.log.begin()->second;
    ASSERT_EQ(2u, log.size());
    EXPECT_EQ(CLS_RGW_OLH_OP_LINK_OLH, log.front().op);
    EXPECT_EQ(b, log.front().key);
    EXPECT_EQ(CLS_RGW_OLH_OP_UNLINK_OLH, log.back().op);
    EXPECT_EQ(olh_key, log.back().key);
    EXPECT_EQ(log.front().epoch, log.back().epoch);
    rgw_bucket_olh_entry olh;
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
    EXPECT_EQ(olh_key, olh.key);
    EXPECT_EQ(20u, olh.epoch);
    EXPECT_FALSE(olh.exists);
    EXPECT_TRUE(olh.pending_removal);
    rgw_bucket_dir_entry stored;
    ASSERT_EQ(0,
              get_bi_test_entry(ioctx, oid, BIIndexType::Instance, b, stored));
    EXPECT_FALSE(stored.is_current());
    EXPECT_EQ(epoch, stored.versioned_epoch);
    marker = result.log.rbegin()->first;
  }
}

TEST_F(cls_rgw, olh_noncurrent_null_relink_under_absent_head_is_canceled) {
  string oid = "olh-relink-absent-null";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key key("obj");
  rgw_bucket_dir_entry marker;
  marker.key = key;
  marker.flags = rgw_bucket_dir_entry::FLAG_VER_MARKER;
  ASSERT_EQ(
      0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, key.name, marker));
  rgw_bucket_dir_entry data;
  data.key = key;
  data.exists = true;
  data.flags = rgw_bucket_dir_entry::FLAG_VER;
  data.versioned_epoch = 5;
  data.meta.etag = "null-payload";
  const string instance_idx = string("\x80"
                                     "1000_",
                                     6) +
                              key.name + string("\0i", 2);
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Instance,
                                 instance_idx, data));
  rgw_bucket_olh_entry olh;
  olh.key = key;
  olh.epoch = 20;
  olh.tag = olh_test_tag;
  olh.pending_removal = true;
  bufferlist before;
  encode(olh, before);
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                 olh_test_index_key(key.name), olh));

  EXPECT_EQ(-ECANCELED,
            link_olh_test_instance(ioctx, oid, key, false, 5, "restore-null"));
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, key, olh));
  bufferlist after;
  encode(olh, after);
  EXPECT_EQ(before.to_str(), after.to_str());
  rgw_bucket_dir_entry unchanged;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::Plain,
                                 cls_rgw_obj_key(instance_idx), unchanged));
  EXPECT_EQ(data.meta.etag, unchanged.meta.etag);
  EXPECT_EQ(data.versioned_epoch, unchanged.versioned_epoch);
  EXPECT_TRUE(unchanged.exists);
  EXPECT_FALSE(unchanged.is_current());
}

TEST_F(cls_rgw, olh_read_log_stale_pagination) {
  string oid = "olh-stale-pagination";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key olh_key("obj");
  rgw_bucket_olh_entry olh;
  olh.key = cls_rgw_obj_key("obj", "head");
  olh.tag = olh_test_tag;
  olh.exists = true;
  for (uint64_t epoch = 1; epoch <= 3003; ++epoch) {
    rgw_bucket_olh_log_entry entry;
    entry.epoch = epoch;
    entry.op = (epoch > 1001 && epoch <= 2002) ? CLS_RGW_OLH_OP_LINK_OLH
                                               : CLS_RGW_OLH_OP_STALE;
    entry.op_tag = to_string(epoch);
    entry.key = olh.key;
    olh.pending_log[epoch].push_back(entry);
  }
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                 olh_test_index_key(olh_key.name), olh));

  rgw_cls_read_olh_log_ret result;
  ASSERT_EQ(0, read_olh_test_log(ioctx, oid, olh_key, 0, result));
  ASSERT_EQ(1000u, result.log.size());
  EXPECT_EQ(1002u, result.log.begin()->first);
  EXPECT_EQ(2001u, result.log.rbegin()->first);
  EXPECT_TRUE(result.is_truncated);
  ASSERT_EQ(0, read_olh_test_log(ioctx, oid, olh_key, 2001, result));
  ASSERT_EQ(1u, result.log.size());
  EXPECT_EQ(2002u, result.log.begin()->first);
  EXPECT_FALSE(result.is_truncated);
  ASSERT_EQ(0, read_olh_test_log(ioctx, oid, olh_key, 2002, result));
  EXPECT_TRUE(result.log.empty());
  EXPECT_FALSE(result.is_truncated);

  // The upgraded client sees all groups and retains the 1000-group limit.
  uint64_t marker = 0;
  size_t count = 0;
  do {
    ASSERT_EQ(0, cls_rgw_get_olh_log(ioctx, oid, olh_key, marker, olh_test_tag,
                                     result));
    ASSERT_FALSE(result.log.empty());
    EXPECT_LE(result.log.size(), 1000u);
    ASSERT_GT(result.log.begin()->first, marker);
    count += result.log.size();
    marker = result.log.rbegin()->first;
  } while (result.is_truncated);
  EXPECT_EQ(3003u, count);
}

TEST_F(cls_rgw, olh_legacy_counter_upgrade) {
  string oid = "olh-legacy-counters";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key old("obj", "old");
  cls_rgw_obj_key head("obj", "head");
  cls_rgw_obj_key olh_key("obj");
  rgw_bucket_dir_entry legacy;
  legacy.key = old;
  legacy.exists = true;
  legacy.flags =
      rgw_bucket_dir_entry::FLAG_VER | rgw_bucket_dir_entry::FLAG_CURRENT;
  legacy.versioned_epoch = 2;
  const string instance_idx = string("\x80"
                                     "1000_",
                                     6) +
                              old.name + string("\0i", 2) + old.instance;
  const string list_idx = old.name + string("\0v913\0i", 7) + old.instance;
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Instance,
                                 instance_idx, legacy));
  ASSERT_EQ(
      0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, list_idx, legacy));
  rgw_bucket_olh_entry olh;
  olh.key = old;
  olh.epoch = 2;
  olh.tag = olh_test_tag;
  olh.exists = true;
  rgw_bucket_olh_log_entry log_entry;
  log_entry.epoch = 2;
  log_entry.op = CLS_RGW_OLH_OP_LINK_OLH;
  log_entry.op_tag = "legacy";
  log_entry.key = old;
  olh.pending_log[2].push_back(log_entry);
  // Store the original v1 Reef encoding with its small counter epochs.
  rgw_cls_bi_entry old_record;
  old_record.type = BIIndexType::OLH;
  old_record.idx = olh_test_index_key(olh_key.name);
  {
    auto &bl = old_record.data;
    ENCODE_START(1, 1, bl);
    encode(olh.key, bl);
    encode(olh.delete_marker, bl);
    encode(olh.epoch, bl);
    encode(olh.pending_log, bl);
    encode(olh.tag, bl);
    encode(olh.exists, bl);
    encode(olh.pending_removal, bl);
    ENCODE_FINISH(bl);
  }
  ObjectWriteOperation put;
  cls_rgw_bi_put(put, oid, old_record);
  ASSERT_EQ(0, ioctx.operate(oid, &put));
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(2u, olh.epoch);

  rgw_cls_read_olh_log_ret result;
  ASSERT_EQ(0,
            cls_rgw_get_olh_log(ioctx, oid, olh_key, 0, olh_test_tag, result));
  ASSERT_EQ(1u, result.log.size());
  EXPECT_EQ(2u, result.log.begin()->first);
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head,
                                     ceph::real_time{ceph::timespan(1)}));
  ASSERT_EQ(0,
            link_olh_test_instance(ioctx, oid, head, false, 0, "local-link"));
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(head, olh.key);
  EXPECT_GT(olh.epoch, 0x1000000000000ULL);
  ASSERT_FALSE(olh.pending_log.empty());
  EXPECT_EQ(2u, olh.pending_log.begin()->first);
  EXPECT_GT(olh.pending_log.rbegin()->first, 2u);

  map<int, rgw_cls_list_ret> listing;
  list_entries(ioctx, oid, 10, listing);
  ASSERT_EQ(1u, listing.size());
  const auto &entries = listing.begin()->second.dir.m;
  ASSERT_EQ(2u, entries.size());
  ASSERT_EQ(1u, entries.count(list_idx));
  EXPECT_EQ(2u, entries.at(list_idx).versioned_epoch);
  EXPECT_EQ(head, entries.begin()->second.key);
  EXPECT_EQ(olh.epoch, entries.begin()->second.versioned_epoch);
}

TEST_F(cls_rgw, olh_legacy_wide_counter_upgrade) {
  for (const bool link_first : {false, true}) {
    SCOPED_TRACE(link_first);
    string oid =
        link_first ? "olh-multi-legacy-link" : "olh-multi-legacy-unlink";
    ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
    cls_rgw_obj_key olh_key("obj");
    cls_rgw_obj_key head("obj", "H");
    rgw_bucket_dir_entry a;
    a.key = cls_rgw_obj_key("obj", "A");
    a.exists = true;
    a.flags =
        rgw_bucket_dir_entry::FLAG_VER | rgw_bucket_dir_entry::FLAG_CURRENT;
    a.versioned_epoch = 6000000000ULL;
    a.meta.etag = "A-payload";
    rgw_bucket_dir_entry b = a;
    b.key.instance = "B";
    b.flags &= ~rgw_bucket_dir_entry::FLAG_CURRENT;
    b.versioned_epoch = 5000000000ULL;
    b.meta.etag = "B-authoritative-payload";
    const string a_idx = a.key.name + string("\0v", 2) +
                         "4-0000000006000000000" + string("\0i", 2) +
                         a.key.instance;
    const string b_idx = b.key.name + string("\0v", 2) +
                         "4-0000000005000000000" + string("\0i", 2) +
                         b.key.instance;
    ASSERT_EQ(0, put_olh_test_version(ioctx, oid, a, a_idx));
    ASSERT_EQ(0, put_olh_test_version(ioctx, oid, b, b_idx));
    // A partially migrated duplicate must not override the instance entry's
    // noncurrent flag or metadata when the whole history is normalized.
    const string b_current_idx = b.key.name + string("\0v", 2) +
                                 "4"
                                 "001094511627775" +
                                 string("\0i", 2) + b.key.instance;
    rgw_bucket_dir_entry obsolete = b;
    obsolete.flags |= rgw_bucket_dir_entry::FLAG_CURRENT;
    obsolete.meta.etag = "obsolete-list-copy";
    ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain,
                                   b_current_idx, obsolete));
    rgw_bucket_olh_entry olh;
    olh.key = a.key;
    olh.epoch = a.versioned_epoch;
    olh.tag = olh_test_tag;
    olh.exists = true;
    ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                   olh_test_index_key(olh_key.name), olh));

    // Similar names and a different encoded namespace share the bucket,
    // but neither belongs to the exact object's binary list-key prefix.
    vector<string> untouched;
    for (const string &name : {string("obj-neighbor"), string("_shadow_obj")}) {
      rgw_bucket_dir_entry other = a;
      other.key.name = name;
      const string idx = name + string("\0v", 2) + "4-0000000006000000000" +
                         string("\0i", 2) + other.key.instance;
      ASSERT_EQ(0, put_olh_test_version(ioctx, oid, other, idx));
      untouched.push_back(idx);
    }
    map<int, rgw_cls_list_ret> listing;
    list_olh_test_versions(ioctx, oid, olh_key.name, 10, listing);
    ASSERT_EQ(1u, listing.size());
    ASSERT_EQ(3u, listing.begin()->second.dir.m.size());
    EXPECT_EQ(1u, listing.begin()->second.dir.m.count(a_idx));
    EXPECT_EQ(1u, listing.begin()->second.dir.m.count(b_idx));

    if (link_first) {
      ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
      ASSERT_EQ(0,
                link_olh_test_instance(ioctx, oid, head, false, 0, "link-H"));
      listing.clear();
      list_olh_test_versions(ioctx, oid, olh_key.name, 10, listing);
      ASSERT_EQ(1u, listing.size());
      const auto &entries = listing.begin()->second.dir.m;
      ASSERT_EQ(3u, entries.size());
      auto next = entries.begin();
      EXPECT_EQ(head, (next++)->second.key);
      EXPECT_EQ(a.key, (next++)->second.key);
      EXPECT_EQ(b.key, next->second.key);
      EXPECT_EQ(b.meta.etag, next->second.meta.etag);
      EXPECT_FALSE(next->second.is_current());
      ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, head, "unlink-H",
                                                  olh_test_tag, 0, false,
                                                  rgw_zone_set{}));
      ASSERT_EQ(0,
                get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
      EXPECT_EQ(a.key, olh.key);
      EXPECT_EQ(a.versioned_epoch, olh.epoch);
    }
    // Whether the first mutation is LINK(H) or UNLINK(A), B must never
    // outrank A during migration, and remains reachable after A is deleted.
    ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, a.key, "unlink-A",
                                                olh_test_tag, 0, false,
                                                rgw_zone_set{}));
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
    EXPECT_EQ(b.key, olh.key);
    EXPECT_EQ(b.versioned_epoch, olh.epoch);
    rgw_bucket_dir_entry stored;
    ASSERT_EQ(
        0, get_bi_test_entry(ioctx, oid, BIIndexType::Instance, b.key, stored));
    EXPECT_TRUE(stored.is_current());
    EXPECT_EQ(b.meta.etag, stored.meta.etag);
    listing.clear();
    list_olh_test_versions(ioctx, oid, olh_key.name, 10, listing);
    ASSERT_EQ(1u, listing.size());
    ASSERT_EQ(1u, listing.begin()->second.dir.m.size());
    EXPECT_EQ(b.key, listing.begin()->second.dir.m.begin()->second.key);
    rgw_cls_bi_entry raw;
    EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                      cls_rgw_obj_key(a_idx), &raw));
    EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                      cls_rgw_obj_key(b_idx), &raw));
    for (const auto &idx : untouched) {
      EXPECT_EQ(0, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                  cls_rgw_obj_key(idx), &raw));
    }
  }
}

TEST_F(cls_rgw, olh_legacy_wide_normalization_pages) {
  string oid = "olh-legacy-normalization-pages";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key olh_key("obj");
  constexpr unsigned count = 140; // exceeds the normalization scan page
  for (unsigned i = 0; i < count; ++i) {
    rgw_bucket_dir_entry entry;
    entry.key = cls_rgw_obj_key("obj", to_string(i));
    entry.exists = true;
    entry.flags = rgw_bucket_dir_entry::FLAG_VER;
    if (i + 1 == count) {
      entry.flags |= rgw_bucket_dir_entry::FLAG_CURRENT;
    }
    entry.versioned_epoch = 5000000000ULL + i;
    char suffix[32];
    snprintf(suffix, sizeof(suffix), "4%020lld",
             -static_cast<long long>(entry.versioned_epoch));
    const string idx = entry.key.name + string("\0v", 2) + suffix +
                       string("\0i", 2) + entry.key.instance;
    ASSERT_EQ(0, put_olh_test_version(ioctx, oid, entry, idx));
  }
  rgw_bucket_olh_entry olh;
  olh.key = cls_rgw_obj_key("obj", to_string(count - 1));
  olh.epoch = 5000000000ULL + count - 1;
  olh.exists = true;
  olh.tag = olh_test_tag;
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                 olh_test_index_key(olh_key.name), olh));
  cls_rgw_obj_key head("obj", "H");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, head, false, 0, "link-H"));
  // Collect bounded read pages too, including configurations whose omap
  // request limit is smaller than the CLS normalization/read page sizes.
  vector<rgw_bucket_dir_entry> ordered;
  cls_rgw_obj_key start;
  map<int, string> oids = {{0, oid}};
  const string prefix = olh_key.name + string("\0", 1);
  string delimiter;
  for (unsigned page = 0; page < count + 2; ++page) {
    map<int, rgw_cls_list_ret> listing;
    ASSERT_EQ(0, CLSRGWIssueBucketList(ioctx, start, prefix, delimiter, 64,
                                       true, oids, listing, 1)());
    ASSERT_EQ(1u, listing.size());
    const auto &result = listing.begin()->second;
    for (const auto &[idx, entry] : result.dir.m) {
      ordered.push_back(entry);
    }
    ASSERT_LE(ordered.size(), count + 1u);
    if (!result.is_truncated) {
      break;
    }
    ASSERT_FALSE(result.dir.m.empty());
    ASSERT_NE(start, result.dir.m.rbegin()->second.key);
    start = result.dir.m.rbegin()->second.key;
  }
  ASSERT_EQ(count + 1u, ordered.size());
  auto next = ordered.begin();
  EXPECT_EQ(head, (next++)->key);
  for (unsigned i = count; i > 0; --i, ++next) {
    EXPECT_EQ(to_string(i - 1), next->key.instance);
    EXPECT_EQ(5000000000ULL + i - 1, next->versioned_epoch);
    EXPECT_FALSE(next->is_current());
  }
  ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, head, "unlink-H",
                                              olh_test_tag, 0, false,
                                              rgw_zone_set{}));
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
  EXPECT_EQ(cls_rgw_obj_key("obj", to_string(count - 1)), olh.key);
  EXPECT_EQ(5000000000ULL + count - 1, olh.epoch);
}

TEST_F(cls_rgw, olh_legacy_normalization_uses_instance_state) {
  string oid = "olh-legacy-instance-authority";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key olh_key("obj");
  rgw_bucket_dir_entry live;
  live.key = cls_rgw_obj_key("obj", "live");
  live.exists = true;
  live.flags =
      rgw_bucket_dir_entry::FLAG_VER | rgw_bucket_dir_entry::FLAG_CURRENT;
  live.versioned_epoch = 6000000000ULL;
  live.meta.etag = "authoritative";
  const string live_idx = live.key.name + string("\0v", 2) +
                          "4"
                          "001093511627775" +
                          string("\0i", 2) + live.key.instance;
  ASSERT_EQ(0, put_olh_test_version(ioctx, oid, live, live_idx));
  rgw_bucket_dir_entry obsolete = live;
  obsolete.meta.etag = "obsolete-canonical-copy";
  ASSERT_EQ(
      0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, live_idx, obsolete));

  rgw_bucket_dir_entry orphan = live;
  orphan.key.instance = "orphan";
  const string orphan_legacy_idx = orphan.key.name + string("\0v", 2) +
                                   "4-0000000006000000000" + string("\0i", 2) +
                                   orphan.key.instance;
  const string orphan_current_idx = orphan.key.name + string("\0v", 2) +
                                    "4"
                                    "001093511627775" +
                                    string("\0i", 2) + orphan.key.instance;
  // No instance entry exists for either obsolete orphan listing.
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain,
                                 orphan_legacy_idx, orphan));
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain,
                                 orphan_current_idx, orphan));
  rgw_bucket_olh_entry olh;
  olh.key = live.key;
  olh.epoch = live.versioned_epoch;
  olh.tag = olh_test_tag;
  olh.exists = true;
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                 olh_test_index_key(olh_key.name), olh));
  cls_rgw_obj_key head("obj", "H");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, head, false, 0, "link-H"));
  rgw_cls_bi_entry raw;
  EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                    cls_rgw_obj_key(orphan_legacy_idx), &raw));
  EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                    cls_rgw_obj_key(orphan_current_idx), &raw));
  map<int, rgw_cls_list_ret> listing;
  list_olh_test_versions(ioctx, oid, olh_key.name, 10, listing);
  ASSERT_EQ(1u, listing.size());
  const auto &entries = listing.begin()->second.dir.m;
  ASSERT_EQ(2u, entries.size());
  EXPECT_EQ(head, entries.begin()->second.key);
  EXPECT_EQ(live.key, entries.rbegin()->second.key);
  EXPECT_EQ(live.meta.etag, entries.rbegin()->second.meta.etag);
  EXPECT_FALSE(entries.rbegin()->second.is_current());
}

static void test_olh_orphan_null_variant(IoCtx &ioctx,
                                         bool live_delete_marker) {
  string oid =
      live_delete_marker ? "olh-orphan-null-data" : "olh-orphan-null-marker";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  cls_rgw_obj_key key("obj");
  rgw_bucket_dir_entry live;
  live.key = key;
  live.exists = !live_delete_marker;
  live.flags =
      rgw_bucket_dir_entry::FLAG_VER | rgw_bucket_dir_entry::FLAG_CURRENT;
  if (live_delete_marker) {
    live.flags |= rgw_bucket_dir_entry::FLAG_DELETE_MARKER;
  }
  live.versioned_epoch = 6000000000ULL;
  live.meta.mtime = ceph::real_time{ceph::timespan(1530000000123456789ULL)};
  live.meta.size = live.meta.accounted_size = live_delete_marker ? 0 : 4096;
  live.meta.etag = live_delete_marker ? "live-null-marker" : "live-null-data";
  live.tag = "authoritative-null-tag";
  live.ver.pool = ioctx.get_id();
  live.ver.epoch = 42;
  const string base_instance_idx = string("\x80"
                                          "1000_",
                                          6) +
                                   key.name + string("\0i", 2);
  const string dm_suffix("\0d", 2);
  const string live_instance_idx =
      base_instance_idx + (live_delete_marker ? dm_suffix : string());
  const string orphan_instance_idx =
      base_instance_idx + (live_delete_marker ? string() : dm_suffix);
  const string current_idx = key.name + string("\0v", 2) +
                             "4"
                             "001093511627775" +
                             string("\0i", 2);
  const string legacy_idx =
      key.name + string("\0v", 2) + "4-0000000006000000000" + string("\0i", 2);
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Instance,
                                 live_instance_idx, live));
  ASSERT_EQ(
      0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, current_idx, live));
  rgw_bucket_dir_entry orphan = live;
  orphan.flags ^= rgw_bucket_dir_entry::FLAG_DELETE_MARKER;
  orphan.exists = live_delete_marker;
  orphan.meta.etag = "obsolete-opposite-null-variant";
  ASSERT_EQ(
      0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, legacy_idx, orphan));
  rgw_cls_bi_entry raw;
  ASSERT_EQ(-ENOENT,
            cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                           cls_rgw_obj_key(orphan_instance_idx), &raw));
  rgw_bucket_olh_entry olh;
  olh.key = key;
  olh.epoch = live.versioned_epoch;
  olh.tag = olh_test_tag;
  olh.exists = true;
  olh.delete_marker = live_delete_marker;
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                 olh_test_index_key(key.name), olh));

  // A different, non-promoting numbered SID triggers normalization without
  // rewriting the live null listing and masking deletion of its cached row.
  cls_rgw_obj_key incoming("obj", "numbered-incoming");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, incoming));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, incoming, false, 5,
                                      "link-numbered"));
  EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                    cls_rgw_obj_key(legacy_idx), &raw));
  rgw_bucket_dir_entry preserved;
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::Plain,
                                 cls_rgw_obj_key(current_idx), preserved));
  bufferlist expected, actual;
  encode(live, expected);
  encode(preserved, actual);
  EXPECT_EQ(expected.to_str(), actual.to_str());
  ASSERT_EQ(0,
            get_bi_test_entry(ioctx, oid, BIIndexType::Plain,
                              cls_rgw_obj_key(live_instance_idx), preserved));
  actual.clear();
  encode(preserved, actual);
  EXPECT_EQ(expected.to_str(), actual.to_str());
  ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, key, olh));
  EXPECT_EQ(key, olh.key);
  EXPECT_EQ(live.versioned_epoch, olh.epoch);
  EXPECT_TRUE(olh.exists);
  EXPECT_EQ(live_delete_marker, olh.delete_marker);
  map<int, rgw_cls_list_ret> listing;
  list_olh_test_versions(ioctx, oid, key.name, 10, listing);
  ASSERT_EQ(1u, listing.size());
  const auto &entries = listing.begin()->second.dir.m;
  ASSERT_EQ(2u, entries.size());
  const auto &current = entries.begin()->second;
  EXPECT_EQ(key, current.key);
  EXPECT_TRUE(current.is_current());
  EXPECT_EQ(live_delete_marker, current.is_delete_marker());
  EXPECT_EQ(live.versioned_epoch, current.versioned_epoch);
  EXPECT_EQ(live.meta.etag, current.meta.etag);
  EXPECT_EQ(incoming, entries.rbegin()->second.key);
  EXPECT_FALSE(entries.rbegin()->second.is_current());
}

TEST_F(cls_rgw, olh_legacy_orphan_null_marker_preserves_null_data) {
  ASSERT_NO_FATAL_FAILURE(test_olh_orphan_null_variant(ioctx, false));
}

TEST_F(cls_rgw, olh_legacy_orphan_null_data_preserves_null_marker) {
  ASSERT_NO_FATAL_FAILURE(test_olh_orphan_null_variant(ioctx, true));
}

TEST_F(cls_rgw, olh_legacy_counter_cursor_and_duplicate_skip) {
  // Actual Reef encodings include both signed-negative wide counters and
  // signed-positive suffixes after the original uint64 negation wraps.
  const vector<pair<uint64_t, string>> cases = {
      {std::numeric_limits<uint64_t>::max(), "4"
                                             "00000000000000000001"},
      {uint64_t{1} << 63, "4-9223372036854775808"},
      {1800000000123456789ULL, "4-1800000000123456789"},
  };
  for (const auto &[epoch, suffix] : cases) {
    SCOPED_TRACE(epoch);
    string oid = "olh-legacy-cursor-" + to_string(epoch);
    ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
    const bool duplicate = epoch == 1800000000123456789ULL;
    cls_rgw_obj_key olh_key("obj");
    rgw_bucket_dir_entry older;
    older.key = cls_rgw_obj_key("obj", "older");
    older.exists = true;
    older.flags = rgw_bucket_dir_entry::FLAG_VER;
    older.versioned_epoch = 1;
    const string older_idx =
        older.key.name + string("\0v914\0i", 7) + older.key.instance;
    ASSERT_EQ(0, put_olh_test_version(ioctx, oid, older, older_idx));

    rgw_bucket_dir_entry legacy = older;
    legacy.key = cls_rgw_obj_key("obj", "wide");
    legacy.flags |= rgw_bucket_dir_entry::FLAG_CURRENT;
    legacy.versioned_epoch = epoch;
    const string legacy_idx = legacy.key.name + string("\0v", 2) + suffix +
                              string("\0i", 2) + legacy.key.instance;
    ASSERT_EQ(0, put_olh_test_version(ioctx, oid, legacy, legacy_idx));
    string current_idx;
    if (duplicate) {
      current_idx = legacy.key.name + string("\0v", 2) +
                    "2"
                    "16646744073586094826" +
                    string("\0i", 2) + legacy.key.instance;
      ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain,
                                     current_idx, legacy));
    }
    rgw_bucket_olh_entry olh;
    olh.key = legacy.key;
    olh.epoch = epoch;
    olh.tag = olh_test_tag;
    olh.exists = true;
    ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::OLH,
                                   olh_test_index_key(olh_key.name), olh));

    map<int, rgw_cls_list_ret> listing;
    list_entries(ioctx, oid, 10, listing);
    ASSERT_EQ(1u, listing.size());
    ASSERT_EQ(duplicate ? 3u : 2u, listing.begin()->second.dir.m.size());
    ASSERT_EQ(1u, listing.begin()->second.dir.m.count(legacy_idx));
    // Use the actual legacy cursor when it is the only key; when both
    // formats exist, skip the obsolete duplicate rather than repeat the SID.
    listing.clear();
    map<int, string> oids = {{0, oid}};
    string empty_prefix, empty_delimiter;
    ASSERT_EQ(0, CLSRGWIssueBucketList(ioctx, legacy.key, empty_prefix,
                                       empty_delimiter, 1, true, oids, listing,
                                       1)());
    ASSERT_EQ(1u, listing.size());
    ASSERT_EQ(1u, listing.begin()->second.dir.m.size());
    EXPECT_EQ(older.key, listing.begin()->second.dir.m.begin()->second.key);

    // For the duplicate case, the legacy copy sorts after the current-format
    // cursor. Promotion must skip that same SID and select the older version.
    ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(ioctx, oid, legacy.key,
                                                "unlink-wide", olh_test_tag,
                                                epoch, false, rgw_zone_set{}));
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
    EXPECT_EQ(older.key, olh.key);
    EXPECT_EQ(older.versioned_epoch, olh.epoch);
    rgw_cls_bi_entry raw;
    EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                      cls_rgw_obj_key(legacy_idx), &raw));
    if (duplicate) {
      EXPECT_EQ(-ENOENT, cls_rgw_bi_get(ioctx, oid, BIIndexType::Plain,
                                        cls_rgw_obj_key(current_idx), &raw));
    }
    listing.clear();
    list_entries(ioctx, oid, 10, listing);
    ASSERT_EQ(1u, listing.size());
    ASSERT_EQ(1u, listing.begin()->second.dir.m.size());
    EXPECT_EQ(older.key, listing.begin()->second.dir.m.begin()->second.key);
    EXPECT_TRUE(listing.begin()->second.dir.m.begin()->second.is_current());

    // The data instance is retained until REMOVE_INSTANCE is applied. A
    // repeated unlink must tolerate both list-key candidates being absent.
    ASSERT_EQ(0, cls_rgw_bucket_unlink_instance(
                     ioctx, oid, legacy.key, "unlink-wide-again", olh_test_tag,
                     epoch, false, rgw_zone_set{}));
    ASSERT_EQ(0, get_bi_test_entry(ioctx, oid, BIIndexType::OLH, olh_key, olh));
    EXPECT_EQ(older.key, olh.key);
    EXPECT_EQ(older.versioned_epoch, olh.epoch);
  }
}

TEST_F(cls_rgw, olh_large_epoch_list_keys) {
  string oid = "olh-large-epoch-keys";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  const vector<uint64_t> epochs = {2,
                                   15,
                                   16,
                                   255,
                                   256,
                                   4095,
                                   4096,
                                   65535,
                                   65536,
                                   0xFFFFFFFFULL,
                                   0x100000000ULL,
                                   0xFFFFFFFFFFULL,
                                   0x10000000000ULL,
                                   0xFFFFFFFFFFFFULL,
                                   0x1000000000000ULL,
                                   1800000000123456789ULL,
                                   std::numeric_limits<uint64_t>::max() - 1,
                                   std::numeric_limits<uint64_t>::max()};
  for (const uint64_t epoch : epochs) {
    cls_rgw_obj_key key("obj", to_string(epoch));
    ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, key));
    ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, key, false, epoch,
                                        "link-" + key.instance));
  }
  map<int, rgw_cls_list_ret> listing;
  list_entries(ioctx, oid, epochs.size() + 1, listing);
  ASSERT_EQ(1u, listing.size());
  const auto &entries = listing.begin()->second.dir.m;
  ASSERT_EQ(epochs.size(), entries.size());
  auto expected = epochs.rbegin();
  for (const auto &[idx, entry] : entries) {
    EXPECT_EQ(*expected, entry.versioned_epoch);
    EXPECT_EQ(to_string(*expected), entry.key.instance);
    ++expected;
  }
  // Small legacy keys remain byte-for-byte unchanged.
  EXPECT_EQ(1u, entries.count(string("obj\0v913\0i2", 11)));
}

TEST_F(cls_rgw, olh_plain_entry_conversion_uses_mtime_epoch) {
  string oid = "olh-plain-conversion";
  ASSERT_EQ(0, init_olh_test_index(ioctx, oid));
  rgw_bucket_dir_entry plain;
  plain.key = cls_rgw_obj_key("obj");
  plain.exists = true;
  plain.meta.mtime = ceph::real_time{ceph::timespan(1530000000123456789ULL)};
  ASSERT_EQ(0, put_bi_test_entry(ioctx, oid, BIIndexType::Plain, plain.key.name,
                                 plain));

  cls_rgw_obj_key head("obj", "head");
  ASSERT_EQ(0, put_olh_test_instance(ioctx, oid, head));
  ASSERT_EQ(0, link_olh_test_instance(ioctx, oid, head, false, 0, "link-head"));
  map<int, rgw_cls_list_ret> listing;
  list_entries(ioctx, oid, 10, listing);
  ASSERT_EQ(1u, listing.size());
  const auto &entries = listing.begin()->second.dir.m;
  ASSERT_EQ(2u, entries.size());
  EXPECT_EQ(head, entries.begin()->second.key);
  EXPECT_GT(entries.begin()->second.versioned_epoch,
            plain.meta.mtime.time_since_epoch().count());
  const auto &converted = entries.rbegin()->second;
  EXPECT_TRUE(converted.key.instance.empty());
  EXPECT_EQ(plain.meta.mtime.time_since_epoch().count(),
            converted.versioned_epoch);
  EXPECT_FALSE(converted.is_current());
}

/* test garbage collection */
static void create_obj(cls_rgw_obj& obj, int i, int j)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "-%d.%d", i, j);
  obj.pool = "pool";
  obj.pool.append(buf);
  obj.key.name = "oid";
  obj.key.name.append(buf);
  obj.loc = "loc";
  obj.loc.append(buf);
}

static bool cmp_objs(cls_rgw_obj& obj1, cls_rgw_obj& obj2)
{
  return (obj1.pool == obj2.pool) &&
         (obj1.key == obj2.key) &&
         (obj1.loc == obj2.loc);
}


TEST_F(cls_rgw, gc_set)
{
  /* add chains */
  string oid = "obj";
  for (int i = 0; i < 10; i++) {
    char buf[32];
    snprintf(buf, sizeof(buf), "chain-%d", i);
    string tag = buf;
    librados::ObjectWriteOperation op;
    cls_rgw_gc_obj_info info;

    cls_rgw_obj obj1, obj2;
    create_obj(obj1, i, 1);
    create_obj(obj2, i, 2);
    info.chain.objs.push_back(obj1);
    info.chain.objs.push_back(obj2);

    op.create(false); // create object

    info.tag = tag;
    cls_rgw_gc_set_entry(op, 0, info);

    ASSERT_EQ(0, ioctx.operate(oid, &op));
  }

  bool truncated;
  list<cls_rgw_gc_obj_info> entries;
  string marker;
  string next_marker;

  /* list chains, verify truncated */
  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 8, true, entries, &truncated, next_marker));
  ASSERT_EQ(8, (int)entries.size());
  ASSERT_EQ(1, truncated);

  entries.clear();
  next_marker.clear();

  /* list all chains, verify not truncated */
  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 10, true, entries, &truncated, next_marker));
  ASSERT_EQ(10, (int)entries.size());
  ASSERT_EQ(0, truncated);
 
  /* verify all chains are valid */
  list<cls_rgw_gc_obj_info>::iterator iter = entries.begin();
  for (int i = 0; i < 10; i++, ++iter) {
    cls_rgw_gc_obj_info& entry = *iter;

    /* create expected chain name */
    char buf[32];
    snprintf(buf, sizeof(buf), "chain-%d", i);
    string tag = buf;

    /* verify chain name as expected */
    ASSERT_EQ(entry.tag, tag);

    /* verify expected num of objects in chain */
    ASSERT_EQ(2, (int)entry.chain.objs.size());

    list<cls_rgw_obj>::iterator oiter = entry.chain.objs.begin();
    cls_rgw_obj obj1, obj2;

    /* create expected objects */
    create_obj(obj1, i, 1);
    create_obj(obj2, i, 2);

    /* assign returned object names */
    cls_rgw_obj& ret_obj1 = *oiter++;
    cls_rgw_obj& ret_obj2 = *oiter;

    /* verify objects are as expected */
    ASSERT_EQ(1, (int)cmp_objs(obj1, ret_obj1));
    ASSERT_EQ(1, (int)cmp_objs(obj2, ret_obj2));
  }
}

TEST_F(cls_rgw, gc_list)
{
  /* add chains */
  string oid = "obj";
  for (int i = 0; i < 10; i++) {
    char buf[32];
    snprintf(buf, sizeof(buf), "chain-%d", i);
    string tag = buf;
    librados::ObjectWriteOperation op;
    cls_rgw_gc_obj_info info;

    cls_rgw_obj obj1, obj2;
    create_obj(obj1, i, 1);
    create_obj(obj2, i, 2);
    info.chain.objs.push_back(obj1);
    info.chain.objs.push_back(obj2);

    op.create(false); // create object

    info.tag = tag;
    cls_rgw_gc_set_entry(op, 0, info);

    ASSERT_EQ(0, ioctx.operate(oid, &op));
  }

  bool truncated;
  list<cls_rgw_gc_obj_info> entries;
  list<cls_rgw_gc_obj_info> entries2;
  string marker;
  string next_marker;

  /* list chains, verify truncated */
  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 8, true, entries, &truncated, next_marker));
  ASSERT_EQ(8, (int)entries.size());
  ASSERT_EQ(1, truncated);

  marker = next_marker;
  next_marker.clear();

  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 8, true, entries2, &truncated, next_marker));
  ASSERT_EQ(2, (int)entries2.size());
  ASSERT_EQ(0, truncated);

  entries.splice(entries.end(), entries2);

  /* verify all chains are valid */
  list<cls_rgw_gc_obj_info>::iterator iter = entries.begin();
  for (int i = 0; i < 10; i++, ++iter) {
    cls_rgw_gc_obj_info& entry = *iter;

    /* create expected chain name */
    char buf[32];
    snprintf(buf, sizeof(buf), "chain-%d", i);
    string tag = buf;

    /* verify chain name as expected */
    ASSERT_EQ(entry.tag, tag);

    /* verify expected num of objects in chain */
    ASSERT_EQ(2, (int)entry.chain.objs.size());

    list<cls_rgw_obj>::iterator oiter = entry.chain.objs.begin();
    cls_rgw_obj obj1, obj2;

    /* create expected objects */
    create_obj(obj1, i, 1);
    create_obj(obj2, i, 2);

    /* assign returned object names */
    cls_rgw_obj& ret_obj1 = *oiter++;
    cls_rgw_obj& ret_obj2 = *oiter;

    /* verify objects are as expected */
    ASSERT_EQ(1, (int)cmp_objs(obj1, ret_obj1));
    ASSERT_EQ(1, (int)cmp_objs(obj2, ret_obj2));
  }
}

TEST_F(cls_rgw, gc_defer)
{
  librados::IoCtx ioctx;
  librados::Rados rados;

  string gc_pool_name = get_temp_pool_name();
  /* create pool */
  ASSERT_EQ("", create_one_pool_pp(gc_pool_name, rados));
  ASSERT_EQ(0, rados.ioctx_create(gc_pool_name.c_str(), ioctx));

  string oid = "obj";
  string tag = "mychain";

  librados::ObjectWriteOperation op;
  cls_rgw_gc_obj_info info;

  op.create(false);

  info.tag = tag;

  /* create chain */
  cls_rgw_gc_set_entry(op, 0, info);

  ASSERT_EQ(0, ioctx.operate(oid, &op));

  bool truncated;
  list<cls_rgw_gc_obj_info> entries;
  string marker;
  string next_marker;

  /* list chains, verify num entries as expected */
  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 1, true, entries, &truncated, next_marker));
  ASSERT_EQ(1, (int)entries.size());
  ASSERT_EQ(0, truncated);

  librados::ObjectWriteOperation op2;

  /* defer chain */
  cls_rgw_gc_defer_entry(op2, 5, tag);
  ASSERT_EQ(0, ioctx.operate(oid, &op2));

  entries.clear();
  next_marker.clear();

  /* verify list doesn't show deferred entry (this may fail if cluster is thrashing) */
  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 1, true, entries, &truncated, next_marker));
  ASSERT_EQ(0, (int)entries.size());
  ASSERT_EQ(0, truncated);

  /* wait enough */
  sleep(5);
  next_marker.clear();

  /* verify list shows deferred entry */
  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 1, true, entries, &truncated, next_marker));
  ASSERT_EQ(1, (int)entries.size());
  ASSERT_EQ(0, truncated);

  librados::ObjectWriteOperation op3;
  vector<string> tags;
  tags.push_back(tag);

  /* remove chain */
  cls_rgw_gc_remove(op3, tags);
  ASSERT_EQ(0, ioctx.operate(oid, &op3));

  entries.clear();
  next_marker.clear();

  /* verify entry was removed */
  ASSERT_EQ(0, cls_rgw_gc_list(ioctx, oid, marker, 1, true, entries, &truncated, next_marker));
  ASSERT_EQ(0, (int)entries.size());
  ASSERT_EQ(0, truncated);

  /* remove pool */
  ioctx.close();
  ASSERT_EQ(0, destroy_one_pool_pp(gc_pool_name, rados));
}

auto populate_usage_log_info(std::string user, std::string payer, int total_usage_entries)
{
  rgw_usage_log_info info;

  for (int i=0; i < total_usage_entries; i++){
    auto bucket = str_int("bucket", i);
    info.entries.emplace_back(rgw_usage_log_entry(user, payer, bucket));
  }

  return info;
}

auto gen_usage_log_info(std::string payer, std::string bucket, int total_usage_entries)
{
  rgw_usage_log_info info;
  for (int i=0; i < total_usage_entries; i++){
    auto user = str_int("user", i);
    info.entries.emplace_back(rgw_usage_log_entry(user, payer, bucket));
  }

  return info;
}

TEST_F(cls_rgw, usage_basic)
{
  string oid="usage.1";
  string user="user1";
  uint64_t start_epoch{0}, end_epoch{(uint64_t) -1};
  int total_usage_entries = 512;
  uint64_t max_entries = 2000;
  string payer;

  auto info = populate_usage_log_info(user, payer, total_usage_entries);
  ObjectWriteOperation op;
  cls_rgw_usage_log_add(op, info);
  ASSERT_EQ(0, ioctx.operate(oid, &op));

  string read_iter;
  map <rgw_user_bucket, rgw_usage_log_entry> usage, usage2;
  bool truncated;


  int ret = cls_rgw_usage_log_read(ioctx, oid, user, "", start_epoch, end_epoch,
				   max_entries, read_iter, usage, &truncated);
  // read the entries, and see that we have all the added entries
  ASSERT_EQ(0, ret);
  ASSERT_FALSE(truncated);
  ASSERT_EQ(static_cast<uint64_t>(total_usage_entries), usage.size());

  // delete and read to assert that we've deleted all the values
  ASSERT_EQ(0, cls_rgw_usage_log_trim(ioctx, oid, user, "", start_epoch, end_epoch));


  ret = cls_rgw_usage_log_read(ioctx, oid, user, "", start_epoch, end_epoch,
			       max_entries, read_iter, usage2, &truncated);
  ASSERT_EQ(0, ret);
  ASSERT_EQ(0u, usage2.size());

  // add and read to assert that bucket option is valid for usage reading
  string bucket1 = "bucket-usage-1";
  string bucket2 = "bucket-usage-2";
  info = gen_usage_log_info(payer, bucket1, 100);
  cls_rgw_usage_log_add(op, info);
  ASSERT_EQ(0, ioctx.operate(oid, &op));

  info = gen_usage_log_info(payer, bucket2, 100);
  cls_rgw_usage_log_add(op, info);
  ASSERT_EQ(0, ioctx.operate(oid, &op));
  ret = cls_rgw_usage_log_read(ioctx, oid, "", bucket1, start_epoch, end_epoch,
                              max_entries, read_iter, usage2, &truncated);
  ASSERT_EQ(0, ret);
  ASSERT_EQ(100u, usage2.size());

  // delete and read to assert that bucket option is valid for usage trim
  ASSERT_EQ(0, cls_rgw_usage_log_trim(ioctx, oid, "", bucket1, start_epoch, end_epoch));

  ret = cls_rgw_usage_log_read(ioctx, oid, "", bucket1, start_epoch, end_epoch,
                               max_entries, read_iter, usage2, &truncated);
  ASSERT_EQ(0, ret);
  ASSERT_EQ(0u, usage2.size());
  ASSERT_EQ(0, cls_rgw_usage_log_trim(ioctx, oid, "", bucket2, start_epoch, end_epoch));
}

TEST_F(cls_rgw, usage_clear_no_obj)
{
  string user="user1";
  string oid="usage.10";
  librados::ObjectWriteOperation op;
  cls_rgw_usage_log_clear(op);
  int ret = ioctx.operate(oid, &op);
  ASSERT_EQ(0, ret);

}

TEST_F(cls_rgw, usage_clear)
{
  string user="user1";
  string payer;
  string oid="usage.10";
  librados::ObjectWriteOperation op;
  int max_entries=2000;

  auto info = populate_usage_log_info(user, payer, max_entries);

  cls_rgw_usage_log_add(op, info);
  ASSERT_EQ(0, ioctx.operate(oid, &op));

  ObjectWriteOperation op2;
  cls_rgw_usage_log_clear(op2);
  int ret = ioctx.operate(oid, &op2);
  ASSERT_EQ(0, ret);

  map <rgw_user_bucket, rgw_usage_log_entry> usage;
  bool truncated;
  uint64_t start_epoch{0}, end_epoch{(uint64_t) -1};
  string read_iter;
  ret = cls_rgw_usage_log_read(ioctx, oid, user, "", start_epoch, end_epoch,
			       max_entries, read_iter, usage, &truncated);
  ASSERT_EQ(0, ret);
  ASSERT_EQ(0u, usage.size());
}

static int bilog_list(librados::IoCtx& ioctx, const std::string& oid,
                      cls_rgw_bi_log_list_ret *result)
{
  int retcode = 0;
  librados::ObjectReadOperation op;
  cls_rgw_bilog_list(op, "", 128, result, &retcode);
  int ret = ioctx.operate(oid, &op, nullptr);
  if (ret < 0) {
    return ret;
  }
  return retcode;
}

static int bilog_trim(librados::IoCtx& ioctx, const std::string& oid,
                      const std::string& start_marker,
                      const std::string& end_marker)
{
  librados::ObjectWriteOperation op;
  cls_rgw_bilog_trim(op, start_marker, end_marker);
  return ioctx.operate(oid, &op);
}

TEST_F(cls_rgw, bi_log_trim)
{
  string bucket_oid = str_int("bucket", 6);

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  // create 10 versioned entries. this generates instance and olh bi entries,
  // allowing us to check that bilog trim doesn't remove any of those
  for (int i = 0; i < 10; i++) {
    cls_rgw_obj_key obj{str_int("obj", i), "inst"};
    string tag = str_int("tag", i);
    string loc = str_int("loc", i);

    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);
    rgw_bucket_dir_entry_meta meta;
    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, 1, obj, meta);
  }
  // bi list
  {
    list<rgw_cls_bi_entry> entries;
    bool truncated{false};
    ASSERT_EQ(0, cls_rgw_bi_list(ioctx, bucket_oid, "", "", 128,
                                 &entries, &truncated));
    // prepare/complete/instance/olh entry for each
    EXPECT_EQ(40u, entries.size());
    EXPECT_FALSE(truncated);
  }
  // bilog list
  vector<rgw_bi_log_entry> bilog1;
  {
    cls_rgw_bi_log_list_ret bilog;
    ASSERT_EQ(0, bilog_list(ioctx, bucket_oid, &bilog));
    // complete/olh entry for each
    EXPECT_EQ(20u, bilog.entries.size());

    bilog1.assign(std::make_move_iterator(bilog.entries.begin()),
                  std::make_move_iterator(bilog.entries.end()));
  }
  // trim front of bilog
  {
    const std::string from = "";
    const std::string to = bilog1[0].id;
    ASSERT_EQ(0, bilog_trim(ioctx, bucket_oid, from, to));
    cls_rgw_bi_log_list_ret bilog;
    ASSERT_EQ(0, bilog_list(ioctx, bucket_oid, &bilog));
    EXPECT_EQ(19u, bilog.entries.size());
    EXPECT_EQ(bilog1[1].id, bilog.entries.begin()->id);
    ASSERT_EQ(-ENODATA, bilog_trim(ioctx, bucket_oid, from, to));
  }
  // trim back of bilog
  {
    const std::string from = bilog1[18].id;
    const std::string to = "9";
    ASSERT_EQ(0, bilog_trim(ioctx, bucket_oid, from, to));
    cls_rgw_bi_log_list_ret bilog;
    ASSERT_EQ(0, bilog_list(ioctx, bucket_oid, &bilog));
    EXPECT_EQ(18u, bilog.entries.size());
    EXPECT_EQ(bilog1[18].id, bilog.entries.rbegin()->id);
    ASSERT_EQ(-ENODATA, bilog_trim(ioctx, bucket_oid, from, to));
  }
  // trim middle of bilog
  {
    const std::string from = bilog1[13].id;
    const std::string to = bilog1[14].id;
    ASSERT_EQ(0, bilog_trim(ioctx, bucket_oid, from, to));
    cls_rgw_bi_log_list_ret bilog;
    ASSERT_EQ(0, bilog_list(ioctx, bucket_oid, &bilog));
    EXPECT_EQ(17u, bilog.entries.size());
    ASSERT_EQ(-ENODATA, bilog_trim(ioctx, bucket_oid, from, to));
  }
  // trim full bilog
  {
    const std::string from = "";
    const std::string to = "9";
    ASSERT_EQ(0, bilog_trim(ioctx, bucket_oid, from, to));
    cls_rgw_bi_log_list_ret bilog;
    ASSERT_EQ(0, bilog_list(ioctx, bucket_oid, &bilog));
    EXPECT_EQ(0u, bilog.entries.size());
    ASSERT_EQ(-ENODATA, bilog_trim(ioctx, bucket_oid, from, to));
  }
  // bi list should be the same
  {
    list<rgw_cls_bi_entry> entries;
    bool truncated{false};
    ASSERT_EQ(0, cls_rgw_bi_list(ioctx, bucket_oid, "", "", 128,
                                 &entries, &truncated));
    EXPECT_EQ(40u, entries.size());
    EXPECT_FALSE(truncated);
  }
}

TEST_F(cls_rgw, index_racing_removes)
{
  string bucket_oid = str_int("bucket", 8);

  ObjectWriteOperation op;
  cls_rgw_bucket_init_index(op);
  ASSERT_EQ(0, ioctx.operate(bucket_oid, &op));

  int epoch = 0;
  rgw_bucket_dir_entry dirent;
  rgw_bucket_dir_entry_meta meta;

  // prepare/complete add for single object
  const cls_rgw_obj_key obj{"obj"};
  std::string loc = "loc";
  {
    std::string tag = "tag-add";
    index_prepare(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, obj, loc);
    index_complete(ioctx, bucket_oid, CLS_RGW_OP_ADD, tag, ++epoch, obj, meta);
    test_stats(ioctx, bucket_oid, RGWObjCategory::None, 1, 0);
  }

  // list to verify no pending ops
  {
    std::map<int, rgw_cls_list_ret> results;
    list_entries(ioctx, bucket_oid, 1, results);
    ASSERT_EQ(1, results.size());
    const auto& entries = results.begin()->second.dir.m;
    ASSERT_EQ(1, entries.size());
    dirent = std::move(entries.begin()->second);
    ASSERT_EQ(obj, dirent.key);
    ASSERT_TRUE(dirent.exists);
    ASSERT_TRUE(dirent.pending_map.empty());
  }

  // prepare three racing removals
  std::string tag1 = "tag-rm1";
  std::string tag2 = "tag-rm2";
  std::string tag3 = "tag-rm3";
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag1, obj, loc);
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag2, obj, loc);
  index_prepare(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag3, obj, loc);

  test_stats(ioctx, bucket_oid, RGWObjCategory::None, 1, 0);

  // complete on tag2
  index_complete(ioctx, bucket_oid, CLS_RGW_OP_DEL, tag2, ++epoch, obj, meta);
  {
    std::map<int, rgw_cls_list_ret> results;
    list_entries(ioctx, bucket_oid, 1, results);
    ASSERT_EQ(1, results.size());
    const auto& entries = results.begin()->second.dir.m;
    ASSERT_EQ(1, entries.size());
    dirent = std::move(entries.begin()->second);
    ASSERT_EQ(obj, dirent.key);
    ASSERT_FALSE(dirent.exists);
    ASSERT_FALSE(dirent.pending_map.empty());
  }

  // cancel on tag1
  index_complete(ioctx, bucket_oid, CLS_RGW_OP_CANCEL, tag1, ++epoch, obj, meta);
  {
    std::map<int, rgw_cls_list_ret> results;
    list_entries(ioctx, bucket_oid, 1, results);
    ASSERT_EQ(1, results.size());
    const auto& entries = results.begin()->second.dir.m;
    ASSERT_EQ(1, entries.size());
    dirent = std::move(entries.begin()->second);
    ASSERT_EQ(obj, dirent.key);
    ASSERT_FALSE(dirent.exists);
    ASSERT_FALSE(dirent.pending_map.empty());
  }

  // final cancel on tag3
  index_complete(ioctx, bucket_oid, CLS_RGW_OP_CANCEL, tag3, ++epoch, obj, meta);

  // verify that the key was removed
  {
    std::map<int, rgw_cls_list_ret> results;
    list_entries(ioctx, bucket_oid, 1, results);
    EXPECT_EQ(1, results.size());
    const auto& entries = results.begin()->second.dir.m;
    ASSERT_EQ(0, entries.size());
  }

  test_stats(ioctx, bucket_oid, RGWObjCategory::None, 0, 0);
}
