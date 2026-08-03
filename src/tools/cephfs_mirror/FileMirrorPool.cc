// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab

#include "common/debug.h"
#include "Utils.h"

#include "FileMirrorPool.h"
#include "PeerReplayer.h"
#include <string>
using namespace std::string_literals;

#define dout_context g_ceph_context
#define dout_subsys ceph_subsys_cephfs_mirror
#undef dout_prefix
#define dout_prefix *_dout << "cephfs::mirror::FileMirrorPool " << __func__

namespace cephfs {
namespace mirror {

std::string FileMirrorPool::FileWorker::state_name[] = {
    "LOCKCONTEST",     "IDLE",
    "CONSUME",         "EXECUTE",
    "BACKOFF_CHECK1",  "BACKOFF_CHECK2",
    "FILE_OPEN_LOCAL", "FILE_OPEN_REMOTE",
    "FILE_READ",       "FILE_WRITE",
    "FILE_FTRUNC",     "FILE_FSYNC",
    "FREE_BUFFER",     "FILE_CLOSE_REMOTE",
    "FILE_CLOSE_LOCAL"};

FileMirrorPool::FileMirrorPool(int num_threads)
    : num_threads(num_threads), sync_count(0), active(false) {
  g_conf().add_observer(this);
  qlimit =
      g_ceph_context->_conf.get_val<uint64_t>("cephfs_mirror_file_queue_size");
}

void FileMirrorPool::activate() {
  {
    std::scoped_lock lock(mtx);
    active = true;
    file_workers = std::vector<FileWorker *>(num_threads);
    for (int i = 0; i < num_threads; ++i) {
      file_workers[i] = new FileWorker();
      file_workers[i]->activate();
    }
  }
  for (int i = 0; i < num_threads; ++i) {
    file_workers[i]->worker =
        std::thread(&FileMirrorPool::run, this, file_workers[i]);
  }
  dout(0) << ": " << num_threads << " number of threads are activated" << dendl;
}

void FileMirrorPool::deactivate() {
  {
    std::scoped_lock lock(mtx);
    active = false;
    for (auto &file_worker : file_workers) {
      file_worker->deactivate();
    }
  }
  drain_queue();

  for (auto &file_worker : file_workers) {
    file_worker->join();
  }
}

void FileMirrorPool::deactivate_queue(int sync_idx) {
  std::scoped_lock lock(mtx);
  ceph_assert(sync_idx >= 0 && sync_idx < sync_queues.size());
  auto sq = sync_queues[sync_idx];
  sq->active = false;
  sq->drain_queue();
  pick_cv.notify_all();
}

void FileMirrorPool::drain_queue(int idx) {
  {
    std::scoped_lock lock(mtx);
    if (idx == -1) {
      for (int i = 0; i < sync_queues.size(); ++i) {
        sync_queues[i]->drain_queue();
      }
    } else if (idx >= 0 && idx < sync_queues.size()){
      sync_queues[idx]->drain_queue();
    }
  }
  pick_cv.notify_all();
}

void FileMirrorPool::run(FileWorker *file_worker) {
  int sync_idx = -1;
  FileSyncMechanism *task;
  SyncQueue *sq = nullptr;
  bool sq_active = true;
  bool mirror_active = true;
  bool found_task = false;
  while (true) {
    file_worker->state = FileWorker::ThreadState::LOCKCONTEST;
    {
      std::unique_lock<std::mutex> lock(mtx);
      if (sq && found_task) {
        sq->dec_in_flight();
      }
      file_worker->state = FileWorker::ThreadState::IDLE;
      pick_cv.wait(lock, [this, file_worker] {
        return (file_worker->stop_called || !sync_ring.empty());
      });
      file_worker->state = FileWorker::ThreadState::CONSUME;
      if (file_worker->stop_called) {
        file_worker->force_deactivate();
        return;
      }
      sync_idx = sync_ring.front();
      sync_ring.pop();
      ceph_assert(sync_idx < sync_queues.size());
      sq = sync_queues[sync_idx];

      if (sq->sync_queue.empty()) {
        found_task = false;
        continue;
      }

      task = sq->sync_queue.front();
      file_worker->current_syncing_file.file_name = task->get_file_name();
      file_worker->current_syncing_file.peer_uid = task->get_peer_uid();
      file_worker->current_syncing_file.file_size = task->get_file_size();
      sq->sync_queue.pop();
      if (sq->sync_stat) {
        sq->sync_stat->current_stat.files_data_synced++;
        sq->sync_stat->current_stat.file_bytes_synced +=
            file_worker->current_syncing_file.file_size;
      }
      if (!sq->sync_queue.empty()) {
        sync_ring.emplace(sync_idx);
      }
      file_worker->state = FileWorker::ThreadState::EXECUTE;
      file_worker->file_transferred++;
      file_worker->bytes_synced = 0;
      task->set_worker_ref(file_worker);
      if (sq->sync_queue.size() < qlimit) {
        sq->give_cv.notify_one();
      }
      sq_active = sq->active;
      mirror_active = active;
      found_task = true;
    }
    task->local_stat = sq->local_stat;
    task->complete((sq_active && mirror_active) ? sq->get_low_level() : -1);
  }
}

void FileMirrorPool::sync_file_data(FileSyncMechanism *task, int sync_idx) {
  {
    std::unique_lock<std::mutex> lock(mtx);
    auto sq = sync_queues[sync_idx];
    if (sq->sync_stat) {
      sq->sync_stat->current_stat.files_in_flight++;
    }
    sq->give_cv.wait(lock, [this, &sq] {
      return (!active || !sq->active || sq->sync_queue.size() < qlimit);
    });
    if (!active || !sq->active) {
      task->complete(-1);
      sq->dec_in_flight();
      sq->give_cv.notify_all();
      return;
    }
    if (sq->sync_queue.empty()) {
      sync_ring.emplace(sync_idx);
    }
    sq->sync_queue.emplace(task);
  }
  pick_cv.notify_one();
}

void FileMirrorPool::sync_file_data(std::queue<FileSyncMechanism *> *batch,
                                    int sync_idx) {
  {
    std::unique_lock<std::mutex> lock(mtx);
    auto sq = sync_queues[sync_idx];
    if (sq->sync_stat) {
      sq->sync_stat->current_stat.files_in_flight += batch->size();
    }
    sq->give_cv.wait(lock, [this, &sq] {
      return (!active || !sq->active || sq->sync_queue.size() < qlimit);
    });
    if (!active || !sq->active) {
      while (!batch->empty()) {
        auto task = batch->front();
        task->complete(-1);
        sq->dec_in_flight();
        batch->pop();
      }
      sq->give_cv.notify_all();
      return;
    }
    if (sq->sync_queue.empty() && !batch->empty()) {
      sync_ring.emplace(sync_idx);
    }
    while (!batch->empty()) {
      auto task = batch->front();
      sq->sync_queue.emplace(task);
      batch->pop();
    }
  }
  pick_cv.notify_all();
}

int FileMirrorPool::sync_start(const std::string &dir_root,
                               const std::shared_ptr<SnapSyncStat> &sync_stat,
                               bool low_level) {
  std::scoped_lock lock(mtx);
  int sync_idx = 0;
  if (!unassigned_sync_ids.empty()) {
    sync_idx = unassigned_sync_ids.back();
    delete sync_queues[sync_idx];
    sync_queues[sync_idx] = nullptr;
    sync_queues[sync_idx] = new SyncQueue(dir_root, sync_stat);
    sync_queues[sync_idx]->set_low_level(low_level);
    unassigned_sync_ids.pop_back();
  } else {
    sync_idx = sync_count++;
    sync_queues.emplace_back(new SyncQueue(dir_root, sync_stat));
    sync_queues.back()->set_low_level(low_level);
  }
  return sync_idx;
}

void FileMirrorPool::sync_cleanup(int sync_idx) {
  ceph_assert(sync_idx >= 0 && sync_idx < sync_queues.size());
  sync_queues[sync_idx]->drain_queue();
  if (sync_queues[sync_idx]->sync_stat) {
    if (sync_queues[sync_idx]->local_stat) {
      sync_queues[sync_idx]->local_stat->flush(
          sync_queues[sync_idx]->sync_stat->current_stat);
    }
    sync_queues[sync_idx]->sync_stat = nullptr;
  }
  unassigned_sync_ids.push_back(sync_idx);
}

void FileMirrorPool::sync_finish(int sync_idx, const std::string &dir_root) {
  std::unique_lock<std::mutex> lock(mtx);
  ceph_assert(sync_idx >= 0 && sync_idx < sync_queues.size());
  auto sq = sync_queues[sync_idx];
  ceph_assert(sq->dir_root == dir_root);
  sq->done = true;
  pick_cv.notify_all();
  if (sq->sync_stat && sq->sync_stat->current_stat.files_in_flight == 0) {
    sq->sync_finish_cond->complete(0);
  }
  else {
    lock.unlock();
    sq->sync_finish_cond->wait();
    lock.lock();
  }
  sync_cleanup(sync_idx);
}

void FileMirrorPool::update_state(int thread_count) {
  std::unique_lock<std::mutex> lock(mtx);
  if (!active) {
    return;
  }
  if (thread_count == num_threads) {
    return;
  }

  if (thread_count < num_threads) {
    for (int i = thread_count; i < num_threads; ++i) {
      file_workers[i]->deactivate();
      dout(0) << ": Lazy shutdown of thread no " << i
              << " from file mirror threadpool" << dendl;
    }
    num_threads = thread_count;
    lock.unlock();
    pick_cv.notify_all();
  } else {
    int i;
    for (i = num_threads; i < file_workers.size() && num_threads < thread_count;
         ++i) {
      if (file_workers[i]->active) {
        std::swap(file_workers[i], file_workers[num_threads]);
        file_workers[num_threads++]->activate();
        dout(0) << ": Reactivating thread no " << i
                << " of file mirror threadpool" << dendl;
      }
    }
    for (i = (int)file_workers.size() - 1; i >= num_threads; --i) {
      if (num_threads == thread_count && file_workers[i]->active) {
        break;
      }
    }
    lock.unlock();
    pick_cv.notify_all();
    if (i < num_threads) {
      for (i = (int)file_workers.size() - 1; i >= num_threads; --i) {
        file_workers[i]->join();
        dout(0) << ": Force shutdown of already lazy shut thread having "
                   "thread no "
                << i << " from file mirror threadpool" << dendl;
        delete file_workers[i];
        file_workers[i] = nullptr;
        file_workers.pop_back();
      }
    }

    for (i = num_threads; i < thread_count; ++i) {
      file_workers.emplace_back(new FileWorker());
      file_workers[i]->activate();
      file_workers[i]->worker =
          std::thread(&FileMirrorPool::run, this, file_workers[i]);
      dout(0) << ": Creating thread no " << i << " in file mirror threadpool"
              << dendl;
    }
    num_threads = thread_count;
  }
  dout(0) << ": updating number of threads in file mirror threadpool to"
          << thread_count << dendl;
}

void FileMirrorPool::handle_conf_change(const ConfigProxy &conf,
                                      const std::set<std::string> &changed) {
  std::scoped_lock lock(config_mutex);
  if (changed.count("cephfs_mirror_file_sync_thread")) {
    update_state(g_ceph_context->_conf.get_val<uint64_t>(
        "cephfs_mirror_file_sync_thread"));
  }
  if (changed.count("cephfs_mirror_file_queue_size")) {
    std::scoped_lock lock(mtx);
    qlimit = g_ceph_context->_conf.get_val<uint64_t>(
        "cephfs_mirror_file_queue_size");
  }
  dout(0) << ": cephfs_mirror_file_sync_thread=" << num_threads
          << ", cephfs_mirror_file_queue_size=" << qlimit << dendl;
}

std::vector<std::string> FileMirrorPool::get_tracked_keys() const noexcept {
  return {"cephfs_mirror_file_sync_thread"s, "cephfs_mirror_file_queue_size"s};
}

FileMirrorPool::SyncQueue::SyncQueue(
    const std::string &dir_root, const std::shared_ptr<SnapSyncStat> &sync_stat)
    : dir_root(dir_root), sync_stat(sync_stat) {
  local_stat = new LocalSyncStat();
  sync_finish_cond = std::make_unique<C_SaferCond>();
  done = false;
  active = true;
}

FileMirrorPool::SyncQueue::~SyncQueue() {
  if (local_stat) {
    delete local_stat;
    local_stat = nullptr;
  }
}

void FileMirrorPool::SyncQueue::dec_in_flight() {
  if (sync_stat) {
    sync_stat->current_stat.files_in_flight--;
    if (done && sync_queue.empty() &&
        sync_stat->current_stat.files_in_flight == 0) {
      sync_finish_cond->complete(0);
    }
  }
}

void FileMirrorPool::SyncQueue::drain_queue() {
  while (!sync_queue.empty()) {
    auto &task = sync_queue.front();
    task->complete(-1);
    sync_queue.pop();
    dec_in_flight();
  }
  give_cv.notify_all();
}

void FileMirrorPool::dump_stats(Formatter *f) {
  std::scoped_lock lock(mtx);
  f->open_object_section("file_mirror_pool");
  for (int i = 0; i < num_threads; ++i) {
    f->open_object_section(std::to_string(i));
    file_workers[i]->dump(f);
    f->close_section();
  }
  f->close_section();
}

} // namespace mirror
} // namespace cephfs