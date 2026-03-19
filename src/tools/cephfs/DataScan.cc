// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab
/*
 * Ceph - scalable distributed file system
 *
 * Copyright (C) 2015 Red Hat
 *
 * This is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License version 2.1, as published by the Free Software
 * Foundation.  See file COPYING.
 *
 */

#include "common/ceph_argparse.h"
#include "common/ceph_json.h"
#include "common/errno.h"
#include "common/strtol.h"
#include "include/ceph_fs.h"
#include "include/compat.h"
#include "include/util.h"
#include "json_spirit/json_spirit_error_position.h"
#include "json_spirit/json_spirit_reader.h"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <condition_variable>
#include <fstream>
#include <functional>
#include <limits>
#include <mutex>
#include <queue>
#include <sstream>
#include <thread>

#include "mds/CDentry.h"
#include "mds/CInode.h"
#include "mds/CDentry.h"
#include "mds/InoTable.h"
#include "mds/SnapServer.h"
#include "cls/cephfs/cls_cephfs_client.h"

#include "PgFiles.h"
#include "DataScan.h"
#include "include/compat.h"

#define dout_context g_ceph_context
#define dout_subsys ceph_subsys_mds
#undef dout_prefix
#define dout_prefix *_dout << "datascan." << __func__ << ": "

using namespace std;

namespace {

class DataScanThreadPool {
public:
  explicit DataScanThreadPool(size_t worker_count) {
    worker_count = std::max<size_t>(worker_count, 1);
    workers.reserve(worker_count);
    max_queue_size = worker_count * 10;
    for (size_t i = 0; i < worker_count; ++i) {
      workers.emplace_back([this]() { worker_entry(); });
    }
  }

  ~DataScanThreadPool() { shutdown(); }

  bool submit(std::function<int()> task) {
    std::unique_lock l(lock);
    queue_has_space.wait(l, [this]() {
      return (error < 0 || stopping || !accepting ||
              tasks.size() < max_queue_size);
    });
    if (error < 0 || !accepting || stopping) {
      return false;
    }
    tasks.push(std::move(task));
    has_work.notify_one();
    return true;
  }

  void wait_for_drain() {
    std::unique_lock l(lock);
    accepting = false;
    drained.wait(l, [this]() { return tasks.empty() && running_tasks == 0; });
    queue_has_space.notify_all();
  }

  void shutdown() {
    {
      std::lock_guard l(lock);
      if (stopping) {
        return;
      }
      accepting = false;
      stopping = true;
    }
    queue_has_space.notify_all();
    has_work.notify_all();
    for (auto &worker : workers) {
      if (worker.joinable()) {
        worker.join();
      }
    }
    workers.clear();
  }
  
  int get_error() {
    return error.load(std::memory_order_relaxed);
  }

private:
  void worker_entry() {
    while (true) {
      std::function<int()> task;
      bool do_run = true;
      int r = 0;
      {
        std::unique_lock l(lock);
        has_work.wait(
            l, [this]() { return stopping || !tasks.empty(); });
        if (stopping && tasks.empty()) {
          return;
        }
        task = std::move(tasks.front());
        tasks.pop();
        queue_has_space.notify_one();
        ++running_tasks;
        do_run = (error == 0);
      }
      if (do_run) {
        r = task();
      }

      {
        std::lock_guard l(lock);
        if (r < 0 && error == 0) {
          error = r;
        }
        --running_tasks;
        if (!accepting && tasks.empty() && running_tasks == 0) {
          drained.notify_all();
        }
      }
    }
  }

  std::mutex lock;
  std::condition_variable has_work;
  std::condition_variable drained;
  std::condition_variable queue_has_space;
  std::queue<std::function<int()>> tasks;
  std::vector<std::thread> workers;
  size_t max_queue_size = 1;
  bool accepting = true;
  bool stopping = false;
  size_t running_tasks = 0;
  std::atomic<int> error = 0;
};

std::string trim_whitespace(const std::string &value)
{
  static constexpr char whitespace[] = " \t\n\r\f\v";
  size_t start = value.find_first_not_of(whitespace);
  if (start == std::string::npos) {
    return std::string();
  }
  size_t end = value.find_last_not_of(whitespace);
  return value.substr(start, end - start + 1);
}

std::string describe_json_parse_error(const std::string &json_line)
{
  json_spirit::Value parsed;
  try {
    json_spirit::read_or_throw(json_line, parsed);
  } catch (const json_spirit::Error_position &e) {
    std::ostringstream oss;
    oss << e.reason_ << " at column " << e.column_;
    return oss.str();
  } catch (const std::string &e) {
    return e;
  } catch (const std::exception &e) {
    return e.what();
  }

  return "invalid JSON syntax";
}

}

void DataScan::usage() {
  std::cout
      << "Usage: \n"
      << "  cephfs-data-scan init [--force-init]\n"
      << "  cephfs-data-scan scan_extents [--force-pool] "
         "[--force-create-head-inode] [--worker_n N "
         "--worker_m M] [--damage-file PATH] [--type 'TYPE|TYPE'] "
         "[--inode-file PATH] [--extent-period N] [--extent-limit N] "
         "[<data pool name> [<extra data pool "
         "name> ...]]\n"
      << "  cephfs-data-scan scan_inodes [--force-pool] [--force-corrupt] "
         "[--force-restore-all-ancestors] "
         "[--worker_n N --worker_m M] [--damage-file PATH] "
         "[--type 'TYPE|TYPE'] [--inode-file PATH] [<data pool name>]\n"
      << "  cephfs-data-scan pg_files <path> <pg id> [<pg id>...]\n"
      << "  cephfs-data-scan scan_links [--thread-count N]\n"
      << "\n"
      << "    --force-corrupt: overrite apparently corrupt structures\n"
      << "    --force-init: write root inodes even if they exist\n"
      << "    --force-pool: use data pool even if it is not in FSMap\n"
      << "    --worker_m: Maximum number of workers\n"
      << "    --worker_n: Worker number, range 0-(worker_m-1)\n"
      << "    --thread-count: Number of scan_links worker threads\n"
      << "    --force-create-head-inode: force flushing inode 0th object in "
         "targeted scan_extents even when no extents are found\n"
      << "    --force-restore-all-ancestors: force restoring all ancestor "
         "dirfrags during targeted scan_inodes\n"
      << "      with targeted scan_inodes (--damage-file/--inode-file), "
         "requires single worker (--worker_n 0 --worker_m 1)\n"
      << "\n"
      << "  cephfs-data-scan scan_frags [--force-corrupt]\n"
      << "  cephfs-data-scan cleanup [<data pool name>]\n"
      << std::endl;

  generic_client_usage();
}

