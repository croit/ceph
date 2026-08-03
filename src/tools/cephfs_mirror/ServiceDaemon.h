// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab

#ifndef CEPHFS_MIRROR_SERVICE_DAEMON_H
#define CEPHFS_MIRROR_SERVICE_DAEMON_H

#include "common/ceph_mutex.h"
#include "common/Timer.h"
#include "mds/FSMap.h"
#include "Types.h"


namespace cephfs {
namespace mirror {

struct SnapSyncStat;

class ServiceDaemon : public md_config_obs_t{
public:
  ServiceDaemon(CephContext *cct, RadosRef rados);
  ~ServiceDaemon();
  using SnapSyncStatMap = std::map<std::string, std::shared_ptr<SnapSyncStat>>;
  using PeerInfo = std::pair<Attributes, SnapSyncStatMap>;
  int init();

  void add_filesystem(fs_cluster_id_t fscid, std::string_view fs_name);
  void remove_filesystem(fs_cluster_id_t fscid);

  void add_peer(fs_cluster_id_t fscid, const Peer &peer);
  void remove_peer(fs_cluster_id_t fscid, const Peer &peer);

  void add_or_update_fs_attribute(fs_cluster_id_t fscid, std::string_view key,
                                  AttributeValue value);
  void add_or_update_peer_attribute(fs_cluster_id_t fscid, const Peer &peer,
                                    std::string_view key, AttributeValue value);
  void update_peer_info(fs_cluster_id_t fscid, const Peer &peer,
                           const SnapSyncStatMap &status_map);
  std::string get_instance_id() const {
    return m_instance_id;
  }
  std::vector<std::string> get_tracked_keys() const noexcept override;
  void handle_conf_change(const ConfigProxy &conf,
                          const std::set<std::string> &changed) override;

private:
  struct Filesystem {
    std::string fs_name;
    Attributes fs_attributes;
    std::map<Peer, PeerInfo> peer_info;

    Filesystem(std::string_view fs_name)
      : fs_name(fs_name) {
    }
  };

  const std::string CEPHFS_MIRROR_AUTH_ID_PREFIX = "cephfs-mirror.";

  CephContext *m_cct;
  RadosRef m_rados;
  SafeTimer *m_timer;
  ceph::mutex m_timer_lock = ceph::make_mutex("cephfs::mirror::ServiceDaemon");

  ceph::mutex m_lock = ceph::make_mutex("cephfs::mirror::service_daemon");
  Context *m_timer_ctx = nullptr;
  std::map<fs_cluster_id_t, Filesystem> m_filesystems;
  int status_update_period = 2;
  std::string host_name = "none";
  std::string m_instance_id;
  std::mutex config_lock;

  void start_update_job();
  void update_status();
};

} // namespace mirror
} // namespace cephfs

#endif // CEPHFS_MIRROR_SERVICE_DAEMON_H
