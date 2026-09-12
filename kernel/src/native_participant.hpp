#pragma once

#include <filesystem>
#include <map>
#include <string>
#include <string_view>
#include <vector>

#include <sil/participant.h>

#include "engine.hpp"

namespace sil {

// Loads a native participant library and bridges the C ABI to the engine.
class NativeParticipant {
 public:
  NativeParticipant(Engine &engine, const std::string &name,
                    const NativeSpec &spec,
                    const std::filesystem::path &base_dir);
  ~NativeParticipant();

  NativeParticipant(const NativeParticipant &) = delete;
  NativeParticipant &operator=(const NativeParticipant &) = delete;

 private:
  Engine &engine_;
  std::string name_;
  // The manifest's Channel contract for this participant. Subscribe and
  // publish calls are checked against it, so a participant defect cannot
  // change the run's topology (issue #49).
  std::vector<SubscriberRouteSpec> subscribes_;
  std::vector<std::string> publishes_;
  // Participants keep the api pointer beyond init; table must outlive them.
  sil_api_v1 api_{};
  void *handle_ = nullptr;
  std::map<std::string, SubscriberRoute *> subscriber_routes_;
  std::vector<uint8_t> take_buffer_;

  // Records this participant's failure without ever throwing. The two reason
  // pieces are joined here, inside the guard, so building the diagnostic
  // cannot itself escape across the seam in place of the failure it reports.
  // `context` names the seam the failure escaped from and may be empty.
  void fail(std::string_view context, std::string_view detail) noexcept;

  // Fails the run when the manifest does not authorize this call, naming the
  // participant, the channel, and the declared direction it violates.
  // Returns true when it has failed the run.
  bool fail_if_undeclared(const std::vector<std::string> &declared,
                          const char *channel, const char *direction);
  const SubscriberRouteSpec *subscriber_route(const char *channel);

  friend struct NativeApiBridge;
};

}  // namespace sil