bool DataScan::parse_kwarg(
    const std::string &command,
    const std::vector<const char*> &args,
    std::vector<const char *>::const_iterator &i,
    int *r)
{
  const std::string arg(*i);

  if (i + 1 == args.end()) {
    if (arg == std::string("--damage-file") || arg == std::string("--type") ||
        arg == std::string("--inode-file") ||
        arg == std::string("--extent-period") ||
        arg == std::string("--extent-limit")) {
      std::cerr << "Missing value for '" << arg << "'" << std::endl;
      *r = -EINVAL;
    }
    return false;
  }

  const std::string val(*(i + 1));

  if (arg == std::string("--output-dir")) {
    if (driver != NULL) {
      derr << "Unexpected --output-dir: output already selected!" << dendl;
      *r = -EINVAL;
      return false;
    }
    dout(4) << "Using local file output to '" << val << "'" << dendl;
    driver = new LocalFileDriver(val, data_io);
    return true;
  } else if (arg == std::string("--worker_n")) {
    if (command == "scan_links") {
      std::cerr << "Option '--worker_n' is invalid with scan_links; "
                   "use '--thread-count'"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    std::string err;
    int64_t worker_n = strict_strtoll(val.c_str(), 10, &err);
    if (!err.empty() || worker_n < 0 ||
        worker_n > std::numeric_limits<uint32_t>::max()) {
      std::cerr << "Invalid worker number '" << val << "'" << std::endl;
      *r = -EINVAL;
      return false;
    }
    n = static_cast<uint32_t>(worker_n);
    return true;
  } else if (arg == std::string("--worker_m")) {
    if (command == "scan_links") {
      std::cerr << "Option '--worker_m' is invalid with scan_links; "
                   "use '--thread-count'"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    std::string err;
    int64_t worker_m = strict_strtoll(val.c_str(), 10, &err);
    if (!err.empty() || worker_m <= 0 ||
        worker_m > std::numeric_limits<uint32_t>::max()) {
      std::cerr << "Invalid worker count '" << val << "'" << std::endl;
      *r = -EINVAL;
      return false;
    }
    m = static_cast<uint32_t>(worker_m);
    return true;
  } else if (arg == std::string("--thread-count")) {
    if (command != "scan_links") {
      std::cerr << "Option '--thread-count' is only valid with scan_links"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    std::string err;
    int64_t thread_count = strict_strtoll(val.c_str(), 10, &err);
    if (!err.empty() || thread_count <= 0 ||
        thread_count > std::numeric_limits<uint32_t>::max()) {
      std::cerr << "Invalid thread count '" << val << "'" << std::endl;
      *r = -EINVAL;
      return false;
    }
    scan_links_thread_count = static_cast<uint32_t>(thread_count);
    return true;
  } else if (arg == std::string("--filter-tag")) {
    filter_tag = val;
    dout(10) << "Applying tag filter: '" << filter_tag << "'" << dendl;
    return true;
  } else if (arg == std::string("--damage-file")) {
    if (command != "scan_extents" && command != "scan_inodes") {
      std::cerr << "Option '--damage-file' is only valid with scan_extents "
                   "or scan_inodes"
                << std::endl;
      *r = -EINVAL;
      return false;
    }
    damage_file_path = val;
    return true;
  } else if (arg == std::string("--type")) {
    if (command != "scan_extents" && command != "scan_inodes") {
      std::cerr << "Option '--type' is only valid with scan_extents or "
                   "scan_inodes"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    std::vector<std::string> tokens;
    *r = parse_damage_type_expr(val, &tokens);
    if (*r < 0) {
      return false;
    }
    damage_type_expr = val;
    damage_type_tokens = tokens;
    damage_type_filter_set = true;
    damage_type_all = false;
    return true;
  } else if (arg == std::string("--inode-file")) {
    if (command != "scan_extents" && command != "scan_inodes") {
      std::cerr << "Option '--inode-file' is only valid with scan_extents "
                   "or scan_inodes"
                << std::endl;
      *r = -EINVAL;
      return false;
    }
    inode_file_path = val;
    return true;
  } else if (arg == std::string("--extent-period")) {
    if (command != "scan_extents") {
      std::cerr << "Option '--extent-period' is only valid with scan_extents"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    auto parsed_period = ceph::parse<uint64_t>(val);
    if (!parsed_period || *parsed_period == 0) {
      std::cerr << "Invalid extent period '" << val << "': must be >= 1"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    extent_period = *parsed_period;
    extent_period_set = true;
    return true;
  } else if (arg == std::string("--extent-limit")) {
    if (command != "scan_extents") {
      std::cerr << "Option '--extent-limit' is only valid with scan_extents"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    auto parsed_limit = ceph::parse<uint64_t>(val);
    if (!parsed_limit || *parsed_limit == 0) {
      std::cerr << "Invalid extent limit '" << val << "': must be >= 1"
                << std::endl;
      *r = -EINVAL;
      return false;
    }

    extent_limit = *parsed_limit;
    extent_limit_set = true;
    return true;
  } else if (arg == std::string("--filesystem")) {
    std::shared_ptr<const Filesystem> fs;
    *r = fsmap->parse_filesystem(val, &fs);
    if (*r != 0) {
      std::cerr << "Invalid filesystem '" << val << "'" << std::endl;
      return false;
    }
    fscid = fs->fscid;
    return true;
  } else if (arg == std::string("--alternate-pool")) {
    metadata_pool_name = val;
    return true;
  } else {
    return false;
  }
}

int DataScan::parse_damage_type_expr(const std::string &expr,
                                     std::vector<std::string> *tokens) {
  tokens->clear();
  std::string trimmed_expr = trim_whitespace(expr);
  if (trimmed_expr.empty()) {
    std::cerr << "Invalid --type expression: empty value" << std::endl;
    return -EINVAL;
  }

  std::set<std::string> seen;
  size_t start = 0;
  while (start <= expr.size()) {
    size_t sep = expr.find('|', start);
    std::string raw = sep == std::string::npos
      ? expr.substr(start)
      : expr.substr(start, sep - start);
    std::string token = trim_whitespace(raw);
    if (token.empty()) {
      std::cerr << "Invalid --type expression '" << expr
                << "': empty token around '|'" << std::endl;
      return -EINVAL;
    }
    if (seen.insert(token).second) {
      tokens->push_back(token);
    }

    if (sep == std::string::npos) {
      break;
    }
    start = sep + 1;
  }

  return 0;
}

bool DataScan::parse_arg(
    const std::vector<const char*> &args,
    std::vector<const char *>::const_iterator &i)
{
  const std::string arg(*i);
  if (arg == "--force-pool") {
    force_pool = true;
    return true;
  } else if (arg == "--force-corrupt") {
    force_corrupt = true;
    return true;
  } else if (arg == "--force-init") {
    force_init = true;
    return true;
  } else if (arg == "--force-create-head-inode") {
    force_create_head_inode = true;
    return true;
  } else if (arg == "--force-restore-all-ancestors") {
    force_restore_all_ancestors = true;
    return true;
  } else {
    return false;
  }
}

int DataScan::main(const std::vector<const char*> &args)
{
  // Parse args
  // ==========
  if (args.size() < 1) {
    cerr << "missing position argument" << std::endl;
    return -EINVAL;
  }

  // Common RADOS init: open metadata pool
  // =====================================
  librados::Rados rados;
  int r = rados.init_with_context(g_ceph_context);
  if (r < 0) {
    derr << "RADOS unavailable" << dendl;
    return r;
  }

  std::string const &command = args[0];
  std::string data_pool_name;
  std::set<std::string> extra_data_pool_names;

  std::string pg_files_path;
  std::set<pg_t> pg_files_pgs;

  // Consume any known --key val or --flag arguments
  for (std::vector<const char *>::const_iterator i = args.begin() + 1;
       i != args.end(); ++i) {
    if (parse_kwarg(command, args, i, &r)) {
      // Skip the kwarg value field
      ++i;
      continue;
    } else if (r) {
      return r;
    }

    if (parse_arg(args, i)) {
      continue;
    }

    // Trailing positional arguments
    if (command == "scan_extents") {
      if (data_pool_name.empty()) {
	data_pool_name = *i;
      } else if (*i != data_pool_name) {
	extra_data_pool_names.insert(*i);
      }
      continue;
    }

    // Trailing positional argument
    if (i + 1 == args.end() &&
        (command == "scan_inodes"
         || command == "cleanup")) {
      data_pool_name = *i;
      continue;
    }

    if (command == "pg_files") {
      if (i == args.begin() + 1) {
        pg_files_path = *i;
        continue;
      } else {
        pg_t pg;
        bool parsed = pg.parse(*i);
        if (!parsed) {
          std::cerr << "Invalid PG '" << *i << "'" << std::endl;
          return -EINVAL;
        } else {
          pg_files_pgs.insert(pg);
          continue;
        }
      }

    }

    // Fall through: unhandled
    std::cerr << "Unknown argument '" << *i << "'" << std::endl;
    return -EINVAL;
  }

  if (command != "scan_extents" && command != "scan_inodes") {
    if (!damage_file_path.empty()) {
      std::cerr << "Option '--damage-file' is only valid with scan_extents "
                   "or scan_inodes"
                << std::endl;
      return -EINVAL;
    }
    if (damage_type_filter_set) {
      std::cerr << "Option '--type' is only valid with scan_extents or "
                   "scan_inodes"
                << std::endl;
      return -EINVAL;
    }
    if (!inode_file_path.empty()) {
      std::cerr << "Option '--inode-file' is only valid with scan_extents "
                   "or scan_inodes"
                << std::endl;
      return -EINVAL;
    }
  }

  if (command != "scan_extents") {
    if (extent_period_set) {
      std::cerr << "Option '--extent-period' is only valid with scan_extents"
                << std::endl;
      return -EINVAL;
    }
    if (extent_limit_set) {
      std::cerr << "Option '--extent-limit' is only valid with scan_extents"
                << std::endl;
      return -EINVAL;
    }
    if (force_create_head_inode) {
      std::cerr << "Option '--force-create-head-inode' is only valid with "
                   "scan_extents"
                << std::endl;
      return -EINVAL;
    }
  }

  if (command != "scan_inodes") {
    if (force_restore_all_ancestors) {
      std::cerr << "Option '--force-restore-all-ancestors' is only valid with "
                   "scan_inodes"
                << std::endl;
      return -EINVAL;
    }
  }

  if (command == "scan_extents" && (extent_period_set || extent_limit_set) &&
      damage_file_path.empty() && inode_file_path.empty()) {
    std::cerr << "Options '--extent-period'/'--extent-limit' require "
                 "'--damage-file' or "
                 "'--inode-file'"
              << std::endl;
    return -EINVAL;
  }

  if ((command == "scan_extents" || command == "scan_inodes") &&
      damage_type_filter_set && damage_file_path.empty()) {
    std::cerr << "Option '--type' requires '--damage-file'" << std::endl;
    return -EINVAL;
  }

  if (command == "scan_extents" && force_create_head_inode &&
      damage_file_path.empty() && inode_file_path.empty()) {
    std::cerr << "Option '--force-create-head-inode' requires '--damage-file' "
                 "or '--inode-file'"
              << std::endl;
    return -EINVAL;
  }

  if (command == "scan_inodes" && force_restore_all_ancestors &&
      damage_file_path.empty() && inode_file_path.empty()) {
    std::cerr << "Option '--force-restore-all-ancestors' requires "
                 "'--damage-file' or '--inode-file'"
              << std::endl;
    return -EINVAL;
  }

  const bool targeted_scan_inodes =
      command == "scan_inodes" &&
      (!damage_file_path.empty() || !inode_file_path.empty());
  if (targeted_scan_inodes && force_restore_all_ancestors &&
      (m != 1 || n != 0)) {
    std::cerr << "Targeted 'scan_inodes' with "
                 "'--force-restore-all-ancestors' requires single worker "
                 "('--worker_n 0 --worker_m 1')"
              << std::endl;
    return -EINVAL;
  }

  if ((command == "scan_extents" || command == "scan_inodes") &&
      !damage_file_path.empty() && !damage_type_filter_set) {
    damage_type_all = true;
  }

  // If caller didn't specify a namespace, try to pick
  // one if only one exists
  if (fscid == FS_CLUSTER_ID_NONE) {
    if (fsmap->filesystem_count() == 1) {
      fscid = fsmap->get_filesystem()->fscid;
    } else {
      std::cerr << "Specify a filesystem with --filesystem" << std::endl;
      return -EINVAL;
    }
  }
  auto fs =  fsmap->get_filesystem(fscid);
  ceph_assert(fs != nullptr);

  // Default to output to metadata pool
  if (driver == NULL) {
    driver = new MetadataDriver(this);
    driver->set_force_corrupt(force_corrupt);
    driver->set_force_init(force_init);
    dout(4) << "Using metadata pool output" << dendl;
  }

  dout(4) << "connecting to RADOS..." << dendl;
  r = rados.connect();
  if (r < 0) {
    std::cerr << "couldn't connect to cluster: " << cpp_strerror(r)
              << std::endl;
    return r;
  }

  r = driver->init(rados, metadata_pool_name, fsmap, fscid);
  if (r < 0) {
    return r;
  }

  if (command == "pg_files") {
    auto pge = PgFiles(objecter, pg_files_pgs);
    pge.init();
    return pge.scan_path(pg_files_path);
  }

  bool autodetect_data_pools = false;

  // Initialize data_io for those commands that need it
  if (command == "scan_inodes" ||
      command == "scan_extents" ||
      command == "cleanup") {
    data_pool_id = fs->mds_map.get_first_data_pool();

    std::string pool_name;
    r = rados.pool_reverse_lookup(data_pool_id, &pool_name);
    if (r < 0) {
      std::cerr << "Failed to resolve data pool: " << cpp_strerror(r)
		<< std::endl;
      return r;
    }

    if (data_pool_name.empty()) {
      autodetect_data_pools = true;
      data_pool_name = pool_name;
    } else if (data_pool_name != pool_name) {
      std::cerr << "Warning: pool '" << data_pool_name << "' is not the "
        "main CephFS data pool!" << std::endl;
      if (!force_pool) {
        std::cerr << "Use --force-pool to continue" << std::endl;
        return -EINVAL;
      }

      data_pool_id = rados.pool_lookup(data_pool_name.c_str());
      if (data_pool_id < 0) {
	std::cerr << "Data pool '" << data_pool_name << "' not found!"
		  << std::endl;
	return -ENOENT;
      }
    }

    dout(4) << "data pool '" << data_pool_name << "' has ID " << data_pool_id
	    << dendl;

    dout(4) << "opening data pool '" << data_pool_name << "'" << dendl;
    r = rados.ioctx_create(data_pool_name.c_str(), data_io);
    if (r != 0) {
      return r;
    }
  }

  // Initialize extra data_ios for those commands that need it
  if (command == "scan_extents") {
    if (autodetect_data_pools) {
      ceph_assert(extra_data_pool_names.empty());

      for (auto &pool_id : fs->mds_map.get_data_pools()) {
	if (pool_id == data_pool_id) {
	  continue;
	}

	std::string pool_name;
	r = rados.pool_reverse_lookup(pool_id, &pool_name);
	if (r < 0) {
	  std::cerr << "Failed to resolve data pool: " << cpp_strerror(r)
		    << std::endl;
	  return r;
	}
	extra_data_pool_names.insert(pool_name);
      }
    }

    for (auto &data_pool_name: extra_data_pool_names) {
      int64_t pool_id = rados.pool_lookup(data_pool_name.c_str());
      if (data_pool_id < 0) {
	std::cerr << "Data pool '" << data_pool_name << "' not found!" << std::endl;
	return -ENOENT;
      } else {
	dout(4) << "data pool '" << data_pool_name << "' has ID " << pool_id
		<< dendl;
      }

      if (!fs->mds_map.is_data_pool(pool_id)) {
	std::cerr << "Warning: pool '" << data_pool_name << "' is not a "
	  "CephFS data pool!" << std::endl;
	if (!force_pool) {
	  std::cerr << "Use --force-pool to continue" << std::endl;
	  return -EINVAL;
	}
      }

      dout(4) << "opening data pool '" << data_pool_name << "'" << dendl;
      extra_data_ios.push_back({});
      r = rados.ioctx_create(data_pool_name.c_str(), extra_data_ios.back());
      if (r != 0) {
	return r;
      }
    }
  }

  // Initialize metadata_io from MDSMap for scan_frags
  if (command == "scan_frags" || command == "scan_links" ||
      command == "scan_inodes") {
    const auto fs = fsmap->get_filesystem(fscid);
    if (fs == nullptr) {
      std::cerr << "Filesystem id " << fscid << " does not exist" << std::endl;
      return -ENOENT;
    }
    int64_t const metadata_pool_id = fs->mds_map.get_metadata_pool();

    dout(4) << "resolving metadata pool " << metadata_pool_id << dendl;
    int r = rados.pool_reverse_lookup(metadata_pool_id, &metadata_pool_name);
    if (r < 0) {
      std::cerr << "Pool " << metadata_pool_id
                << " identified in MDS map not found in RADOS!" << std::endl;
      return r;
    }

    r = rados.ioctx_create(metadata_pool_name.c_str(), metadata_io);
    if (r != 0) {
      return r;
    }

    data_pools = fs->mds_map.get_data_pools();
  }

  // Finally, dispatch command
  if (command == "scan_inodes") {
    if (!damage_file_path.empty()) {
      return scan_inodes_for_damage_file();
    }
    if (!inode_file_path.empty()) {
      return scan_inodes_for_inode_file();
    }
    return scan_inodes();
  } else if (command == "scan_extents") {
    if (!damage_file_path.empty()) {
      return scan_extents_for_damage_file();
    }
    if (!inode_file_path.empty()) {
      return scan_extents_for_inode_file();
    }
    return scan_extents();
  } else if (command == "scan_frags") {
    return scan_frags();
  } else if (command == "scan_links") {
    return scan_links();
  } else if (command == "cleanup") {
    return cleanup();
  } else if (command == "init") {
    return driver->init_roots(fs->mds_map.get_first_data_pool());
  } else {
    std::cerr << "Unknown command '" << command << "'" << std::endl;
    return -EINVAL;
  }
}

int MetadataDriver::inject_unlinked_inode(
    inodeno_t inono, int mode, int64_t data_pool_id)
{
  const object_t oid = InodeStore::get_object_name(inono, frag_t(), ".inode");

  // Skip if exists
  bool already_exists = false;
  int r = root_exists(inono, &already_exists);
  if (r) {
    return r;
  }
  if (already_exists && !force_init) {
    std::cerr << "Inode 0x" << std::hex << inono << std::dec << " already"
               " exists, skipping create.  Use --force-init to overwrite"
               " the existing object." << std::endl;
    return 0;
  }

  // Compose
  InodeStore inode_data;
  auto inode = inode_data.get_inode();
  inode->ino = inono;
  inode->version = 1;
  inode->xattr_version = 1;
  inode->mode = 0500 | mode;
  // Fake dirstat.nfiles to 1, so that the directory doesn't appear to be empty
  // (we won't actually give the *correct* dirstat here though)
  inode->dirstat.nfiles = 1;

  inode->ctime = inode->mtime = ceph_clock_now();
  inode->nlink = 1;
  inode->truncate_size = -1ull;
  inode->truncate_seq = 1;
  inode->uid = g_conf()->mds_root_ino_uid;
  inode->gid = g_conf()->mds_root_ino_gid;

  // Force layout to default: should we let users override this so that
  // they don't have to mount the filesystem to correct it?
  inode->layout = file_layout_t::get_default();
  inode->layout.pool_id = data_pool_id;
  inode->dir_layout.dl_dir_hash = g_conf()->mds_default_dir_hash;

  // Assume that we will get our stats wrong, and that we may
  // be ignoring dirfrags that exist
  inode_data.damage_flags |= (DAMAGE_STATS | DAMAGE_RSTATS | DAMAGE_FRAGTREE);

  if (inono == CEPH_INO_ROOT || MDS_INO_IS_MDSDIR(inono)) {
    sr_t srnode;
    srnode.seq = 1;
    encode(srnode, inode_data.snap_blob);
  }

  // Serialize
  bufferlist inode_bl;
  encode(std::string(CEPH_FS_ONDISK_MAGIC), inode_bl);
  inode_data.encode(inode_bl, CEPH_FEATURES_SUPPORTED_DEFAULT);

  // Write
  r = metadata_io.write_full(oid.name, inode_bl);
  if (r != 0) {
    derr << "Error writing '" << oid.name << "': " << cpp_strerror(r) << dendl;
    return r;
  }

  return r;
}

int MetadataDriver::root_exists(inodeno_t ino, bool *result)
{
  object_t oid = InodeStore::get_object_name(ino, frag_t(), ".inode");
  uint64_t size;
  time_t mtime;
  int r = metadata_io.stat(oid.name, &size, &mtime);
  if (r == -ENOENT) {
    *result = false;
    return 0;
  } else if (r < 0) {
    return r;
  }

  *result = true;
  return 0;
}

int MetadataDriver::init_roots(int64_t data_pool_id)
{
  int r = 0;
  r = inject_unlinked_inode(CEPH_INO_ROOT, S_IFDIR|0755, data_pool_id);
  if (r != 0) {
    return r;
  }
  r = inject_unlinked_inode(MDS_INO_MDSDIR(0), S_IFDIR, data_pool_id);
  if (r != 0) {
    return r;
  }
  bool created = false;
  r = find_or_create_dirfrag(MDS_INO_MDSDIR(0), frag_t(), &created);
  if (r != 0) {
    return r;
  }

  return 0;
}

int MetadataDriver::check_roots(bool *result)
{
  int r;
  r = root_exists(CEPH_INO_ROOT, result);
  if (r != 0) {
    return r;
  }
  if (!*result) {
    return 0;
  }

  r = root_exists(MDS_INO_MDSDIR(0), result);
  if (r != 0) {
    return r;
  }
  if (!*result) {
    return 0;
  }

  return 0;
}

/**
 * Stages:
 *
 * SERIAL init
 *  0. Create root inodes if don't exist
 * PARALLEL scan_extents
 *  1. Size and mtime recovery: scan ALL objects, and update 0th
 *   objects with max size and max mtime seen.
 * PARALLEL scan_inodes
 *  2. Inode recovery: scan ONLY 0th objects, and inject metadata
 *   into dirfrag OMAPs, creating blank dirfrags as needed.  No stats
 *   or rstats at this stage.  Inodes without backtraces go into
 *   lost+found
 * TODO: SERIAL "recover stats"
 *  3. Dirfrag statistics: depth first traverse into metadata tree,
 *    rebuilding dir sizes.
 * TODO PARALLEL "clean up"
 *  4. Cleanup; go over all 0th objects (and dirfrags if we tagged
 *   anything onto them) and remove any of the xattrs that we
 *   used for accumulating.
 */


int parse_oid(const std::string &oid, uint64_t *inode_no, uint64_t *obj_id)
{
  if (oid.find(".") == std::string::npos || oid.find(".") == oid.size() - 1) {
    return -EINVAL;
  }

  std::string err;
  std::string inode_str = oid.substr(0, oid.find("."));
  *inode_no = strict_strtoll(inode_str.c_str(), 16, &err);
  if (!err.empty()) {
    return -EINVAL;
  }

  std::string pos_string = oid.substr(oid.find(".") + 1);
  *obj_id = strict_strtoll(pos_string.c_str(), 16, &err);
  if (!err.empty()) {
    return -EINVAL;
  }

  return 0;
}

std::string format_extent_oid(inodeno_t ino, uint64_t extent_id) {
  char buf[60];
  snprintf(buf, sizeof(buf), "%llx.%08llx", (long long unsigned)ino,
           (long long unsigned)extent_id);
  return std::string(buf);
}

int DataScan::scan_extent_for_inode(
    inodeno_t ino, const std::vector<librados::IoCtx *> &data_ios) {
  ceph_assert(!data_ios.empty());

  AccumulateResult accum_res;
  bool found_extent = false;
  uint64_t window_start = 0;
  while (window_start < extent_limit) {
    bool window_productive = false;
    bool all_stat_failed = true;

    uint64_t window_end = window_start + extent_period;
    if (window_end < window_start || window_end > extent_limit) {
      window_end = extent_limit;
    }
    for (uint64_t extent_id = window_start; extent_id < window_end;
         ++extent_id) {
      const std::string oid = format_extent_oid(ino, extent_id);

      for (auto ioctx : data_ios) {
        uint64_t size = 0;
        time_t mtime = 0;
        int r = ioctx->stat(oid, &size, &mtime);
        if (r != 0) {
          dout(20) << "Cannot stat '" << oid << "' in pool id "
                   << ioctx->get_id() << ": " << cpp_strerror(r) << dendl;
          continue;
        }

        all_stat_failed = false;
        int64_t obj_pool_id =
            data_io.get_id() != ioctx->get_id() ? ioctx->get_id() : -1;

        r = ClsCephFSClient::accumulate_inode_metadata(data_io, ino, extent_id,
                                                       size, obj_pool_id, mtime,
                                                       &accum_res, false);
        if (r < 0) {
          derr << "Failed to accumulate metadata for inode 0x" << std::hex
               << static_cast<uint64_t>(ino) << std::dec << " from '" << oid
               << "': " << cpp_strerror(r) << dendl;
          return r;
        }

        window_productive = true;
        found_extent = true;
      }
    }

    if (!window_productive && all_stat_failed) {
      break;
    }

    window_start = window_end;
  }

  if (!found_extent) {
    dout(10) << "No extents found for inode 0x" << std::hex
             << static_cast<uint64_t>(ino) << std::dec << dendl;
    if (!force_create_head_inode) {
      dout(10) << "Skipping flush for inode 0x" << std::hex
               << static_cast<uint64_t>(ino) << std::dec
               << " because no extents were found and "
                  "--force-create-head-inode is not set"
               << dendl;
      return 0;
    }
    dout(10) << "Forcing flush for inode 0x" << std::hex
             << static_cast<uint64_t>(ino) << std::dec
             << " despite no extents found because "
                "--force-create-head-inode is set"
             << dendl;
  }

  int r =
      ClsCephFSClient::flush_inode_accumulate_result(data_io, ino, accum_res);
  if (r < 0) {
    derr << "Failed to flush accumulated metadata for inode 0x" << std::hex
         << static_cast<uint64_t>(ino) << std::dec << ": " << cpp_strerror(r)
         << dendl;
  }

  return r;
}

int DataScan::scan_extents()
{
  std::vector<librados::IoCtx *> data_ios;
  data_ios.push_back(&data_io);
  for (auto &extra_data_io : extra_data_ios) {
    data_ios.push_back(&extra_data_io);
  }

  for (auto ioctx : data_ios) {
    int r = forall_objects(*ioctx, false, [this, ioctx](
        std::string const &oid,
        uint64_t obj_name_ino,
        uint64_t obj_name_offset) -> int
    {
      // Read size
      uint64_t size;
      time_t mtime;
      int r = ioctx->stat(oid, &size, &mtime);
      dout(10) << "handling object " << obj_name_ino
	       << "." << obj_name_offset << dendl;
      if (r != 0) {
	dout(4) << "Cannot stat '" << oid << "': skipping" << dendl;
	return r;
      }
      int64_t obj_pool_id = data_io.get_id() != ioctx->get_id() ?
	ioctx->get_id() : -1;

      // I need to keep track of
      //  * The highest object ID seen
      //  * The size of the highest object ID seen
      //  * The largest object seen
      //  * The pool of the objects seen (if it is not the main data pool)
      //
      //  Given those things, I can later infer the object chunking
      //  size, the offset of the last object (chunk size * highest ID seen),
      //  the actual size (offset of last object + size of highest ID seen),
      //  and the layout pool id.
      //
      //  This logic doesn't take account of striping.
      r = ClsCephFSClient::accumulate_inode_metadata(
          data_io,
	  obj_name_ino,
	  obj_name_offset,
	  size,
	  obj_pool_id,
	  mtime);
      if (r < 0) {
	derr << "Failed to accumulate metadata data from '"
	     << oid << "': " << cpp_strerror(r) << dendl;
	return r;
      }

      return r;
    });
    if (r < 0) {
      return r;
    }
  }

  return 0;
}

int DataScan::scan_extents_for_damage_file()
{
  if (m == 0) {
    std::cerr << "Invalid worker count " << m << ": must be > 0" << std::endl;
    return -EINVAL;
  }
  if (n >= m) {
    std::cerr << "Invalid worker number " << n
              << ": must be in range [0, " << (m - 1) << "]" << std::endl;
    return -EINVAL;
  }

  std::ifstream input(damage_file_path);
  if (!input.is_open()) {
    int err = errno ? -errno : -ENOENT;
    std::cerr << "Failed to open damage file '" << damage_file_path << "': "
              << cpp_strerror(err) << std::endl;
    return err;
  }

  std::vector<librados::IoCtx *> data_ios;
  data_ios.push_back(&data_io);
  for (auto &extra_data_io : extra_data_ios) {
    data_ios.push_back(&extra_data_io);
  }

  uint64_t candidate_index = 0;
  uint64_t line_no = 0;
  std::string line;
  while (std::getline(input, line)) {
    ++line_no;
    if (trim_whitespace(line).empty()) {
      continue;
    }

    JSONParser parser;
    if (!parser.parse(line.c_str(), static_cast<int>(line.size()))) {
      std::cerr << "Invalid JSON on line " << line_no
                << ": " << describe_json_parse_error(line)
                << std::endl;
      return -EINVAL;
    }
    if (!parser.is_object()) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": JSON value must be an object" << std::endl;
      return -EINVAL;
    }

    std::string damage_type;
    unsigned long long ino = 0;
    try {
      JSONDecoder::decode_json("damage_type", damage_type, &parser, true);
      JSONDecoder::decode_json("ino", ino, &parser, true);
    } catch (const JSONDecoder::err &e) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": " << e.what() << std::endl;
      return -EINVAL;
    }

    JSONObj *damage_type_obj = parser.find_obj("damage_type");
    ceph_assert(damage_type_obj != nullptr);
    if (!damage_type_obj->get_data_val().quoted) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": damage_type must be a string" << std::endl;
      return -EINVAL;
    }

    JSONObj *ino_obj = parser.find_obj("ino");
    ceph_assert(ino_obj != nullptr);
    if (ino_obj->get_data_val().quoted) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": ino must be a numeric JSON value" << std::endl;
      return -EINVAL;
    }

    if (trim_whitespace(damage_type).empty()) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": damage_type must be a non-empty string" << std::endl;
      return -EINVAL;
    }

    bool type_matched = damage_type_all;
    if (!damage_type_all) {
      type_matched = std::find(damage_type_tokens.begin(),
                               damage_type_tokens.end(),
                               damage_type) != damage_type_tokens.end();
    }
    if (!type_matched) {
      continue;
    }

    if ((candidate_index % m) == n) {
      dout(10) << "selected damage entry line=" << line_no
               << " damage_type='" << damage_type << "'"
               << " ino=0x" << std::hex << ino << std::dec
               << " candidate_index=" << candidate_index
               << " worker=" << n << "/" << m << dendl;

      int r = scan_extent_for_inode(static_cast<inodeno_t>(ino), data_ios);
      if (r < 0) {
        std::cerr << "Failed targeted extent scan for inode 0x" << std::hex
                  << ino << std::dec << ": " << cpp_strerror(r) << std::endl;
        return r;
      }
    }

    ++candidate_index;
  }

  return 0;
}

int DataScan::scan_extents_for_inode_file()
{
  if (m == 0) {
    std::cerr << "Invalid worker count " << m << ": must be > 0" << std::endl;
    return -EINVAL;
  }
  if (n >= m) {
    std::cerr << "Invalid worker number " << n
              << ": must be in range [0, " << (m - 1) << "]" << std::endl;
    return -EINVAL;
  }

  std::ifstream input(inode_file_path);
  if (!input.is_open()) {
    int err = errno ? -errno : -ENOENT;
    std::cerr << "Failed to open inode file '" << inode_file_path << "': "
              << cpp_strerror(err) << std::endl;
    return err;
  }

  std::vector<librados::IoCtx *> data_ios;
  data_ios.push_back(&data_io);
  for (auto &extra_data_io : extra_data_ios) {
    data_ios.push_back(&extra_data_io);
  }

  uint64_t candidate_index = 0;
  uint64_t line_no = 0;
  std::string line;
  while (std::getline(input, line)) {
    ++line_no;

    std::string trimmed = trim_whitespace(line);
    if (trimmed.empty()) {
      continue;
    }

    auto ino_val = ceph::parse<uint64_t>(trimmed);
    if (!ino_val) {
      std::cerr << "Invalid inode entry on line " << line_no
                << ": expected unsigned integer inode" << std::endl;
      return -EINVAL;
    }
    inodeno_t ino(*ino_val);

    if ((candidate_index % m) == n) {
      dout(10) << "selected inode entry line=" << line_no
               << " ino=0x" << std::hex
               << static_cast<uint64_t>(ino) << std::dec
               << " candidate_index=" << candidate_index
               << " worker=" << n << "/" << m << dendl;

      int r = scan_extent_for_inode(ino, data_ios);
      if (r < 0) {
        std::cerr << "Failed targeted extent scan for inode 0x" << std::hex
                  << static_cast<uint64_t>(ino) << std::dec << ": "
                  << cpp_strerror(r) << std::endl;
        return r;
      }
    }

    ++candidate_index;
  }

  return 0;
}

int DataScan::scan_inodes_for_damage_file() {
  bool roots_present;
  int r = driver->check_roots(&roots_present);
  if (r != 0) {
    derr << "Unexpected error checking roots: '" << cpp_strerror(r) << "'"
         << dendl;
    return r;
  }

  if (!roots_present) {
    std::cerr << "Some or all system inodes are absent.  Run 'init' from "
                 "one node before running 'scan_inodes'"
              << std::endl;
    return -EIO;
  }

  std::ifstream input(damage_file_path);
  if (!input.is_open()) {
    int err = errno ? -errno : -ENOENT;
    std::cerr << "Failed to open damage file '" << damage_file_path
              << "': " << cpp_strerror(err) << std::endl;
    return err;
  }

  uint64_t candidate_index = 0;
  uint64_t line_no = 0;
  std::string line;
  while (std::getline(input, line)) {
    ++line_no;
    if (trim_whitespace(line).empty()) {
      continue;
    }

    JSONParser parser;
    if (!parser.parse(line.c_str(), static_cast<int>(line.size()))) {
      std::cerr << "Invalid JSON on line " << line_no << ": "
                << describe_json_parse_error(line) << std::endl;
      return -EINVAL;
    }
    if (!parser.is_object()) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": JSON value must be an object" << std::endl;
      return -EINVAL;
    }

    std::string damage_type;
    unsigned long long ino = 0;
    try {
      JSONDecoder::decode_json("damage_type", damage_type, &parser, true);
      JSONDecoder::decode_json("ino", ino, &parser, true);
    } catch (const JSONDecoder::err &e) {
      std::cerr << "Invalid damage entry on line " << line_no << ": "
                << e.what() << std::endl;
      return -EINVAL;
    }

    JSONObj *damage_type_obj = parser.find_obj("damage_type");
    ceph_assert(damage_type_obj != nullptr);
    if (!damage_type_obj->get_data_val().quoted) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": damage_type must be a string" << std::endl;
      return -EINVAL;
    }

    JSONObj *ino_obj = parser.find_obj("ino");
    ceph_assert(ino_obj != nullptr);
    if (ino_obj->get_data_val().quoted) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": ino must be a numeric JSON value" << std::endl;
      return -EINVAL;
    }

    if (trim_whitespace(damage_type).empty()) {
      std::cerr << "Invalid damage entry on line " << line_no
                << ": damage_type must be a non-empty string" << std::endl;
      return -EINVAL;
    }

    bool type_matched = damage_type_all;
    if (!damage_type_all) {
      type_matched =
          std::find(damage_type_tokens.begin(), damage_type_tokens.end(),
                    damage_type) != damage_type_tokens.end();
    }
    if (!type_matched) {
      continue;
    }

    inodeno_t selected_ino = static_cast<inodeno_t>(ino);
    if ((candidate_index % m) == n) {
      dout(10) << "selected damage entry line=" << line_no << " damage_type='"
               << damage_type << "'"
               << " ino=0x" << std::hex << ino << std::dec
               << " candidate_index=" << candidate_index << " worker=" << n
               << "/" << m << dendl;
      const std::string oid = format_extent_oid(selected_ino, 0);
      r = scan_inode_from_oid(oid, selected_ino, 0,
                              force_restore_all_ancestors);
      if (r < 0) {
        return r;
      }
    }

    ++candidate_index;
  }

  return 0;
}

