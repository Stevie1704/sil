#pragma once

#include <filesystem>
#include <map>
#include <string>
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
  // Participants keep the api pointer beyond init; table must outlive them.
  sil_api_v1 api_{};
  void *handle_ = nullptr;
  std::map<std::string, SubQueue *> subscriptions_;
  std::vector<uint8_t> take_buffer_;

  friend struct NativeApiBridge;
};

}  // namespace sil
