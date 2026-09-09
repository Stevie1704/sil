// Toy native participant: throws a C++ exception at a configured point, so the
// kernel's containment at the C ABI seam is exercised through a real shared
// library rather than a same-translation-unit stub.
//
// Config selects where the throw happens ("throw_in": "init" | "task") and what
// is thrown ("kind": "std" | "other" — a non-std type is what proves the
// catch-all), and whether the participant reports a functional failure through
// api->fail before throwing ("fail_first"), which containment must preserve.
// Uses one global instance: at most one participant per library per run.
#include <sil/participant.h>

#include <stdexcept>
#include <string>

#include <nlohmann/json.hpp>

namespace {

struct Thrower {
  const sil_api_v1 *api;
  std::string kind;
  bool fail_first = false;
};

Thrower g_thrower;

[[noreturn]] void throw_now(const Thrower &t) {
  if (t.fail_first) t.api->fail(t.api->ctx, "real reason");
  if (t.kind == "other") throw 42;
  throw std::runtime_error("boom");
}

void explode(void *user, uint64_t) {
  throw_now(*static_cast<Thrower *>(user));
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  nlohmann::json cfg = nlohmann::json::parse(config_json);
  g_thrower.api = api;
  g_thrower.kind = cfg.value("kind", "std");
  g_thrower.fail_first = cfg.value("fail_first", false);
  if (cfg.value("throw_in", "init") == "init") throw_now(g_thrower);
  return api->register_task(api->ctx, "explode",
                            cfg.value("period_ns", 10'000'000ULL), 0, 0,
                            explode, &g_thrower);
}