int DataScan::scan_inodes_for_inode_file() {
  bool roots_present;
  int r = driver->check_roots(&roots_present);
  if (r != 0) {
    derr << "Unexpected error checking roots: '" << cpp_strerror(r) << "'"
         << dendl;
    return r;
  }

  if (!roots_present) {
    std::cerr << "Some or all system inodes are absent.  Run 'init' from "
                 "one node before running 'scan_inodes'"
              << std::endl;
    return -EIO;
  }

  std::ifstream input(inode_file_path);
  if (!input.is_open()) {
    int err = errno ? -errno : -ENOENT;
    std::cerr << "Failed to open inode file '" << inode_file_path
              << "': " << cpp_strerror(err) << std::endl;
    return err;
  }

  uint64_t candidate_index = 0;
  uint64_t line_no = 0;
  std::string line;
  while (std::getline(input, line)) {
    ++line_no;

    std::string trimmed = trim_whitespace(line);
    if (trimmed.empty()) {
      continue;
    }

    auto ino_val = ceph::parse<uint64_t>(trimmed);
    if (!ino_val) {
      std::cerr << "Invalid inode entry on line " << line_no
                << ": expected unsigned integer inode" << std::endl;
      return -EINVAL;
    }

    inodeno_t selected_ino(*ino_val);
    if ((candidate_index % m) == n) {
      dout(10) << "selected inode entry line=" << line_no << " ino=0x"
               << std::hex << static_cast<uint64_t>(selected_ino) << std::dec
               << " candidate_index=" << candidate_index << " worker=" << n
               << "/" << m << dendl;
      const std::string oid = format_extent_oid(selected_ino, 0);
      r = scan_inode_from_oid(oid, selected_ino, 0,
                              force_restore_all_ancestors);
      if (r < 0) {
        return r;
      }
    }

    ++candidate_index;
  }

  return 0;
}

