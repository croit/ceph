// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab
#include "ServiceDaemon.h"
#include "PeerReplayer.h"
#include "common/debug.h"
#include "common/errno.h"
#include "common/Timer.h"
#include "include/Context.h"
#include "include/stringify.h"

#include <string>
using namespace std::string_literals;

#define dout_context g_ceph_context
#define dout_subsys ceph_subsys_cephfs_mirror
#undef dout_prefix
#define dout_prefix *_dout << "cephfs::mirror::ServiceDaemon: " << this << " " \
                           << __func__

namespace cephfs {
namespace mirror {

namespace {

struct AttributeDumpVisitor : public boost::static_visitor<void> {
  ceph::Formatter *f;
  std::string name;

  AttributeDumpVisitor(ceph::Formatter *f, std::string_view name)
    : f(f), name(name) {
  }

  void operator()(bool val) const {
    f->dump_bool(name.c_str(), val);
  }
  void operator()(uint64_t val) const {
    f->dump_unsigned(name.c_str(), val);
  }
  void operator()(const std::string &val) const {
    f->dump_string(name.c_str(), val);
  }
};

} // anonymous namespace

ServiceDaemon::ServiceDaemon(CephContext *cct, RadosRef rados)
  : m_cct(cct),
    m_rados(rados),
    m_timer(new SafeTimer(cct, m_timer_lock, false)) {
  g_conf().add_observer(this);
  char hostname[HOST_NAME_MAX];
  if (gethostname(hostname, sizeof(hostname)) == 0) {
    host_name = std::string(hostname);
  }
}

ServiceDaemon::~ServiceDaemon() {
  dout(10) << dendl;
  {
    std::scoped_lock timer_lock(m_timer_lock);
    if (m_timer_ctx != nullptr) {
      dout(5) << ": canceling timer task=" << m_timer_ctx << dendl;
      m_timer->cancel_event(m_timer_ctx);
    }
    m_timer->shutdown();
  }

  delete m_timer;
}

int ServiceDaemon::init() {
  dout(20) << dendl;

  std::string id = m_cct->_conf->name.get_id();
  if (id.find(CEPHFS_MIRROR_AUTH_ID_PREFIX) == 0) {
    id = id.substr(CEPHFS_MIRROR_AUTH_ID_PREFIX.size());
  }
  std::string instance_id = stringify(m_rados->get_instance_id());

  std::map<std::string, std::string> service_metadata = {{"id", id},
                                                         {"instance_id", instance_id}};
  int r = m_rados->service_daemon_register("cephfs-mirror", instance_id,
                                           service_metadata);
  if (r < 0) {
    return r;
  }
  m_timer->init();
  status_update_period = g_ceph_context->_conf.get_val<uint64_t>(
      "cephfs_mirror_mirror_status_update_period");
  start_update_job();
  return 0;
}

void ServiceDaemon::add_filesystem(fs_cluster_id_t fscid, std::string_view fs_name) {
  dout(10) << ": fscid=" << fscid << ", fs_name=" << fs_name << dendl;

  {
    std::scoped_lock locker(m_lock);
    m_filesystems.emplace(fscid, Filesystem(fs_name));
  }
}

void ServiceDaemon::remove_filesystem(fs_cluster_id_t fscid) {
  dout(10) << ": fscid=" << fscid << dendl;

  {
    std::scoped_lock locker(m_lock);
    m_filesystems.erase(fscid);
  }
}

void ServiceDaemon::add_peer(fs_cluster_id_t fscid, const Peer &peer) {
  dout(10) << ": peer=" << peer << dendl;

  {
    std::scoped_lock locker(m_lock);
    auto fs_it = m_filesystems.find(fscid);
    if (fs_it == m_filesystems.end()) {
      return;
    }
    fs_it->second.peer_info.emplace(peer, PeerInfo());
  }
}

void ServiceDaemon::remove_peer(fs_cluster_id_t fscid, const Peer &peer) {
  dout(10) << ": peer=" << peer << dendl;

  {
    std::scoped_lock locker(m_lock);
    auto fs_it = m_filesystems.find(fscid);
    if (fs_it == m_filesystems.end()) {
      return;
    }
    fs_it->second.peer_info.erase(peer);
  }
}

void ServiceDaemon::add_or_update_fs_attribute(fs_cluster_id_t fscid, std::string_view key,
                                               AttributeValue value) {
  dout(10) << ": fscid=" << fscid << dendl;

  {
    std::scoped_lock locker(m_lock);
    auto fs_it = m_filesystems.find(fscid);
    if (fs_it == m_filesystems.end()) {
      return;
    }

    fs_it->second.fs_attributes[std::string(key)] = value;
  }
}

void ServiceDaemon::add_or_update_peer_attribute(fs_cluster_id_t fscid, const Peer &peer,
                                                 std::string_view key, AttributeValue value) {
  dout(10) << ": fscid=" << fscid << dendl;

  {
    std::scoped_lock locker(m_lock);
    auto fs_it = m_filesystems.find(fscid);
    if (fs_it == m_filesystems.end()) {
      return;
    }

    auto peer_it = fs_it->second.peer_info.find(peer);
    if (peer_it == fs_it->second.peer_info.end()) {
      return;
    }

    peer_it->second.first[std::string(key)] = value;
  }
}

void ServiceDaemon::update_peer_info(fs_cluster_id_t fscid, const Peer &peer,
                                       const SnapSyncStatMap &status_map) {
  dout(10) << ": fscid=" << fscid << dendl;
  {
    std::scoped_lock locker(m_lock);
    auto fs_it = m_filesystems.find(fscid);
    if (fs_it == m_filesystems.end()) {
      return;
    }
    auto peer_it = fs_it->second.peer_info.find(peer);
    if (peer_it == fs_it->second.peer_info.end()) {
      return;
    }
    peer_it->second.second = status_map;
  }
}

void ServiceDaemon::handle_conf_change(const ConfigProxy &conf,
                                      const std::set<std::string> &changed) {
  std::scoped_lock lock(config_lock);
  if (changed.count("cephfs_mirror_mirror_status_update_period")) {
    status_update_period = g_ceph_context->_conf.get_val<uint64_t>(
        "cephfs_mirror_mirror_status_update_period");
    {
      std::scoped_lock timer_lock(m_timer_lock);
      m_timer->cancel_all_events();
    }
    start_update_job();
  }
}

std::vector<std::string> ServiceDaemon::get_tracked_keys() const noexcept {
  return {
    "cephfs_mirror_mirror_status_update_period"s
  };
}

void ServiceDaemon::start_update_job() {
  dout(10) << dendl;

  std::scoped_lock timer_lock(m_timer_lock);
  update_status();
  m_timer_ctx = new LambdaContext([this] { start_update_job(); });
  m_timer->add_event_after(status_update_period, m_timer_ctx);
}

void ServiceDaemon::update_status() {
  dout(20) << ": " << m_filesystems.size() << " filesystem(s)" << dendl;
  // dout(0) << ": updating status-->" << dendl;

  ceph::JSONFormatter f;
  {
    std::scoped_lock locker(m_lock);
    f.open_object_section("filesystems");
    for (auto &[fscid, filesystem] : m_filesystems) {
      f.open_object_section(stringify(fscid).c_str());
      f.dump_string("name", filesystem.fs_name);
      for (auto &[attr_name, attr_value] : filesystem.fs_attributes) {
            AttributeDumpVisitor visitor(&f, attr_name);
            boost::apply_visitor(visitor, attr_value);
      }
      f.open_object_section("peers");
      for (auto &[peer, info] : filesystem.peer_info) {
        f.open_object_section(peer.uuid);
        f.dump_object("remote", peer.remote);
        f.open_object_section("stats");
        for (auto &[attr_name, attr_value] : info.first) {
            AttributeDumpVisitor visitor(&f, attr_name);
            boost::apply_visitor(visitor, attr_value);
        }
        f.dump_string("hostname", host_name);
        f.open_object_section("status");
        for (auto &[dir, status] : info.second) {
          f.open_object_section(dir);
          status->dump_stats(&f);
          f.close_section(); // dir_root
        }
        f.close_section();
        f.close_section(); // stats
        f.close_section(); // peer.uuid
      }
      f.close_section(); // peers
      f.close_section(); // fscid
    }
    f.close_section(); // filesystems
  }

  std::stringstream ss;
  f.flush(ss);

  int r = m_rados->service_daemon_update_status({{"status_json", ss.str()}});
  if (r < 0) {
    derr << ": failed to update service daemon status: " << cpp_strerror(r)
         << dendl;
  }
}

} // namespace mirror
} // namespace cephfs
