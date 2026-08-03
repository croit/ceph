// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab


#ifndef CEPHFS_MIRROR_FILE_MIRROR_POOL_H
#define CEPHFS_MIRROR_FILE_MIRROR_POOL_H


#include "common/Formatter.h"
#include "common/Thread.h"
#include "common/config_obs.h"
#include "common/Cond.h"


namespace cephfs {
namespace mirror {

class FileSyncMechanism;
class SnapSyncStat;
class LocalSyncStat;

class FileMirrorPool : public md_config_obs_t{
  public:
    FileMirrorPool(int num_threads);
    std::vector<std::string> get_tracked_keys() const noexcept override;
    void handle_conf_change(const ConfigProxy &conf,
                            const std::set<std::string> &changed) override;
    void activate();
    void deactivate();
    void deactivate_queue(int sync_idx);
    void sync_file_data(FileSyncMechanism *task, int sync_idx);
    void sync_file_data(std::queue<FileSyncMechanism *> *batch, int sync_idx);
    int sync_start(const std::string &dir_root,
                   const std::shared_ptr<SnapSyncStat> &sync_stat,
                   bool low_level = false);
    void sync_finish(int idx, const std::string &dir_root);
    void update_state(int thread_count);
    void drain_queue(int idx = -1);
    void sync_cleanup(int sync_idx);
    void dump_stats(Formatter *f);
    struct FileWorker {
      struct FileInfo {
        std::string file_name;
        std::string peer_uid;
        uint64_t file_size = 0;
      };
      std::thread worker;
      enum class ThreadState {
        LOCKCONTEST,
        IDLE,
        CONSUME,
        EXECUTE,
        BACKOFF_CHECK1,
        BACKOFF_CHECK2,
        FILE_OPEN_LOCAL,
        FILE_OPEN_REMOTE,
        FILE_READ,
        FILE_WRITE,
        FILE_FTRUNC,
        FILE_FSYNC,
        FREE_BUFFER,
        FILE_CLOSE_REMOTE,
        FILE_CLOSE_LOCAL
      };
      static std::string state_name[];
      uint64_t file_transferred = 0;
      FileInfo current_syncing_file;
      uint64_t bytes_synced = 0;
      ThreadState state = ThreadState::LOCKCONTEST;
      bool stop_called = true;
      bool active = false;
      void dump(Formatter *f) {
        f->dump_unsigned("file_transferred", file_transferred);
        f->dump_string("thread_state", state_name[static_cast<int>(state)]);
        f->dump_string("current_syncing_file", current_syncing_file.file_name);
        f->dump_string("peer", current_syncing_file.peer_uid);
        f->dump_unsigned("file_size", current_syncing_file.file_size);
        f->dump_unsigned("bytes_synced", bytes_synced);
      }
      void activate() { active = true, stop_called = false; }
      void deactivate() { stop_called = true; }
      void force_deactivate() { active = false, stop_called = true; }
      void join() {
        if (worker.joinable()) {
          worker.join();
        }
      }
    };

  private:
    struct SyncQueue {
      std::string dir_root;
      std::queue<FileSyncMechanism *> sync_queue;
      std::condition_variable give_cv;
      bool low_level = false;
      std::shared_ptr<SnapSyncStat> sync_stat = nullptr;
      LocalSyncStat* local_stat = nullptr;
      bool done = false;
      bool active = true;
      std::unique_ptr<C_SaferCond> sync_finish_cond;
      SyncQueue(const std::string &dir_root,
                const std::shared_ptr<SnapSyncStat> &sync_stat);
      ~SyncQueue();
      SyncQueue(const SyncQueue &other)
          : dir_root(other.dir_root), sync_queue(other.sync_queue),
            low_level(other.low_level), sync_stat(other.sync_stat),
            local_stat(local_stat), done(other.done), active(other.active) {
        sync_finish_cond = std::make_unique<C_SaferCond>();
      }

      SyncQueue& operator=(const SyncQueue& other) {
        if (this != &other) {
          dir_root = other.dir_root;
          sync_queue = other.sync_queue;
          low_level = other.low_level;
          sync_stat = other.sync_stat;
          local_stat = other.local_stat;
          done = other.done;
          active = other.active;
          sync_finish_cond = std::make_unique<C_SaferCond>();
        }
        return *this;
      }
      void drain_queue();
      void set_low_level(bool _low_level) { low_level = _low_level; }
      bool get_low_level() { return low_level; }
      void dec_in_flight();
    };

    void run(FileWorker* file_worker);
    int num_threads;
    std::queue<int> sync_ring;
    std::vector <SyncQueue*> sync_queues;
    std::condition_variable pick_cv;
    std::mutex mtx;
    std::mutex config_mutex;
    int qlimit;
    std::vector<FileWorker *> file_workers;
    std::vector<int> unassigned_sync_ids;
    int sync_count = 0;
    bool active;
  };

} // namespace mirror
} // namespace cephfs

#endif // CEPHFS_MIRROR_PEER_REPLAYER_H