int DataScan::probe_filter(librados::IoCtx &ioctx)
{
  bufferlist filter_bl;
  ClsCephFSClient::build_tag_filter("test", &filter_bl);
  librados::ObjectCursor range_i;
  librados::ObjectCursor range_end;

  std::vector<librados::ObjectItem> tmp_result;
  librados::ObjectCursor tmp_next;
  int r = ioctx.object_list(ioctx.object_list_begin(), ioctx.object_list_end(),
                            1, filter_bl, &tmp_result, &tmp_next);

  return r >= 0;
}

int DataScan::forall_objects(
    librados::IoCtx &ioctx,
    bool untagged_only,
    std::function<int(std::string, uint64_t, uint64_t)> handler
    )
{
  librados::ObjectCursor range_i;
  librados::ObjectCursor range_end;
  ioctx.object_list_slice(
      ioctx.object_list_begin(),
      ioctx.object_list_end(),
      n,
      m,
      &range_i,
      &range_end);


  bufferlist filter_bl;

  bool legacy_filtering = false;
  if (untagged_only) {
    // probe to deal with older OSDs that don't support
    // the cephfs pgls filtering mode
    legacy_filtering = !probe_filter(ioctx);
    if (!legacy_filtering) {
      ClsCephFSClient::build_tag_filter(filter_tag, &filter_bl);
    }
  }

  int r = 0;
  while(range_i < range_end) {
    std::vector<librados::ObjectItem> result;
    int r = ioctx.object_list(range_i, range_end, 1,
                                filter_bl, &result, &range_i);
    if (r < 0) {
      derr << "Unexpected error listing objects: " << cpp_strerror(r) << dendl;
      return r;
    }

    for (const auto &i : result) {
      const std::string &oid = i.oid;
      uint64_t obj_name_ino = 0;
      uint64_t obj_name_offset = 0;
      r = parse_oid(oid, &obj_name_ino, &obj_name_offset);
      if (r != 0) {
        dout(4) << "Bad object name '" << oid << "', skipping" << dendl;
        continue;
      }

      if (untagged_only && legacy_filtering) {
        dout(20) << "Applying filter to " << oid << dendl;

        // We are only interested in 0th objects during this phase: we touched
        // the other objects during scan_extents
        if (obj_name_offset != 0) {
          dout(20) << "Non-zeroth object" << dendl;
          continue;
        }

        bufferlist scrub_tag_bl;
        int r = ioctx.getxattr(oid, "scrub_tag", scrub_tag_bl);
        if (r >= 0) {
          std::string read_tag;
          auto q = scrub_tag_bl.cbegin();
          try {
            decode(read_tag, q);
            if (read_tag == filter_tag) {
              dout(20) << "skipping " << oid << " because it has the filter_tag"
                       << dendl;
              continue;
            }
          } catch (const buffer::error &err) {
          }
          dout(20) << "read non-matching tag '" << read_tag << "'" << dendl;
        } else {
          dout(20) << "no tag read (" << r << ")" << dendl;
        }

      } else if (untagged_only) {
        ceph_assert(obj_name_offset == 0);
        dout(20) << "OSD matched oid " << oid << dendl;
      }

      int this_oid_r = handler(oid, obj_name_ino, obj_name_offset);
      if (r == 0 && this_oid_r < 0) {
        r = this_oid_r;
      }
    }
  }

  return r;
}

int DataScan::scan_inode_from_oid(const std::string &oid, uint64_t obj_name_ino,
                                  uint64_t obj_name_offset,
                                  bool force_restore_ancestors) {

    int r = 0;

    dout(10) << "handling object "
	     << std::hex << obj_name_ino << "." << obj_name_offset << std::dec
	     << dendl;

    AccumulateResult accum_res;
    inode_backtrace_t backtrace;
    file_layout_t loaded_layout = file_layout_t::get_default();
    std::string symlink;
    r = ClsCephFSClient::fetch_inode_accumulate_result(
        data_io, oid, &backtrace, &loaded_layout, &symlink, &accum_res);

    if (r == -EINVAL) {
      dout(4) << "Accumulated metadata missing from '"
              << oid << ", did you run scan_extents?" << dendl;
      return r;
    } else if (r < 0) {
      dout(4) << "Unexpected error loading accumulated metadata from '"
              << oid << "': " << cpp_strerror(r) << dendl;
      // FIXME: this creates situation where if a client has a corrupt
      // backtrace/layout, we will fail to inject it.  We should (optionally)
      // proceed if the backtrace/layout is corrupt but we have valid
      // accumulated metadata.
      return r;
    }

    const time_t file_mtime = accum_res.max_mtime;
    uint64_t file_size = 0;
    bool have_backtrace = !(backtrace.ancestors.empty());

    // This is the layout we will use for injection, populated either
    // from loaded_layout or from best guesses
    file_layout_t guessed_layout;
    if (accum_res.obj_pool_id == -1) {
      guessed_layout.pool_id = data_pool_id;
    } else {
      guessed_layout.pool_id = accum_res.obj_pool_id;

      librados::IoCtx ioctx;
      r = librados::Rados(data_io).ioctx_create2(guessed_layout.pool_id, ioctx);
      if (r != 0) {
	derr << "Unexpected error opening file data pool id="
	     << guessed_layout.pool_id << ": " << cpp_strerror(r) << dendl;
	return r;
      }

      bufferlist bl;
      int r = ioctx.getxattr(oid, "layout", bl);
      if (r < 0) {
	if (r != -ENODATA) {
	  derr << "Unexpected error reading layout for " << oid << ": "
	       << cpp_strerror(r) << dendl;
	  return r;
	}
      } else {
	try {
	  auto q = bl.cbegin();
	  decode(loaded_layout, q);
	} catch (ceph::buffer::error &e) {
	  derr << "Unexpected error decoding layout for " << oid << dendl;
	  return -EINVAL;
	}
      }
    }

    // Calculate file_size, guess the layout
    if (accum_res.ceiling_obj_index > 0) {
      uint32_t chunk_size = file_layout_t::get_default().object_size;
      // When there are multiple objects, the largest object probably
      // indicates the chunk size.  But not necessarily, because files
      // can be sparse.  Only make this assumption if size seen
      // is a power of two, as chunk sizes typically are.
      if ((accum_res.max_obj_size & (accum_res.max_obj_size - 1)) == 0) {
        chunk_size = accum_res.max_obj_size;
      }

      if (loaded_layout.pool_id == -1) {
        // If no stashed layout was found, guess it
        guessed_layout.object_size = chunk_size;
        guessed_layout.stripe_unit = chunk_size;
        guessed_layout.stripe_count = 1;
      } else if (!loaded_layout.is_valid() ||
          loaded_layout.object_size < accum_res.max_obj_size) {
        // If the max size seen exceeds what the stashed layout claims, then
        // disbelieve it.  Guess instead.  Same for invalid layouts on disk.
        dout(4) << "bogus xattr layout on 0x" << std::hex << obj_name_ino
                << std::dec << ", ignoring in favour of best guess" << dendl;
        guessed_layout.object_size = chunk_size;
        guessed_layout.stripe_unit = chunk_size;
        guessed_layout.stripe_count = 1;
      } else {
        // We have a stashed layout that we can't disprove, so apply it
        guessed_layout = loaded_layout;
        dout(20) << "loaded layout from xattr:"
          << " pi: " << guessed_layout.pool_id
          << " os: " << guessed_layout.object_size
          << " sc: " << guessed_layout.stripe_count
          << " su: " << guessed_layout.stripe_unit
          << dendl;
        // User might have transplanted files from a pool with a different
        // ID, so if the pool from loaded_layout is not found in the list of
        // the data pools, we'll force the injected layout to point to the
        // pool we read from.
	if (!fsmap->get_filesystem(fscid)->mds_map.is_data_pool(
	      guessed_layout.pool_id)) {
	  dout(20) << "overwriting layout pool_id " << data_pool_id << dendl;
	  guessed_layout.pool_id = data_pool_id;
	}
      }

      if (guessed_layout.stripe_count == 1) {
        // Unstriped file: simple chunking
        file_size = guessed_layout.object_size * accum_res.ceiling_obj_index
                    + accum_res.ceiling_obj_size;
      } else {
        // Striped file: need to examine the last stripe_count objects
        // in the file to determine the size.

	librados::IoCtx ioctx;
	if (guessed_layout.pool_id == data_io.get_id()) {
	  ioctx.dup(data_io);
	} else {
	  r = librados::Rados(data_io).ioctx_create2(guessed_layout.pool_id,
						     ioctx);
	  if (r != 0) {
	    derr << "Unexpected error opening file data pool id="
		 << guessed_layout.pool_id << ": " << cpp_strerror(r) << dendl;
	    return r;
	  }
	}

        // How many complete (i.e. not last stripe) objects?
        uint64_t complete_objs = 0;
        if (accum_res.ceiling_obj_index > guessed_layout.stripe_count - 1) {
          complete_objs = (accum_res.ceiling_obj_index / guessed_layout.stripe_count) * guessed_layout.stripe_count;
        } else {
          complete_objs = 0;
        }

        // How many potentially-short objects (i.e. last stripe set) objects?
        uint64_t partial_objs = accum_res.ceiling_obj_index + 1 - complete_objs;

        dout(10) << "calculating striped size from complete objs: "
                 << complete_objs << ", partial objs: " << partial_objs
                 << dendl;

        // Maximum amount of data that may be in the incomplete objects
        uint64_t incomplete_size = 0;

        // For each short object, calculate the max file size within it
        // and accumulate the maximum
        for (uint64_t i = complete_objs; i < complete_objs + partial_objs; ++i) {
          char buf[60];
          snprintf(buf, sizeof(buf), "%llx.%08llx",
              (long long unsigned)obj_name_ino, (long long unsigned)i);

          uint64_t osize(0);
          time_t omtime(0);
          r = ioctx.stat(std::string(buf), &osize, &omtime);
          if (r == 0) {
            if (osize > 0) {
              // Upper bound within this object
              uint64_t upper_size = (osize - 1) / guessed_layout.stripe_unit
                * (guessed_layout.stripe_unit * guessed_layout.stripe_count)
                + (i % guessed_layout.stripe_count)
                * guessed_layout.stripe_unit + (osize - 1)
                % guessed_layout.stripe_unit + 1;
              incomplete_size = std::max(incomplete_size, upper_size);
            }
          } else if (r == -ENOENT) {
            // Absent object, treat as size 0 and ignore.
          } else {
            // Unexpected error, carry r to outer scope for handling.
            break;
          }
        }
        if (r != 0 && r != -ENOENT) {
          derr << "Unexpected error checking size of ino 0x" << std::hex
               << obj_name_ino << std::dec << ": " << cpp_strerror(r) << dendl;
          return r;
        }
        file_size = complete_objs * guessed_layout.object_size
                    + incomplete_size;
      }
    } else {
      file_size = accum_res.ceiling_obj_size;
      if (loaded_layout.pool_id < 0
          || loaded_layout.object_size < accum_res.max_obj_size) {
        // No layout loaded, or inconsistent layout, use default
        guessed_layout = file_layout_t::get_default();
	guessed_layout.pool_id = accum_res.obj_pool_id != -1 ?
	  accum_res.obj_pool_id : data_pool_id;
      } else {
        guessed_layout = loaded_layout;
      }
    }

    // Santity checking backtrace ino against object name
    if (have_backtrace && backtrace.ino != obj_name_ino) {
      dout(4) << "Backtrace ino 0x" << std::hex << backtrace.ino
        << " doesn't match object name ino 0x" << obj_name_ino
        << std::dec << dendl;
      have_backtrace = false;
    }

    InodeStore dentry;
    build_file_dentry(obj_name_ino, file_size, file_mtime, guessed_layout, &dentry, symlink);

    // Inject inode to the metadata pool
    if (have_backtrace) {
      inode_backpointer_t root_bp = *(backtrace.ancestors.rbegin());
      if (MDS_INO_IS_MDSDIR(root_bp.dirino)) {
        /* Special case for strays: even if we have a good backtrace,
         * don't put it in the stray dir, because while that would technically
         * give it linkage it would still be invisible to the user */
        r = driver->inject_lost_and_found(obj_name_ino, dentry);
        if (r < 0) {
          dout(4) << "Error injecting 0x" << std::hex << backtrace.ino
            << std::dec << " into lost+found: " << cpp_strerror(r) << dendl;
          if (r == -EINVAL) {
            dout(4) << "Use --force-corrupt to overwrite structures that "
                       "appear to be corrupt" << dendl;
          }
        }
      } else {
        /* Happy case: we will inject a named dentry for this inode */
        r = driver->inject_with_backtrace(backtrace, dentry,
                                          force_restore_ancestors);
        if (r < 0) {
          dout(4) << "Error injecting 0x" << std::hex << backtrace.ino
            << std::dec << " with backtrace: " << cpp_strerror(r) << dendl;
          if (r == -EINVAL) {
            dout(4) << "Use --force-corrupt to overwrite structures that "
                       "appear to be corrupt" << dendl;
          }
        }
      }
    } else {
      /* Backtrace-less case: we will inject a lost+found dentry */
      r = driver->inject_lost_and_found(
          obj_name_ino, dentry);
      if (r < 0) {
        dout(4) << "Error injecting 0x" << std::hex << obj_name_ino
          << std::dec << " into lost+found: " << cpp_strerror(r) << dendl;
        if (r == -EINVAL) {
          dout(4) << "Use --force-corrupt to overwrite structures that "
                     "appear to be corrupt" << dendl;
        }
      }
    }

    return r;
}

