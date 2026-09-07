#include "native_participant.hpp"

#include <dlfcn.h>

#include <algorithm>

namespace sil {

namespace {

NativeParticipant *self(void *ctx) {
  return static_cast<NativeParticipant *>(ctx);
}

}  // namespace

// The C ABI callbacks must not let exceptions cross participant frames:
// errors are routed through Engine::fail and surface after the callback.
struct NativeApiBridge {
  static int register_task(void *ctx, const char *name, uint64_t period_ns,
                           uint64_t offset_ns, int32_t priority,
                           sil_task_fn fn, void *user) {
    NativeParticipant *p = self(ctx);
    if (!p->engine_.in_setup() || !name || !fn) return SIL_ERR;
    try {
      p->engine_.register_task(
          p->name_, name, period_ns, offset_ns, priority,
          [fn, user](uint64_t now) { fn(user, now); });
    } catch (const std::exception &e) {
      p->engine_.fail(p->name_, e.what());
      return SIL_ERR;
    }
    return SIL_OK;
  }

  static int subscribe(void *ctx, const char *channel) {
    NativeParticipant *p = self(ctx);
    if (!p->engine_.in_setup() || !channel) return SIL_ERR;
    if (p->fail_if_undeclared(p->subscribes_, channel, "input"))
      return SIL_ERR;
    try {
      p->subscriptions_[channel] = p->engine_.subscribe(p->name_, channel);
    } catch (const std::exception &e) {
      p->engine_.fail(p->name_, e.what());
      return SIL_ERR;
    }
    return SIL_OK;
  }

  static int publish(void *ctx, const char *channel, const void *data,
                     size_t len) {
    NativeParticipant *p = self(ctx);
    // Data plane is only valid inside a task callback (see participant.h).
    if (!p->engine_.in_task() || !channel || !data) return SIL_ERR;
    if (p->fail_if_undeclared(p->publishes_, channel, "output"))
      return SIL_ERR;
    try {
      p->engine_.publish(p->name_, channel, data, len);
    } catch (const std::exception &e) {
      p->engine_.fail(p->name_, e.what());
      return SIL_ERR;
    }
    return SIL_OK;
  }

  static int take(void *ctx, const char *channel, const void **data,
                  size_t *len) {
    NativeParticipant *p = self(ctx);
    if (!p->engine_.in_task() || !channel || !data || !len) return SIL_ERR;
    auto it = p->subscriptions_.find(channel);
    if (it == p->subscriptions_.end()) {
      p->engine_.fail(p->name_, std::string("take on unsubscribed channel '") +
                                    channel + "'");
      return SIL_ERR;
    }
    PendingMessage msg;
    if (!p->engine_.take(*it->second, msg)) return 0;
    p->take_buffer_ = std::move(msg.bytes);
    *data = p->take_buffer_.data();
    *len = p->take_buffer_.size();
    return 1;
  }

  static uint64_t now_ns(void *ctx) { return self(ctx)->engine_.now_ns(); }

  static void fail(void *ctx, const char *reason) {
    NativeParticipant *p = self(ctx);
    p->engine_.fail(p->name_, reason ? reason : "(no reason)");
  }
};

bool NativeParticipant::fail_if_undeclared(
    const std::vector<std::string> &declared, const char *channel,
    const char *direction) {
  if (std::find(declared.begin(), declared.end(), channel) != declared.end())
    return false;
  engine_.fail(name_, std::string("channel '") + channel +
                          "' is not a declared " + direction);
  return true;
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
  // survives with its channel and direction; this is the fallback for a
  // participant that refuses to start without saying why. Engine::setup turns
  // either into a config error before the run begins.
  if (init(&api_, name_.c_str(), spec.config_json.c_str()) != SIL_OK)
    engine_.fail(name_, "init failed");
}

NativeParticipant::~NativeParticipant() {
  if (handle_) dlclose(handle_);
}

}  // namespace sil
