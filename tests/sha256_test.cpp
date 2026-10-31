// The SHA-256 interface (#325): published FIPS 180-4 and NIST CAVP vectors, and
// the one-digest rule of a hash object. Agreement with an independent
// implementation over many lengths and chunkings is in tests/test_sha256.py.
#include "sha256.hpp"

#include <iostream>
#include <stdexcept>
#include <string>

namespace {

void check_digest(const std::string &input, const std::string &expected) {
  const std::string actual = sil::sha256_hex(input);
  if (actual != expected)
    throw std::runtime_error("length " + std::to_string(input.size()) +
                             ": got " + actual + ", want " + expected);
}

void known_answers() {
  check_digest("",
               "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  check_digest("abc",
               "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  // 56 bytes: the padding does not fit, so a second block is necessary.
  check_digest("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
               "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
  check_digest(std::string(1000000, 'a'),
               "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
}

void empty_updates_do_not_change_the_digest() {
  sil::Sha256 hash;
  hash.update(nullptr, 0);
  hash.update("abc", 3);
  hash.update("abc", 0);
  const std::string actual = hash.hex_digest();
  if (actual !=
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    throw std::runtime_error("empty update changed the digest: " + actual);
}

template <typename Use>
void must_reject_finished(const char *name, Use use) {
  sil::Sha256 hash;
  hash.update("abc", 3);
  (void)hash.hex_digest();
  try {
    use(hash);
  } catch (const std::logic_error &) {
    return;
  }
  throw std::runtime_error(std::string(name) + " accepted a finished hash");
}

}  // namespace

int main() {
  try {
    known_answers();
    empty_updates_do_not_change_the_digest();
    must_reject_finished("update", [](sil::Sha256 &h) { h.update("x", 1); });
    must_reject_finished("hex_digest",
                         [](sil::Sha256 &h) { (void)h.hex_digest(); });
  } catch (const std::exception &e) {
    std::cerr << "sha256_test: " << e.what() << "\n";
    return 1;
  }
  return 0;
}