int DataScan::scan_inodes() {
  bool roots_present;
  int r = driver->check_roots(&roots_present);
  if (r != 0) {
    derr << "Unexpected error checking roots: '" << cpp_strerror(r) << "'"
         << dendl;
    return r;
  }

  if (!roots_present) {
    std::cerr << "Some or all system inodes are absent.  Run 'init' from "
                 "one node before running 'scan_inodes'"
              << std::endl;
    return -EIO;
  }

  return forall_objects(data_io, true,
                        [this](std::string const &oid, uint64_t obj_name_ino,
                               uint64_t obj_name_offset) -> int {
                          return scan_inode_from_oid(oid, obj_name_ino,
                                                     obj_name_offset);
                        });
}

int DataScan::cleanup()
{
  // We are looking for only zeroth object
  //
  return forall_objects(data_io, true, [this](
        std::string const &oid,
        uint64_t obj_name_ino,
        uint64_t obj_name_offset) -> int
      {
      int r = 0;
      r = ClsCephFSClient::delete_inode_accumulate_result(data_io, oid);
      if (r < 0) {
      dout(4) << "Error deleting accumulated metadata from '"
      << oid << "': " << cpp_strerror(r) << dendl;
      }
      return r;
      });
}

bool DataScan::valid_ino(inodeno_t ino) const
{
  return (ino >= inodeno_t((1ull << 40)))
    || (MDS_INO_IS_STRAY(ino))
    || (MDS_INO_IS_MDSDIR(ino))
    || ino == CEPH_INO_ROOT
    || ino == CEPH_INO_CEPH
    || ino == CEPH_INO_LOST_AND_FOUND;
}

int DataScan::scan_links()
{
  MetadataDriver *metadata_driver = dynamic_cast<MetadataDriver*>(driver);
  if (!metadata_driver) {
    derr << "Unexpected --output-dir option for scan_links" << dendl;
    return -EINVAL;
  }

  interval_set<uint64_t> used_inos;
  map<inodeno_t, int> remote_links;
  map<snapid_t, SnapInfo> snaps;
  snapid_t last_snap = 1;
  snapid_t snaprealm_v2_since = 2;

  struct link_info_t {
    inodeno_t dirino;
    frag_t frag;
    string name;
    version_t version;
    int nlink;
    bool is_dir;
    map<snapid_t, SnapInfo> snaps;
    link_info_t() : version(0), nlink(0), is_dir(false) {}
    link_info_t(inodeno_t di, frag_t df, const string& n, const CInode::inode_const_ptr& i) :
      dirino(di), frag(df), name(n),
      version(i->version), nlink(i->nlink), is_dir(S_IFDIR & i->mode) {}
    dirfrag_t dirfrag() const {
      return dirfrag_t(dirino, frag);
    }
  };
  map<inodeno_t, list<link_info_t> > dup_primaries;
  map<inodeno_t, link_info_t> bad_nlink_inos;
  map<inodeno_t, link_info_t> injected_inos;

  map<dirfrag_t, set<string> > to_remove;

  enum {
    SCAN_INOS = 1,
    CHECK_LINK,
    REMOVE_DUP_DENTRIES,
    FIX_BAD_NLINK,
    FIX_INJECTED_DNFIRST,
  };

  const size_t phase_workers = std::max<uint32_t>(1, scan_links_thread_count);

  auto run_phase = [phase_workers](auto &&schedule, bool use_strand) -> int {
    std::unique_ptr<DataScanThreadPool> concurrent_runner =
        std::make_unique<DataScanThreadPool>(phase_workers);
    std::unique_ptr<DataScanThreadPool> strand_runner = nullptr;
    if (use_strand) {
      strand_runner = std::make_unique<DataScanThreadPool>(1);
    }

    int r = 0;
    int schedule_r = schedule(concurrent_runner, strand_runner);
    if (schedule_r < 0) {
      r = schedule_r;
    }

    concurrent_runner->wait_for_drain();
    int con_r = concurrent_runner->get_error();
    if (r == 0 && con_r < 0) {
      r = con_r;
    }
    if (use_strand)  {
      strand_runner->wait_for_drain();
      int str_r = strand_runner->get_error();
      if (r == 0 && str_r < 0) {
        r = str_r;
      }
    }
    concurrent_runner->shutdown();
    if (use_strand) {
      strand_runner->shutdown();
    }
    return r;
  };

  auto scan_dirfrag_slice = [this, &used_inos, &remote_links,
                             &dup_primaries, &bad_nlink_inos, &injected_inos,
                             &to_remove, &snaps, &last_snap,
                             &snaprealm_v2_since]
                             (int step, size_t worker_id,
                              size_t worker_count, 
                              std::unique_ptr <DataScanThreadPool>& strand_runner) -> int {
    librados::ObjectCursor range_i;
    librados::ObjectCursor range_end;
    metadata_io.object_list_slice(metadata_io.object_list_begin(),
                                  metadata_io.object_list_end(), worker_id,
                                  worker_count, &range_i, &range_end);

    const bufferlist filter_bl;
    bool success = true;
    while (range_i < range_end && success) {
      std::vector<librados::ObjectItem> result;
      int r = metadata_io.object_list(range_i, range_end, 1, filter_bl, &result,
                                      &range_i);
      if (r < 0) {
        derr << "Unexpected error listing objects: " << cpp_strerror(r)
             << dendl;
        return r;
      }

      for (const auto &item : result) {
        if (!success) {
          break;
        }
        const std::string &oid = item.oid;
        // std::cout << "phase-->" << step << ", oid-->" << oid << std::endl;

        dout(10) << "step " << step << ": handling object " << oid << dendl;

      uint64_t dir_ino = 0;
      uint64_t frag_id = 0;
      int r = parse_oid(oid, &dir_ino, &frag_id);
      if (r == -EINVAL) {
	dout(10) << "Not a dirfrag: '" << oid << "'" << dendl;
	continue;
      } else {
	// parse_oid can only do 0 or -EINVAL
	ceph_assert(r == 0);
      }

      if (!valid_ino(dir_ino)) {
	dout(10) << "Not a dirfrag (invalid ino): '" << oid << "'" << dendl;
	continue;
      }

      std::map<std::string, bufferlist> items;
      r = metadata_io.omap_get_vals(oid, "", (uint64_t)-1, &items);
      if (r < 0) {
	derr << "Error getting omap from '" << oid << "': " << cpp_strerror(r) << dendl;
	return r;
      }
      auto strand_task = [&used_inos, &remote_links, &dup_primaries,
                          &bad_nlink_inos, &injected_inos,
                          &to_remove, &snaps, &last_snap,
                          &snaprealm_v2_since, step = step,
                          dir_ino = dir_ino, frag_id = frag_id,
                          items = std::move(items)]() -> int {
      for (auto& p : items) {
	auto q = p.second.cbegin();
	string dname;
	snapid_t last;
	dentry_key_t::decode_helper(p.first, dname, last);
  // std::cout << "phase-->" << step << ", dir_ino-->" << dir_ino << ", dname-->" << dname << std::endl;

	if (last != CEPH_NOSNAP) {
	  if (last > last_snap)
	    last_snap = last;
	  continue;
	}

	try {
	  snapid_t dnfirst;
	  decode(dnfirst, q);
          if (dnfirst == CEPH_NOSNAP) {
            dout(20) << "injected ino detected" << dendl;
          } else if (dnfirst <= CEPH_MAXSNAP) {
	    if (dnfirst - 1 > last_snap)
	      last_snap = dnfirst - 1;
	  }
	  char dentry_type;
	  decode(dentry_type, q);
	  mempool::mds_co::string alternate_name;
	  if (dentry_type == 'I' || dentry_type == 'i') {
	    InodeStore inode;
            if (dentry_type == 'i') {
	      DECODE_START(2, q);
              if (struct_v >= 2)
                decode(alternate_name, q);
	      inode.decode(q);
	      DECODE_FINISH(q);
	    } else {
	      inode.decode_bare(q);
	    }

	    inodeno_t ino = inode.inode->ino;

	    if (step == SCAN_INOS) {
	      if (used_inos.contains(ino, 1)) {
		dup_primaries.emplace(std::piecewise_construct,
				      std::forward_as_tuple(ino),
				      std::forward_as_tuple());
	      } else {
		used_inos.insert(ino);
	      }
	    } else if (step == CHECK_LINK) {
	      sr_t srnode;
	      if (inode.snap_blob.length()) {
		auto p = inode.snap_blob.cbegin();
		decode(srnode, p);
		for (auto it = srnode.snaps.begin();
		     it != srnode.snaps.end(); ) {
		  if (it->second.ino != ino ||
		      it->second.snapid != it->first) {
		    srnode.snaps.erase(it++);
		  } else {
		    ++it;
		  }
		}
		if (!srnode.past_parents.empty()) {
		  snapid_t last = srnode.past_parents.rbegin()->first;
		  if (last + 1 > snaprealm_v2_since)
		    snaprealm_v2_since = last + 1;
		}
	      }
	      if (inode.old_inodes && !inode.old_inodes->empty()) {
		auto _last_snap = inode.old_inodes->rbegin()->first;
		if (_last_snap > last_snap)
		  last_snap = _last_snap;
	      }
	      auto q = dup_primaries.find(ino);
	      if (q != dup_primaries.end()) {
		q->second.push_back(link_info_t(dir_ino, frag_id, dname, inode.inode));
		q->second.back().snaps.swap(srnode.snaps);
	      } else {
		int nlink = 0;
		auto r = remote_links.find(ino);
		if (r != remote_links.end())
		  nlink = r->second;
		if (!MDS_INO_IS_STRAY(dir_ino))
		  nlink++;
		if (inode.inode->nlink != nlink) {
		  derr << "Bad nlink on " << ino << " expected " << nlink
		       << " has " << inode.inode->nlink << dendl;
		  bad_nlink_inos[ino] = link_info_t(dir_ino, frag_id, dname, inode.inode);
		  bad_nlink_inos[ino].nlink = nlink;
		}
		snaps.insert(make_move_iterator(begin(srnode.snaps)),
			     make_move_iterator(end(srnode.snaps)));
	      }
	      if (dnfirst == CEPH_NOSNAP) {
                injected_inos[ino] = link_info_t(dir_ino, frag_id, dname, inode.inode);
                dout(20) << "adding " << ino << " for future processing to fix dnfirst" << dendl;
              }
	    }
	  } else if (dentry_type == 'L' || dentry_type == 'l') {
	    inodeno_t ino;
	    unsigned char d_type;
            CDentry::decode_remote(dentry_type, ino, d_type, alternate_name, q);

	    if (step == SCAN_INOS) {
	      remote_links[ino]++;
	    } else if (step == CHECK_LINK) {
	      if (!used_inos.contains(ino, 1)) {
		derr << "Bad remote link dentry 0x" << std::hex << dir_ino
		     << std::dec << "/" << dname
		     << ", ino " << ino << " not found" << dendl;
		std::string key;
		dentry_key_t dn_key(CEPH_NOSNAP, dname.c_str());
		dn_key.encode(key);
		to_remove[dirfrag_t(dir_ino, frag_id)].insert(key);
	      }
	    }
	  } else {
	    derr << "Invalid tag char '" << dentry_type << "' dentry 0x" << dir_ino
		 << std::dec << "/" << dname << dendl;
	    return -EINVAL;
	  }
	} catch (const buffer::error &err) {
	  derr << "Error decoding dentry 0x" << std::hex << dir_ino
	       << std::dec << "/" << dname << dendl;
	  return -EINVAL;
	}
      }
      return 0;
    };
    success = strand_runner->submit(strand_task);
    }
  }
    return 0;
  };

  // Phase 1: SCAN_INOS
  // std::cout << "scan_inos-->" << std::endl;
  dout(0) << "scanning inode dentries" << dendl;
  int r = run_phase(
      [phase_workers, &scan_dirfrag_slice](
          std::unique_ptr<DataScanThreadPool> &concurrent_runner,
          std::unique_ptr<DataScanThreadPool> &strand_runner) {
        bool success = true;
        for (size_t worker = 0; worker < phase_workers && success; ++worker) {
          success = concurrent_runner->submit([&, worker]() {
            return scan_dirfrag_slice(SCAN_INOS, worker, phase_workers,
                                      strand_runner);
          });
        }
        return 0;
      },
      true);
  if (r < 0) {
    return r;
  }

  // Phase 2: CHECK_LINK
  // std::cout << "scan_links-->" << std::endl;
  dout(0) << "scanning link dentries" << dendl;
  r = run_phase(
      [phase_workers, &scan_dirfrag_slice](
          std::unique_ptr<DataScanThreadPool> &concurrent_runner,
          std::unique_ptr<DataScanThreadPool> &strand_runner) {
        bool success = true;
        for (size_t worker = 0; worker < phase_workers && success; ++worker) {
          success = concurrent_runner->submit([&, worker]() {
            return scan_dirfrag_slice(CHECK_LINK, worker, phase_workers,
                                      strand_runner);
          });
        }
        return 0;
      },
      true);
  if (r < 0) {
    return r;
  }

  map<unsigned, uint64_t> max_ino_map;
  {
    auto prev_max_ino = (uint64_t)1 << 40;
    for (auto p = used_inos.begin(); p != used_inos.end(); ++p) {
      auto cur_max = p.get_start() + p.get_len() - 1;
      if (cur_max < prev_max_ino)
	continue; // system inodes

      if ((prev_max_ino >> 40)  != (cur_max >> 40)) {
	unsigned rank = (prev_max_ino >> 40) - 1;
	max_ino_map[rank] = prev_max_ino;
      } else if ((p.get_start() >> 40) != (cur_max >> 40)) {
	unsigned rank = (p.get_start() >> 40) - 1;
	max_ino_map[rank] = ((uint64_t)(rank + 2) << 40) - 1;
      }
      prev_max_ino = cur_max;
    }
    unsigned rank = (prev_max_ino >> 40) - 1;
    max_ino_map[rank] = prev_max_ino;
  }

  used_inos.clear();

  dout(10) << "processing " << dup_primaries.size() << " dup_primaries, "
	   << remote_links.size() << " remote_links" << dendl;

  for (auto& p : dup_primaries) {

    dout(10) << "handling dup " << p.first << dendl;

    link_info_t newest;
    for (auto& q : p.second) {
      if (q.version > newest.version) {
	newest = q;
      } else if (q.version == newest.version &&
		 !MDS_INO_IS_STRAY(q.dirino) &&
		 MDS_INO_IS_STRAY(newest.dirino)) {
	newest = q;
      }
    }
    auto injected_it = injected_inos.find(p.first);
    if (injected_it != injected_inos.end()) {
      if (injected_it->second.dirino != newest.dirino ||
          injected_it->second.name != newest.name) {
        dout(10) << "Removing ino=" << p.first
                 << "from injected_inos as they will be removed anyway"
                 << dendl;
        injected_inos.erase(injected_it);
      }
    }

    for (auto& q : p.second) {
      // in the middle of dir fragmentation?
      if (newest.dirino == q.dirino && newest.name == q.name) {
	snaps.insert(make_move_iterator(begin(q.snaps)),
		     make_move_iterator(end(q.snaps)));
	continue;
      }

      std::string key;
      dentry_key_t dn_key(CEPH_NOSNAP, q.name.c_str());
      dn_key.encode(key);
      to_remove[q.dirfrag()].insert(key);
      derr << "Remove duplicated ino 0x" << p.first << " from "
	   << q.dirfrag() << "/" << q.name << dendl;
    }

    int nlink = 0;
    auto q = remote_links.find(p.first);
    if (q != remote_links.end())
      nlink = q->second;
    if (!MDS_INO_IS_STRAY(newest.dirino))
      nlink++;

    if (nlink != newest.nlink) {
      derr << "Bad nlink on " << p.first << " expected " << nlink
	   << " has " << newest.nlink << dendl;
      bad_nlink_inos[p.first] = newest;
      bad_nlink_inos[p.first].nlink = nlink;
    }
  }
  dup_primaries.clear();
  remote_links.clear();

  {
    objecter->with_osdmap([&](const OSDMap& o) {
      for (auto p : data_pools) {
	const pg_pool_t *pi = o.get_pg_pool(p);
	if (!pi)
	  continue;
	if (pi->snap_seq > last_snap)
	  last_snap = pi->snap_seq;
      }
    });

    if (!snaps.empty()) {
      if (snaps.rbegin()->first > last_snap)
	last_snap = snaps.rbegin()->first;
    }
  }

  dout(0) << "removing dup dentries from " << to_remove.size() << " objects"
	   << dendl;
  // std::cout << "remove_dup_dentries-->" << std::endl;

  // Phase 3: REMOVE_DUP_DENTRIES
  r = run_phase(
      [this, &to_remove](std::unique_ptr<DataScanThreadPool> &concurrent_runner,
                         std::unique_ptr<DataScanThreadPool> &strand_runner) {
        bool success = true;
        // std::cout << "to_remove.size()-->" << to_remove.size() << std::endl;
        for (auto it = to_remove.begin(); it != to_remove.end() && success;
             ++it) {

          auto *p = &(*it);
          success = concurrent_runner->submit([this, p]() {
            // std::cout << "to_remove-->" << p->first.ino << ", frag-->"
            //           << p->first.frag << std::endl;
            object_t frag_oid =
                InodeStore::get_object_name(p->first.ino, p->first.frag, "");

            dout(10) << "removing dup dentries from " << p->first << dendl;

            int phase_r = metadata_io.omap_rm_keys(frag_oid.name, p->second);
            if (phase_r != 0) {
              derr << "Error removing duplicated dentries from " << p->first
                   << dendl;
            }
            return phase_r;
          });
        }
        return 0;
      },
      false);
  if (r < 0) {
    return r;
  }
  to_remove.clear();

  dout(0) << "processing " << bad_nlink_inos.size() << " bad_nlink_inos"
           << dendl;

  // Phase 4: FIX_BAD_NLINK
  // std::cout << "fix_bad_nlink-->" << std::endl;
  r = run_phase(
      [this, &bad_nlink_inos,
       &metadata_driver](std::unique_ptr<DataScanThreadPool> &concurrent_runner,
                         std::unique_ptr<DataScanThreadPool> &strand_runner) {
        bool success = true;
        // std::cout << "bad_nlink_inos.size()-->" << bad_nlink_inos.size() << std::endl;
        for (auto it = bad_nlink_inos.begin();
             it != bad_nlink_inos.end() && success; ++it) {

          auto *p = &(*it);
          success = concurrent_runner->submit([this, &metadata_driver, p]() {
            // std::cout << "bad_nlink_ino-->" << p->first << std::endl;
            dout(10) << "handling bad_nlink_ino " << p->first << dendl;

            InodeStore inode;
            snapid_t first;
            int phase_r = read_dentry(p->second.dirino, p->second.frag,
                                      p->second.name, &inode, &first);
            if (phase_r < 0) {
              derr << "Unexpected error reading dentry " << p->second.dirfrag()
                   << "/" << p->second.name << ": " << cpp_strerror(phase_r)
                   << dendl;
              return phase_r;
            }

            if (inode.inode->ino != p->first ||
                inode.inode->version != p->second.version) {
              return 0;
            }

            inode.get_inode()->nlink = p->second.nlink;
            return metadata_driver->inject_linkage(
                p->second.dirino, p->second.name, p->second.frag, inode, first);
          });
        }
        return 0;
      },
      false);
  if (r < 0) {
    return r;
  }

  dout(0) << "processing " << injected_inos.size() << " injected_inos"
	   << dendl;

  // Phase 5: FIX_INJECTED_DNFIRST
  // std::cout << "fix_injected_dnfirst" << std::endl;
  r = run_phase(
      [this, &injected_inos, &metadata_driver,
       &last_snap](std::unique_ptr<DataScanThreadPool> &concurrent_runner,
                   std::unique_ptr<DataScanThreadPool> &strand_runner) {
        bool success = true;
        // std::cout << "injected_inos.size()-->" << injected_inos.size() << std::endl;
        for (auto it = injected_inos.begin();
             it != injected_inos.end() && success; ++it) {
          auto *p = &(*it);
          success = concurrent_runner->submit([this, &last_snap,
                                               &metadata_driver, p]() {
            // std::cout << "injected_ino-->" << p->first << std::endl;
            dout(10) << "handling injected_ino " << p->first << dendl;

            InodeStore inode;
            snapid_t first;
            dout(20) << " fixing linkage (dnfirst) of " << p->second.dirino
                     << ":" << p->second.name << dendl;
            int phase_r = read_dentry(p->second.dirino, p->second.frag,
                                      p->second.name, &inode, &first);
            if (phase_r < 0) {
              derr << "Unexpected error reading dentry " << p->second.dirfrag()
                   << "/" << p->second.name << ": " << cpp_strerror(phase_r)
                   << dendl;
              return 0;
            }

            if (first != CEPH_NOSNAP) {
              dout(20) << " ????" << dendl;
              return 0;
            }

            first = last_snap + 1;
            dout(20) << " first is now " << first << dendl;
            return metadata_driver->inject_linkage(
                p->second.dirino, p->second.name, p->second.frag, inode, first);
          });
        }
        return 0;
      },
      false);
  if (r < 0) {
    return r;
  }

  dout(10) << "updating inotable" << dendl;

  for (auto& p : max_ino_map) {
    InoTable inotable(nullptr);
    inotable.set_rank(p.first);
    bool dirty = false;
    int r = metadata_driver->load_table(&inotable);
    if (r < 0) {
      inotable.reset_state();
      dirty = true;
    }
    if (inotable.force_consume_to(p.second))
      dirty = true;
    if (dirty) {
      r = metadata_driver->save_table(&inotable);
      if (r < 0)
	return r;
    }
  }

  dout(10) << "updating snaptable" << dendl;

  {
    SnapServer snaptable;
    snaptable.set_rank(0);
    bool dirty = false;
    int r = metadata_driver->load_table(&snaptable);
    if (r < 0) {
      snaptable.reset_state();
      dirty = true;
    }
    if (snaptable.force_update(last_snap, snaprealm_v2_since, snaps))
      dirty = true;
    if (dirty) {
      r = metadata_driver->save_table(&snaptable);
      if (r < 0)
	return r;
    }
  }
  return 0;
}

