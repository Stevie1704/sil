#include "native_participant.hpp"

#include <dlfcn.h>

#include <algorithm>
#include <exception>
#include <string>
#include <utility>

namespace sil {

namespace {

NativeParticipant *self(void *ctx) {
  return static_cast<NativeParticipant *>(ctx);
}

}  // namespace

// The C ABI seam runs in both directions and neither may carry an exception.
// A kernel frame the participant calls into must not throw back across the
// ABI: the participant may be built against a different C++ runtime, so an
// unwind across the boundary is undefined. Participant code the kernel calls
// into must not unwind kernel frames either. Both directions therefore catch
// everything and record it through NativeParticipant::fail, which keeps the
// first failure and surfaces it after the activation (issue #65).
struct NativeApiBridge {
  // Every service call that can allocate funnels its body through here: an
  // escape becomes the run's first failure plus the participant's documented
  // error return.
  template <typename Body>
  static int with_containment(NativeParticipant *p, Body body) {
    try {
      return body();
    } catch (const std::exception &e) {
      p->fail("", e.what());
      return SIL_ERR;
    } catch (...) {
      p->fail("", "unknown exception");
      return SIL_ERR;
    }
  }

  // A registered Task is participant code the scheduler calls directly, so its
  // seam is here rather than in the loop: returning normally is what lets
  // Engine::run leave its in-task state and report the recorded failure.
  static void run_task(NativeParticipant *p, std::string_view site,
                       sil_task_fn fn, void *user, uint64_t now) {
    try {
      fn(user, now);
    } catch (const std::exception &e) {
      p->fail(site, e.what());
    } catch (...) {
      p->fail(site, "an unknown exception");
    }
  }

  static int register_task(void *ctx, const char *name, uint64_t period_ns,
                           uint64_t offset_ns, int32_t priority,
                           sil_task_fn fn, void *user) {
    NativeParticipant *p = self(ctx);
    if (!p->engine_.in_setup() || !name || !fn) return SIL_ERR;
    return with_containment(
        p, [p, name, period_ns, offset_ns, priority, fn, user] {
          // `name` belongs to the participant and dies with this call, so the
          // activation's diagnostic is built here, once, from a copy of it.
          p->engine_.register_task(
              p->name_, name, period_ns, offset_ns, priority,
              [p, fn, user, site = "task '" + std::string(name) + "' threw: "](
                  uint64_t now) { run_task(p, site, fn, user, now); });
          return SIL_OK;
        });
  }

  static int subscribe(void *ctx, const char *channel) {
    NativeParticipant *p = self(ctx);
    if (!p->engine_.in_setup() || !channel) return SIL_ERR;
    return with_containment(p, [p, channel] {
      const SubscriberRouteSpec *route = p->subscriber_route(channel);
      if (!route) {
        p->fail("", std::string("channel '") + channel +
                        "' is not a declared input");
        return SIL_ERR;
      }
      if (p->subscriber_routes_.count(channel)) {
        p->fail("", std::string("subscribed to Channel '") + channel +
                        "' more than once");
        return SIL_ERR;
      }
      p->subscriber_routes_[channel] = p->engine_.subscribe(p->name_, *route);
      return SIL_OK;
    });
  }

  static int publish(void *ctx, const char *channel, const void *data,
                     size_t len) {
    NativeParticipant *p = self(ctx);
    // Data plane is only valid inside a task activation (see participant.h).
    if (!p->engine_.in_task() || !channel || !data) return SIL_ERR;
    return with_containment(p, [p, channel, data, len] {
      if (p->fail_if_undeclared(p->publishes_, channel, "output"))
        return SIL_ERR;
      p->engine_.publish(p->name_, channel, data, len);
      return SIL_OK;
    });
  }

