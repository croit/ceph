// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab
/*
 * Admin-socket controlled delay points for reproducing MDS lock races.
 *
 * Everything here is disarmed by default and is only armed through the
 * "unlink_hold_cap_deferral" admin socket command.  The delay points never create, drop or
 * edit lock state themselves; they only delay work that the MDS would perform
 * anyway:
 *
 *  - unlink_hold: after an unlink whose target dentry has the armed name has
 *    acquired all of its locks through the normal acquire_locks() path, park
 *    the request.  Releasing it re-dispatches the request through the normal
 *    retry path, which continues exactly where it stopped.
 *
 *  - defer_caps: queue MClientCaps / MClientCapRelease messages from one
 *    client (as a slow client or network would).  Releasing it delivers the
 *    queued messages, in order, through Locker::dispatch().
 *
 * All state is accessed under mds_lock.
 */

#pragma once

#include <deque>
#include <string>
#include <vector>

#include "common/Clock.h"
#include "common/Formatter.h"
#include "messages/MClientCapRelease.h"
#include "messages/MClientCaps.h"
#include "mds/MDCache.h"
#include "mds/Mutation.h"

namespace unlink_hold_cap_deferral {

struct State {
  // unlink_hold
  std::string unlink_hold_name;     // armed while non-empty
  MDRequestRef unlink_held;
  // defer_caps
  int64_t defer_caps_client = -1;   // armed while >= 0
  std::deque<cref_t<Message>> deferred_caps;
  // ordered, timestamped record of fault point activity
  std::vector<std::string> events;

  void event(const std::string& what) {
    std::ostringstream ss;
    ss << ceph_clock_now() << " " << what;
    events.push_back(ss.str());
  }
};

inline State state;

// Called by Server::handle_client_unlink right after it acquired its locks.
// Returns true if the request was parked and the caller must return.
inline bool maybe_hold_unlink(const MDRequestRef& mdr, std::string_view dname)
{
  if (state.unlink_hold_name.empty() || state.unlink_held ||
      dname != state.unlink_hold_name)
    return false;
  state.unlink_hold_name.clear();   // one shot
  state.unlink_held = mdr;
  mdr->mark_event("unlink_hold_cap_deferral: unlink held after acquiring locks");
  std::ostringstream ss;
  ss << "unlink_hold: parked " << mdr->reqid << " on dentry " << dname;
  state.event(ss.str());
  return true;
}

// Called by Locker for client capability messages.  Returns true if the
// message was queued and the caller must not process it now.
inline bool maybe_defer_caps(const cref_t<Message>& m)
{
  if (state.defer_caps_client < 0 ||
      !m->get_source().is_client() ||
      m->get_source().num() != state.defer_caps_client)
    return false;
  state.deferred_caps.push_back(m);
  std::ostringstream ss;
  ss << "defer_caps: queued " << m->get_type_name() << " from "
     << m->get_source();
  if (auto caps = dynamic_cast<const MClientCaps*>(m.get()))
    ss << " op " << ceph_cap_op_name(caps->get_op())
       << " ino " << caps->get_ino()
       << " caps " << ccap_string(caps->get_caps());
  state.event(ss.str());
  return true;
}

inline void dump(Formatter* f)
{
  f->open_object_section("unlink_hold_cap_deferral");
  f->dump_string("unlink_hold_armed_name", state.unlink_hold_name);
  f->dump_bool("unlink_held", bool(state.unlink_held));
  if (state.unlink_held)
    f->dump_stream("unlink_held_reqid") << state.unlink_held->reqid;
  f->dump_int("defer_caps_client", state.defer_caps_client);
  f->dump_unsigned("deferred_caps", state.deferred_caps.size());
  f->open_array_section("events");
  for (const auto& e : state.events)
    f->dump_string("event", e);
  f->close_section();
  f->close_section();
}

// Admin socket handler; caller holds mds_lock.
template <typename Rank>
int command(Rank* rank, const std::string& action, const std::string& arg,
            Formatter* f, std::ostream& ss)
{
  if (action == "arm_unlink_hold") {
    if (arg.empty()) {
      ss << "arm_unlink_hold needs a dentry name";
      return -EINVAL;
    }
    state.unlink_hold_name = arg;
    state.event("unlink_hold: armed for dentry " + arg);
  } else if (action == "release_unlink_hold") {
    state.unlink_hold_name.clear();
    if (state.unlink_held) {
      std::ostringstream es;
      es << "unlink_hold: released " << state.unlink_held->reqid;
      state.event(es.str());
      rank->queue_waiter(new C_MDS_RetryRequest(rank->mdcache,
                                                state.unlink_held));
      state.unlink_held.reset();
    }
  } else if (action == "arm_defer_caps") {
    try {
      state.defer_caps_client = std::stoll(arg);
    } catch (const std::exception&) {
      ss << "arm_defer_caps needs a numeric client id";
      return -EINVAL;
    }
    state.event("defer_caps: armed for client." + arg);
  } else if (action == "release_defer_caps") {
    state.defer_caps_client = -1;
    auto queued = std::move(state.deferred_caps);
    state.deferred_caps.clear();
    state.event("defer_caps: released " + std::to_string(queued.size()) +
                " queued messages");
    for (auto& m : queued)
      rank->locker->dispatch(m);
  } else if (action == "clear_events") {
    state.events.clear();
  } else if (action != "status") {
    ss << "unknown action " << action;
    return -EINVAL;
  }
  dump(f);
  return 0;
}

} // namespace unlink_hold_cap_deferral