int DataScan::scan_frags()
{
  bool roots_present;
  int r = driver->check_roots(&roots_present);
  if (r != 0) {
    derr << "Unexpected error checking roots: '"
      << cpp_strerror(r) << "'" << dendl;
    return r;
  }

  if (!roots_present) {
    std::cerr << "Some or all system inodes are absent.  Run 'init' from "
      "one node before running 'scan_inodes'" << std::endl;
    return -EIO;
  }

  return forall_objects(metadata_io, true,
                        [this](std::string const &oid, uint64_t obj_name_ino,
                               uint64_t obj_name_offset) -> int {
                          return scan_frag_from_oid(oid, obj_name_ino,
                                                    obj_name_offset);
                        });
}

int DataScan::scan_frag_from_oid(const std::string &oid, uint64_t obj_name_ino,
                                 uint64_t obj_name_offset,
                                 bool force_restore_ancestors,
                                 std::unordered_set<uint64_t> &&inode_set) {
  if (force_restore_ancestors &&
      inode_set.find(obj_name_ino) != inode_set.end()) {
    derr << "Found cycle while restoring ancestors" << dendl;
    return -EINVAL;
  }
    int r = 0;
    r = parse_oid(oid, &obj_name_ino, &obj_name_offset);
    if (r != 0) {
      dout(4) << "Bad object name '" << oid << "', skipping" << dendl;
      return r;
    }

    if (obj_name_ino < (1ULL << 40)) {
      // FIXME: we're skipping stray dirs here: if they're
      // orphaned then we should be resetting them some other
      // way
      dout(10) << "Skipping system ino " << obj_name_ino << dendl;
      return 0;
    }

    AccumulateResult accum_res;
    inode_backtrace_t backtrace;

    // Default to inherit layout (i.e. no explicit layout on dir) which is
    // expressed as a zeroed layout struct (see inode_t::has_layout)
    file_layout_t loaded_layout;

    int parent_r = 0;
    bufferlist parent_bl;
    int layout_r = 0;
    bufferlist layout_bl;
    bufferlist op_bl;

    librados::ObjectReadOperation op;
    op.getxattr("parent", &parent_bl, &parent_r);
    op.getxattr("layout", &layout_bl, &layout_r);
    r = metadata_io.operate(oid, &op, &op_bl);
    if (r != 0 && r != -ENODATA) {
      derr << "Unexpected error reading backtrace: " << cpp_strerror(parent_r) << dendl;
      return r;
    }

    if (parent_r != -ENODATA) {
      try {
        auto q = parent_bl.cbegin();
        backtrace.decode(q);
      } catch (buffer::error &e) {
        dout(4) << "Corrupt backtrace on '" << oid << "': " << e.what() << dendl;
        if (!force_corrupt) {
          return -EINVAL;
        } else {
          // Treat backtrace as absent: we'll inject into lost+found
          backtrace = inode_backtrace_t();
        }
      }
    }

    if (layout_r != -ENODATA) {
      try {
        auto q = layout_bl.cbegin();
        decode(loaded_layout, q);
      } catch (buffer::error &e) {
        dout(4) << "Corrupt layout on '" << oid << "': " << e.what() << dendl;
        if (!force_corrupt) {
          return -EINVAL;
        }
      }
    }

    bool have_backtrace = !(backtrace.ancestors.empty());

    // Santity checking backtrace ino against object name
    if (have_backtrace && backtrace.ino != obj_name_ino) {
      dout(4) << "Backtrace ino 0x" << std::hex << backtrace.ino
        << " doesn't match object name ino 0x" << obj_name_ino
        << std::dec << dendl;
      have_backtrace = false;
    }

    uint64_t fnode_version = 0;
    fnode_t fnode;
    r = read_fnode(obj_name_ino, frag_t(), &fnode, &fnode_version);
    if (r == -EINVAL) {
      derr << "Corrupt fnode on " << oid << dendl;
      if (force_corrupt) {
	fnode.fragstat.mtime = 0;
	fnode.fragstat.nfiles = 1;
	fnode.fragstat.nsubdirs = 0;
	fnode.accounted_fragstat = fnode.fragstat;
      } else {
        return r;
      }
    }

    InodeStore dentry;
    build_dir_dentry(obj_name_ino, fnode.accounted_fragstat,
		loaded_layout, &dentry);

    // Inject inode to the metadata pool
    if (have_backtrace) {
      inode_backpointer_t root_bp = *(backtrace.ancestors.rbegin());
      if (MDS_INO_IS_MDSDIR(root_bp.dirino)) {
        /* Special case for strays: even if we have a good backtrace,
         * don't put it in the stray dir, because while that would technically
         * give it linkage it would still be invisible to the user */
        r = driver->inject_lost_and_found(obj_name_ino, dentry);
        if (r < 0) {
          dout(4) << "Error injecting 0x" << std::hex << backtrace.ino
            << std::dec << " into lost+found: " << cpp_strerror(r) << dendl;
          if (r == -EINVAL) {
            dout(4) << "Use --force-corrupt to overwrite structures that "
                       "appear to be corrupt" << dendl;
          }
        }
      } else {
        /* Happy case: we will inject a named dentry for this inode */
        r = driver->inject_with_backtrace(
            backtrace, dentry, force_restore_ancestors, std::move(inode_set));
        if (r < 0) {
          dout(4) << "Error injecting 0x" << std::hex << backtrace.ino
            << std::dec << " with backtrace: " << cpp_strerror(r) << dendl;
          if (r == -EINVAL) {
            dout(4) << "Use --force-corrupt to overwrite structures that "
                       "appear to be corrupt" << dendl;
          }
        }
      }
    } else {
      /* Backtrace-less case: we will inject a lost+found dentry */
      r = driver->inject_lost_and_found(
          obj_name_ino, dentry);
      if (r < 0) {
        dout(4) << "Error injecting 0x" << std::hex << obj_name_ino
          << std::dec << " into lost+found: " << cpp_strerror(r) << dendl;
        if (r == -EINVAL) {
          dout(4) << "Use --force-corrupt to overwrite structures that "
                     "appear to be corrupt" << dendl;
        }
      }
    }

    return r;
}

