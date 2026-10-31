// The SHA-256 backend: OpenSSL EVP. libcrypto selects its SHA extension or
// ARMv8 code path at run time from the CPU features, so no build needs a
// host-specific compiler flag and no build requires those instructions.
#include "sha256.hpp"

#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <array>
#include <stdexcept>

namespace sil {

namespace {

[[noreturn]] void backend_failure(const char *operation) {
  throw std::runtime_error(std::string("SHA-256 backend: ") + operation +
                           " failed");
}

}  // namespace

struct Sha256::Context {
  EVP_MD_CTX *digest = nullptr;
  ~Context() { EVP_MD_CTX_free(digest); }
};

Sha256::Sha256() : context_(std::make_unique<Context>()) {
  // A host openssl.cnf or OPENSSL_CONF can load providers. A digest needs
  // neither, and a Run must not read host configuration to calculate one.
  if (OPENSSL_init_crypto(OPENSSL_INIT_NO_LOAD_CONFIG, nullptr) != 1)
    backend_failure("initialization");
  context_->digest = EVP_MD_CTX_new();
  if (context_->digest == nullptr) backend_failure("context allocation");
  if (EVP_DigestInit_ex(context_->digest, EVP_sha256(), nullptr) != 1)
    backend_failure("digest start");
}

Sha256::~Sha256() = default;

void Sha256::update(const void *data, size_t len) {
  if (finished_) throw std::logic_error("SHA-256 update after the digest ended");
  if (len == 0) return;
  if (EVP_DigestUpdate(context_->digest, data, len) != 1)
    backend_failure("digest update");
}

std::string Sha256::hex_digest() {
  if (finished_) throw std::logic_error("SHA-256 digest already ended");
  std::array<unsigned char, 32> bytes{};
  unsigned int written = 0;
  if (EVP_DigestFinal_ex(context_->digest, bytes.data(), &written) != 1 ||
      written != bytes.size())
    backend_failure("digest end");
  finished_ = true;

  static constexpr char hex[] = "0123456789abcdef";
  std::string out;
  out.reserve(bytes.size() * 2);
  for (const unsigned char byte : bytes) {
    out.push_back(hex[byte >> 4]);
    out.push_back(hex[byte & 0xf]);
  }
  return out;
}

std::string sha256_hex(const std::string &bytes) {
  Sha256 hash;
  hash.update(bytes.data(), bytes.size());
  return hash.hex_digest();
}

}  // namespace sil