  static int take(void *ctx, const char *channel, const void **data,
                  size_t *len) {
    NativeParticipant *p = self(ctx);
    if (!p->engine_.in_task() || !channel || !data || !len) return SIL_ERR;
    return with_containment(p, [p, channel, data, len] {
      auto it = p->subscriber_routes_.find(channel);
      if (it == p->subscriber_routes_.end()) {
        p->fail("", std::string("take on unsubscribed channel '") + channel +
                        "'");
        return SIL_ERR;
      }
      PendingMessage msg;
      if (!p->engine_.take(*it->second, msg)) return 0;
      p->take_buffer_ = std::move(msg.bytes);
      *data = p->take_buffer_.data();
      *len = p->take_buffer_.size();
      return 1;
    });
  }

  // Reads one engine member: nothing can throw, and the signature carries no
  // error value to report one through.
  static uint64_t now_ns(void *ctx) { return self(ctx)->engine_.now_ns(); }

  static void fail(void *ctx, const char *reason) {
    self(ctx)->fail("", reason ? reason : "(no reason)");
  }
};

void NativeParticipant::fail(std::string_view context,
                             std::string_view detail) noexcept {
  try {
    std::string reason(context);
    reason.append(detail);
    engine_.fail(name_, reason);
  } catch (...) {
    // Only an allocation failure reaches here, and it must not take the place
    // of the failure it was reporting by crossing back into participant code.
  }
}

bool NativeParticipant::fail_if_undeclared(
    const std::vector<std::string> &declared, const char *channel,
    const char *direction) {
  if (std::find(declared.begin(), declared.end(), channel) != declared.end())
    return false;
  fail("", std::string("channel '") + channel + "' is not a declared " +
               direction);
  return true;
}

const SubscriberRouteSpec *NativeParticipant::subscriber_route(
    const char *channel) {
  auto it = std::find_if(
      subscribes_.begin(), subscribes_.end(), [channel](const auto &route) {
        return route.channel == channel;
      });
  return it == subscribes_.end() ? nullptr : &*it;
}

NativeParticipant::NativeParticipant(Engine &engine, const std::string &name,
                                     const NativeSpec &spec,
                                     const std::filesystem::path &base_dir)
    : engine_(engine),
      name_(name),
      subscribes_(spec.subscribes),
      publishes_(spec.publishes) {
  std::filesystem::path lib = spec.library;
  if (lib.is_relative()) lib = base_dir / lib;

  handle_ = dlopen(lib.c_str(), RTLD_NOW | RTLD_LOCAL);
  if (!handle_)
    throw ManifestError("manifest error: participant '" + name +
                        "': cannot load library: " + dlerror());
  auto init = reinterpret_cast<sil_participant_init_fn>(
      dlsym(handle_, "sil_participant_init"));
  if (!init)
    throw ManifestError("manifest error: participant '" + name +
                        "': library exports no sil_participant_init");

  sil_api_v1 api;
  api.abi_version = SIL_ABI_VERSION;
  api.ctx = this;
  api.register_task = &NativeApiBridge::register_task;
  api.subscribe = &NativeApiBridge::subscribe;
  api.publish = &NativeApiBridge::publish;
  api.take = &NativeApiBridge::take;
  api.now_ns = &NativeApiBridge::now_ns;
  api.fail = &NativeApiBridge::fail;

  api_ = api;
  // fail() keeps the first failure, so a contract violation raised during init
  // survives with its channel and direction; the return code and the
  // containment here are the fallbacks for a participant that stops without
  // saying why. Engine::setup turns any of them into a Manifest error before the
  // run begins. Recording the failure rather than throwing keeps this object
  // in the engine, so its destructor still closes the library.
  int rc = SIL_ERR;
  try {
    rc = init(&api_, name_.c_str(), spec.config_json.c_str());
  } catch (const std::exception &e) {
    fail("init threw: ", e.what());
    return;
  } catch (...) {
    fail("init threw: ", "an unknown exception");
    return;
  }
  if (rc != SIL_OK) fail("", "init failed");
}

NativeParticipant::~NativeParticipant() {
  if (handle_) dlclose(handle_);
}

}  // namespace sil