int MetadataTool::read_fnode(
    inodeno_t ino, frag_t frag, fnode_t *fnode,
    uint64_t *last_version)
{
  ceph_assert(fnode != NULL);

  object_t frag_oid = InodeStore::get_object_name(ino, frag, "");
  bufferlist fnode_bl;
  int r = metadata_io.omap_get_header(frag_oid.name, &fnode_bl);
  *last_version = metadata_io.get_last_version();
  if (r < 0) {
    return r;
  }

  auto old_fnode_iter = fnode_bl.cbegin();
  try {
    (*fnode).decode(old_fnode_iter);
  } catch (const buffer::error &err) {
    return -EINVAL;
  }

  return 0;
}

int MetadataTool::read_dentry(inodeno_t parent_ino, frag_t frag,
                const std::string &dname, InodeStore *inode, snapid_t *dnfirst)
{
  ceph_assert(inode != NULL);

  std::string key;
  dentry_key_t dn_key(CEPH_NOSNAP, dname.c_str());
  dn_key.encode(key);

  std::set<std::string> keys;
  keys.insert(key);
  std::map<std::string, bufferlist> vals;
  object_t frag_oid = InodeStore::get_object_name(parent_ino, frag, "");
  int r = metadata_io.omap_get_vals_by_keys(frag_oid.name, keys, &vals);  
  dout(20) << "oid=" << frag_oid.name
           << " dname=" << dname
           << " frag=" << frag
           << ", r=" << r << dendl;
  if (r < 0) {
    return r;
  }

  if (vals.find(key) == vals.end()) {
    dout(20) << key << " not found in result" << dendl;
    return -ENOENT;
  }

  try {
    auto q = vals[key].cbegin();
    snapid_t first;
    decode(first, q);
    char dentry_type;
    decode(dentry_type, q);
    if (dentry_type == 'I' || dentry_type == 'i') {
      if (dentry_type == 'i') {
        mempool::mds_co::string alternate_name;

        DECODE_START(2, q);
        if (struct_v >= 2)
          decode(alternate_name, q);
        inode->decode(q);
        DECODE_FINISH(q);
      } else {
        inode->decode_bare(q);
      }
    } else {
      dout(20) << "dentry type '" << dentry_type << "': cannot"
                  "read an inode out of that" << dendl;
      return -EINVAL;
    }
    if (dnfirst)
      *dnfirst = first;
  } catch (const buffer::error &err) {
    dout(20) << "encoding error in dentry 0x" << std::hex << parent_ino
             << std::dec << "/" << dname << dendl;
    return -EINVAL;
  }

  return 0;
}

int MetadataDriver::load_table(MDSTable *table)
{
  object_t table_oid = table->get_object_name();

  bufferlist table_bl;
  int r = metadata_io.read(table_oid.name, table_bl, 0, 0);
  if (r < 0) {
    derr << "unable to read mds table '" << table_oid.name << "': "
      << cpp_strerror(r) << dendl;
    return r;
  }

  try {
    version_t table_ver;
    auto p = table_bl.cbegin();
    decode(table_ver, p);
    table->decode_state(p);
    table->force_replay_version(table_ver);
  } catch (const buffer::error &err) {
    derr << "unable to decode mds table '" << table_oid.name << "': "
      << err.what() << dendl;
    return -EIO;
  }
  return 0;
}

int MetadataDriver::save_table(MDSTable *table)
{
  object_t table_oid = table->get_object_name();

  bufferlist table_bl;
  encode(table->get_version(), table_bl);
  table->encode_state(table_bl);
  int r = metadata_io.write_full(table_oid.name, table_bl);
  if (r != 0) {
    derr << "error updating mds table " << table_oid.name
      << ": " << cpp_strerror(r) << dendl;
    return r;
  }
  return 0;
}

int MetadataDriver::inject_lost_and_found(
    inodeno_t ino, const InodeStore &dentry)
{
  // Create lost+found if doesn't exist
  bool created = false;
  int r = find_or_create_dirfrag(CEPH_INO_ROOT, frag_t(), &created);
  if (r < 0) {
    return r;
  }
  InodeStore lf_ino;
  r = read_dentry(CEPH_INO_ROOT, frag_t(), "lost+found", &lf_ino);
  if (r == -ENOENT || r == -EINVAL) {
    if (r == -EINVAL && !force_corrupt) {
      return r;
    }

    // To have a directory not specify a layout, give it zeros (see
    // inode_t::has_layout)
    file_layout_t inherit_layout;

    // Construct LF inode
    frag_info_t fragstat;
    fragstat.nfiles = 1,
    build_dir_dentry(CEPH_INO_LOST_AND_FOUND, fragstat, inherit_layout, &lf_ino);

    // Inject link to LF inode in the root dir
    r = inject_linkage(CEPH_INO_ROOT, "lost+found", frag_t(), lf_ino);
    if (r < 0) {
      return r;
    }
  } else {
    if (!(lf_ino.inode->mode & S_IFDIR)) {
      derr << "lost+found exists but is not a directory!" << dendl;
      // In this case we error out, and the user should do something about
      // this problem.
      return -EINVAL;
    }
  }

  r = find_or_create_dirfrag(CEPH_INO_LOST_AND_FOUND, frag_t(), &created);
  if (r < 0) {
    return r;
  }

  const std::string dname = lost_found_dname(ino);

  // Write dentry into lost+found dirfrag
  return inject_linkage(lf_ino.inode->ino, dname, frag_t(), dentry);
}


int MetadataDriver::get_frag_of(
    inodeno_t dirino,
    const std::string &target_dname,
    frag_t *result_ft)
{
  object_t root_frag_oid = InodeStore::get_object_name(dirino, frag_t(), "");

  dout(20) << "dirino=" << dirino << " target_dname=" << target_dname << dendl;

  // Find and load fragtree if existing dirfrag
  // ==========================================
  bool have_backtrace = false; 
  bufferlist parent_bl;
  int r = metadata_io.getxattr(root_frag_oid.name, "parent", parent_bl);
  if (r == -ENODATA) {
    dout(10) << "No backtrace on '" << root_frag_oid << "'" << dendl;
  } else if (r < 0) {
    dout(4) << "Unexpected error on '" << root_frag_oid << "': "
      << cpp_strerror(r) << dendl;
    return r;
  }

  // Deserialize backtrace
  inode_backtrace_t backtrace;
  if (parent_bl.length()) {
    try {
      auto q = parent_bl.cbegin();
      backtrace.decode(q);
      have_backtrace = true;
    } catch (buffer::error &e) {
      dout(4) << "Corrupt backtrace on '" << root_frag_oid << "': "
	      << e.what() << dendl;
    }
  }

  if (!(have_backtrace && backtrace.ancestors.size())) {
    // Can't work out fragtree without a backtrace
    dout(4) << "No backtrace on '" << root_frag_oid
            << "': cannot determine fragtree" << dendl;
    return -ENOENT;
  }

  // The parentage of dirino
  const inode_backpointer_t &bp = *(backtrace.ancestors.begin());

  // The inode of dirino's parent
  const inodeno_t parent_ino = bp.dirino;

  // The dname of dirino in its parent.
  const std::string &parent_dname = bp.dname;

  dout(20) << "got backtrace parent " << parent_ino << "/"
           << parent_dname << dendl;

  // The primary dentry for dirino
  InodeStore existing_dentry;

  // See if we can find ourselves in dirfrag zero of the parent: this
  // is a fast path that avoids needing to go further up the tree
  // if the parent isn't fragmented (worst case we would have to
  // go all the way to the root)
  r = read_dentry(parent_ino, frag_t(), parent_dname, &existing_dentry);
  if (r >= 0) {
    // Great, fast path: return the fragtree from here
    if (existing_dentry.inode->ino != dirino) {
      dout(4) << "Unexpected inode in dentry! 0x" << std::hex
              << existing_dentry.inode->ino
              << " vs expected 0x" << dirino << std::dec << dendl;
      return -ENOENT;
    }
    dout(20) << "fast path, fragtree is "
             << existing_dentry.dirfragtree << dendl;
    *result_ft = existing_dentry.pick_dirfrag(target_dname);
    dout(20) << "frag is " << *result_ft << dendl;
    return 0;
  } else if (r != -ENOENT) {
    // Dentry not present in 0th frag, must read parent's fragtree
    frag_t parent_frag;
    r = get_frag_of(parent_ino, parent_dname, &parent_frag);
    if (r == 0) {
      // We have the parent fragtree, so try again to load our dentry
      r = read_dentry(parent_ino, parent_frag, parent_dname, &existing_dentry);
      if (r >= 0) {
        // Got it!
        *result_ft = existing_dentry.pick_dirfrag(target_dname);
        dout(20) << "resolved via parent, frag is " << *result_ft << dendl;
        return 0;
      } else {
        if (r == -EINVAL || r == -ENOENT) {
          return -ENOENT;  // dentry missing or corrupt, so frag is missing
        } else {
          return r;
        }
      }
    } else {
      // Couldn't resolve parent fragtree, so can't find ours.
      return r;
    }
  } else if (r == -EINVAL) {
    // Unreadable dentry, can't know the fragtree.
    return -ENOENT;
  } else {
    // Unexpected error, raise it
    return r;
  }
}

