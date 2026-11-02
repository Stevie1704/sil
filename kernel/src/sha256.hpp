// SHA-256 (FIPS 180-4) for Manifest bytes, artifact files, and Recordings.
// This is the one hashing interface of the kernel. The backend is in
// sha256.cpp; see docs/architecture.md for its choice and its consequences.
#pragma once

#include <cstddef>
#include <memory>
#include <string>

namespace sil {

// One streaming digest. Any sequence of update() calls over the same bytes
// gives the same digest. A backend failure throws std::runtime_error, so a
// caller cannot record a digest that was not calculated.
class Sha256 {
 public:
  Sha256();
  ~Sha256();
  Sha256(const Sha256 &) = delete;
  Sha256 &operator=(const Sha256 &) = delete;

  void update(const void *data, size_t len);

  // Ends the digest. A second call, or update() after this call, throws
  // std::logic_error.
  std::string hex_digest();

 private:
  struct Context;
  std::unique_ptr<Context> context_;
  bool finished_ = false;
};

std::string sha256_hex(const std::string &bytes);

}  // namespace sil