int MetadataDriver::inject_with_backtrace(
    const inode_backtrace_t &backtrace, const InodeStore &dentry,
    bool force_inject_ancestors, std::unordered_set<uint64_t> &&inode_set) {

  // On dirfrags
  // ===========
  // In order to insert something into a directory, we first (ideally)
  // need to know the fragtree for the directory.  Sometimes we can't
  // get that, in which case we just go ahead and insert it into
  // fragment zero for a good chance of that being the right thing
  // anyway (most moderate-sized dirs aren't fragmented!)

  // On ancestry
  // ===========
  // My immediate ancestry should be correct, so if we can find that
  // directory's dirfrag then go inject it there.  This works well
  // in the case that this inode's dentry was somehow lost and we
  // are recreating it, because the rest of the hierarchy
  // will probably still exist.
  //
  // It's more of a "better than nothing" approach when rebuilding
  // a whole tree, as backtraces will in general not be up to date
  // beyond the first parent, if anything in the trace was ever
  // moved after the file was created.

  // On inode numbers
  // ================
  // The backtrace tells us inodes for each of the parents.  If we are
  // creating those parent dirfrags, then there is a risk that somehow
  // the inode indicated here was also used for data (not a dirfrag) at
  // some stage.  That would be a zany situation, and we don't check
  // for it here, because to do so would require extra IOs for everything
  // we inject, and anyway wouldn't guarantee that the inode number
  // wasn't in use in some dentry elsewhere in the metadata tree that
  // just happened not to have any data objects.

  // On multiple workers touching the same traces
  // ============================================
  // When creating linkage for a directory, *only* create it if we are
  // also creating the object.  That way, we might not manage to get the
  // *right* linkage for a directory, but at least we won't multiply link
  // it.  We assume that if a root dirfrag exists for a directory, then
  // it is linked somewhere (i.e. that the metadata pool is not already
  // inconsistent).
  //
  // Making sure *that* is true is someone else's job!  Probably someone
  // who is not going to run in parallel, so that they can self-consistently
  // look at versions and move things around as they go.
  // Note this isn't 100% safe: if we die immediately after creating dirfrag
  // object, next run will fail to create linkage for the dirfrag object
  // and leave it orphaned.

  inodeno_t ino = backtrace.ino;
  dout(10) << "  inode: 0x" << std::hex << ino << std::dec << dendl;
  for (std::vector<inode_backpointer_t>::const_iterator i = backtrace.ancestors.begin();
      i != backtrace.ancestors.end(); ++i) {
    if (force_inject_ancestors && !inode_set.insert(ino).second) {
      derr << "Found cycle while restoring ancestors" << dendl;
      return -EINVAL;
    }
    const inode_backpointer_t &backptr = *i;
    dout(10) << "  backptr: 0x" << std::hex << backptr.dirino << std::dec
      << "/" << backptr.dname << dendl;

    // Examine root dirfrag for parent
    const inodeno_t parent_ino = backptr.dirino;
    const std::string dname = backptr.dname;

    frag_t fragment;
    int r = get_frag_of(parent_ino, dname, &fragment);
    if (r == -ENOENT) {
      // Don't know fragment, fall back to assuming root
      dout(20) << "don't know fragment for 0x" << std::hex <<
        parent_ino << std::dec << "/" << dname << ", will insert to root"
        << dendl;
    }

    // Find or create dirfrag
    // ======================
    bool created_dirfrag;
    r = find_or_create_dirfrag(parent_ino, fragment, &created_dirfrag);
    if (r < 0) {
      return r;
    }

    // Check if dentry already exists
    // ==============================
    InodeStore existing_dentry;
    r = read_dentry(parent_ino, fragment, dname, &existing_dentry);
    bool write_dentry = false;
    if (r == -ENOENT || r == -EINVAL) {
      if (r == -EINVAL && !force_corrupt) {
        return r;
      }
      // Missing or corrupt dentry
      write_dentry = true;
    } else if (r < 0) {
      derr << "Unexpected error reading dentry 0x" << std::hex
        << parent_ino << std::dec << "/"
        << dname << ": " << cpp_strerror(r) << dendl;
      break;
    } else {
      // Dentry already present, does it link to me?
      if (existing_dentry.inode->ino == ino) {
        dout(20) << "Dentry 0x" << std::hex
          << parent_ino << std::dec << "/"
          << dname << " already exists and points to me" << dendl;
      } else {
        derr << "Dentry 0x" << std::hex
          << parent_ino << std::dec << "/"
          << dname << " already exists but points to 0x"
          << std::hex << existing_dentry.inode->ino << std::dec << dendl;
        // Fall back to lost+found!
        return inject_lost_and_found(backtrace.ino, dentry);
      }
    }

    // Inject linkage
    // ==============

    if (write_dentry) {
      if (i == backtrace.ancestors.begin()) {
        // This is the linkage for the file of interest
        dout(10) << "Linking inode 0x" << std::hex << ino
          << " at 0x" << parent_ino << "/" << dname << std::dec
          << " with size=" << dentry.inode->size << " bytes" << dendl;

        /* NOTE: dnfirst fixed in scan_links */
        r = inject_linkage(parent_ino, dname, fragment, dentry);
      } else {
        // This is the linkage for an ancestor directory
        dout(10) << "Linking ancestor directory of inode 0x" << std::hex << ino
                 << " at 0x" << std::hex << parent_ino
                 << ":" << dname << dendl;

        InodeStore ancestor_dentry;
        auto inode = ancestor_dentry.get_inode();
        inode->mode = 0755 | S_IFDIR;

        // Set nfiles to something non-zero, to fool any other code
        // that tries to ignore 'empty' directories.  This won't be
        // accurate, but it should avoid functional issues.

        inode->dirstat.nfiles = 1;
        inode->dir_layout.dl_dir_hash =
                               g_conf()->mds_default_dir_hash;

        inode->nlink = 1;
        inode->ino = ino;
        inode->uid = g_conf()->mds_root_ino_uid;
        inode->gid = g_conf()->mds_root_ino_gid;
        inode->version = 1;
        inode->backtrace_version = 1;
        /* NOTE: dnfirst fixed in scan_links */
        r = inject_linkage(parent_ino, dname, fragment, ancestor_dentry);
      }

      if (r < 0) {
        return r;
      }
    }

    if (!created_dirfrag) {
      // If the parent dirfrag already existed, then stop traversing the
      // backtrace: assume that the other ancestors already exist too.  This
      // is an assumption rather than a truth, but it's a convenient way
      // to avoid the risk of creating multiply-linked directories while
      // injecting data.  If there are in fact missing ancestors, this
      // should be fixed up using a separate tool scanning the metadata
      // pool.
      if (force_inject_ancestors) {
        return dscan->scan_frag_from_oid(
            InodeStore::get_object_name(parent_ino, frag_t(), "").name,
            parent_ino, 0, force_inject_ancestors, std::move(inode_set));
      } else {
        break;
      }
    } else {
      // Proceed up the backtrace, creating parents
      ino = parent_ino;
    }
  }

  return 0;
}

int MetadataDriver::find_or_create_dirfrag(
    inodeno_t ino,
    frag_t fragment,
    bool *created)
{
  ceph_assert(created != NULL);

  fnode_t existing_fnode;
  *created = false;

  uint64_t read_version = 0;
  int r = read_fnode(ino, fragment, &existing_fnode, &read_version);
  dout(10) << "read_version = " << read_version << dendl;

  if (r == -ENOENT || r == -EINVAL) {
    if (r == -EINVAL && !force_corrupt) {
      return r;
    }

    // Missing or corrupt fnode, create afresh
    bufferlist fnode_bl;
    fnode_t blank_fnode;
    blank_fnode.version = 1;
    // mark it as non-empty
    blank_fnode.fragstat.nfiles = 1;
    blank_fnode.accounted_fragstat = blank_fnode.fragstat;
    blank_fnode.damage_flags |= (DAMAGE_STATS | DAMAGE_RSTATS);
    blank_fnode.encode(fnode_bl);


    librados::ObjectWriteOperation op;

    if (read_version) {
      ceph_assert(r == -EINVAL);
      // Case A: We must assert that the version isn't changed since we saw the object
      // was unreadable, to avoid the possibility of two data-scan processes
      // both creating the frag.
      op.assert_version(read_version);
    } else {
      ceph_assert(r == -ENOENT);
      // Case B: The object didn't exist in read_fnode, so while creating it we must
      // use an exclusive create to correctly populate *creating with
      // whether we created it ourselves or someone beat us to it.
      op.create(true);
    }

    object_t frag_oid = InodeStore::get_object_name(ino, fragment, "");
    op.omap_set_header(fnode_bl);
    r = metadata_io.operate(frag_oid.name, &op);
    if (r == -EOVERFLOW || r == -EEXIST) {
      // Someone else wrote it (see case A above)
      dout(10) << "Dirfrag creation race: 0x" << std::hex
        << ino << " " << fragment << std::dec << dendl;
      *created = false;
      return 0;
    } else if (r < 0) {
      // We were unable to create or write it, error out
      derr << "Failed to create dirfrag 0x" << std::hex
        << ino << std::dec << ": " << cpp_strerror(r) << dendl;
      return r;
    } else {
      // Success: the dirfrag object now exists with a value header
      dout(10) << "Created dirfrag: 0x" << std::hex
        << ino << std::dec << dendl;
      *created = true;
    }
  } else if (r < 0) {
    derr << "Unexpected error reading dirfrag 0x" << std::hex
      << ino << std::dec << " : " << cpp_strerror(r) << dendl;
    return r;
  } else {
    dout(20) << "Dirfrag already exists: 0x" << std::hex
      << ino << " " << fragment << std::dec << dendl;
  }

  return 0;
}

int MetadataDriver::inject_linkage(
    inodeno_t dir_ino, const std::string &dname,
    const frag_t fragment, const InodeStore &inode, const snapid_t dnfirst)
{
  object_t frag_oid = InodeStore::get_object_name(dir_ino, fragment, "");

  std::string key;
  dentry_key_t dn_key(CEPH_NOSNAP, dname.c_str());
  dn_key.encode(key);

  bufferlist dentry_bl;
  encode(dnfirst, dentry_bl);
  encode('I', dentry_bl);
  inode.encode_bare(dentry_bl, CEPH_FEATURES_SUPPORTED_DEFAULT);

  // Write out
  std::map<std::string, bufferlist> vals;
  vals[key] = dentry_bl;
  int r = metadata_io.omap_set(frag_oid.name, vals);
  if (r != 0) {
    derr << "Error writing dentry 0x" << std::hex
      << dir_ino << std::dec << "/"
      << dname << ": " << cpp_strerror(r) << dendl;
    return r;
  } else {
    dout(20) << "Injected dentry 0x" << std::hex
      << dir_ino << "/" << dname << " pointing to 0x"
      << inode.inode->ino << std::dec << dendl;
    return 0;
  }
}


int MetadataDriver::init(
  librados::Rados &rados, std::string &metadata_pool_name, const FSMap *fsmap,
  fs_cluster_id_t fscid)
{
  if (metadata_pool_name.empty()) {
    auto fs =  fsmap->get_filesystem(fscid);
    ceph_assert(fs != nullptr);
    int64_t const metadata_pool_id = fs->mds_map.get_metadata_pool();

    dout(4) << "resolving metadata pool " << metadata_pool_id << dendl;
    int r = rados.pool_reverse_lookup(metadata_pool_id, &metadata_pool_name);
    if (r < 0) {
      derr << "Pool " << metadata_pool_id
	   << " identified in MDS map not found in RADOS!" << dendl;
      return r;
    }
    dout(4) << "found metadata pool '" << metadata_pool_name << "'" << dendl;
  } else {
    dout(4) << "forcing metadata pool '" << metadata_pool_name << "'" << dendl;
  }
  return rados.ioctx_create(metadata_pool_name.c_str(), metadata_io);
}

int LocalFileDriver::init(
  librados::Rados &rados, std::string &metadata_pool_name, const FSMap *fsmap,
  fs_cluster_id_t fscid)
{
  return 0;
}

int LocalFileDriver::inject_data(
    const std::string &file_path,
    uint64_t size,
    uint32_t chunk_size,
    inodeno_t ino)
{
  // Scrape the file contents out of the data pool and into the
  // local filesystem
  std::fstream f;
  f.open(file_path.c_str(), std::fstream::out | std::fstream::binary);

  for (uint64_t offset = 0; offset < size; offset += chunk_size) {
    bufferlist bl;

    char buf[32];
    snprintf(buf, sizeof(buf),
        "%llx.%08llx",
        (unsigned long long)ino,
        (unsigned long long)(offset / chunk_size));
    std::string oid(buf);

    int r = data_io.read(oid, bl, chunk_size, 0);

    if (r <= 0 && r != -ENOENT) {
      derr << "error reading data object '" << oid << "': "
        << cpp_strerror(r) << dendl;
      f.close();
      return r;
    } else if (r >=0) {
      
      f.seekp(offset);
      bl.write_stream(f);
    }
  }
  f.close();

  return 0;
}

int LocalFileDriver::inject_with_backtrace(
    const inode_backtrace_t &bt, const InodeStore &dentry,
    bool force_inject_ancestors, std::unordered_set<uint64_t> &&inode_set) {
  std::string path_builder = path;

  // Iterate through backtrace creating directory parents
  std::vector<inode_backpointer_t>::const_reverse_iterator i;
  for (i = bt.ancestors.rbegin();
      i != bt.ancestors.rend(); ++i) {

    const inode_backpointer_t &backptr = *i;
    path_builder += "/";
    path_builder += backptr.dname;

    // Last entry is the filename itself
    bool is_file = (i + 1 == bt.ancestors.rend());
    if (is_file) {
      // FIXME: inject_data won't cope with interesting (i.e. striped)
      // layouts (need a librados-compatible Filer to read these)
      inject_data(path_builder, dentry.inode->size,
		  dentry.inode->layout.object_size, bt.ino);
    } else {
      int r = mkdir(path_builder.c_str(), 0755);
      if (r != 0 && r != -EPERM) {
        derr << "error creating directory: '" << path_builder << "': "
          << cpp_strerror(r) << dendl;
        return r;
      }
    }
  }

  return 0;
}

int LocalFileDriver::inject_lost_and_found(
    inodeno_t ino,
    const InodeStore &dentry)
{
  std::string lf_path = path + "/lost+found";
  int r = mkdir(lf_path.c_str(), 0755);
  if (r != 0 && r != -EPERM) {
    derr << "error creating directory: '" << lf_path << "': "
      << cpp_strerror(r) << dendl;
    return r;
  }
  
  std::string file_path = lf_path + "/" + lost_found_dname(ino);
  return inject_data(file_path, dentry.inode->size,
		     dentry.inode->layout.object_size, ino);
}

int LocalFileDriver::init_roots(int64_t data_pool_id)
{
  // Ensure that the path exists and is a directory
  bool exists;
  int r = check_roots(&exists);
  if (r != 0) {
    return r;
  }

  if (exists) {
    return 0;
  } else {
    return ::mkdir(path.c_str(), 0755);
  }
}

int LocalFileDriver::check_roots(bool *result)
{
  // Check if the path exists and is a directory
  DIR *d = ::opendir(path.c_str());
  if (d == NULL) {
    *result = false;
  } else {
    int r = closedir(d);
    if (r != 0) {
      // Weird, but maybe possible with e.g. stale FD on NFS mount?
      *result = false;
    } else {
      *result = true;
    }
  }

  return 0;
}

void MetadataTool::build_file_dentry(
    inodeno_t ino, uint64_t file_size, time_t file_mtime,
    const file_layout_t &layout, InodeStore *out, std::string symlink)
{
  ceph_assert(out != NULL);

  auto inode = out->get_inode();
  if(!symlink.empty()) {
    inode->mode = 0777 | S_IFLNK;
    out->symlink = symlink;
  }
  else {
    inode->mode = 0500 | S_IFREG;
  }

  inode->size = file_size;
  inode->max_size_ever = file_size;
  inode->mtime.tv.tv_sec = file_mtime;
  inode->atime.tv.tv_sec = file_mtime;
  inode->ctime.tv.tv_sec = file_mtime;

  inode->layout = layout;

  inode->truncate_seq = 1;
  inode->truncate_size = -1ull;

  inode->inline_data.version = CEPH_INLINE_NONE;

  inode->nlink = 1;
  inode->ino = ino;
  inode->version = 1;
  inode->backtrace_version = 1;
  inode->uid = g_conf()->mds_root_ino_uid;
  inode->gid = g_conf()->mds_root_ino_gid;
}

void MetadataTool::build_dir_dentry(
    inodeno_t ino, const frag_info_t &fragstat,
    const file_layout_t &layout, InodeStore *out)
{
  ceph_assert(out != NULL);

  auto inode = out->get_inode();
  inode->mode = 0755 | S_IFDIR;
  inode->dirstat = fragstat;
  inode->mtime.tv.tv_sec = fragstat.mtime;
  inode->atime.tv.tv_sec = fragstat.mtime;
  inode->ctime.tv.tv_sec = fragstat.mtime;

  inode->layout = layout;
  inode->dir_layout.dl_dir_hash = g_conf()->mds_default_dir_hash;

  inode->truncate_seq = 1;
  inode->truncate_size = -1ull;

  inode->inline_data.version = CEPH_INLINE_NONE;

  inode->nlink = 1;
  inode->ino = ino;
  inode->version = 1;
  inode->backtrace_version = 1;
  inode->uid = g_conf()->mds_root_ino_uid;
  inode->gid = g_conf()->mds_root_ino_gid;
}
