// Hashes a file through the kernel's hashing interface, for two callers:
// tests/test_sha256.py compares the digest with an independent implementation,
// and tools/sha256_backend_measure.py times the backends (#325).
//
//   sil_sha256_digest FILE CHUNK[,CHUNK...]  [--backend kernel|minimal]
//
// The file is read into memory first, so a chunk list drives update() with
// exact call sizes and the timed interval holds hashing only. The list repeats
// until the input ends. The digest goes to stdout; with --backend, the hash
// CPU and wall time follow it on the same line.
#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <iostream>
#include <iterator>
#include <sstream>
#include <string>
#include <vector>

#include "minimal_sha256_reference.hpp"
#include "sha256.hpp"

namespace {

template <typename Hash>
std::string digest(const std::vector<char> &bytes,
                   const std::vector<size_t> &chunks) {
  Hash hash;
  size_t offset = 0;
  for (size_t i = 0; offset < bytes.size(); i = (i + 1) % chunks.size()) {
    const size_t take = std::min(chunks[i], bytes.size() - offset);
    hash.update(bytes.data() + offset, take);
    offset += take;
  }
  return hash.hex_digest();
}

std::vector<size_t> parse_chunks(const std::string &text) {
  std::vector<size_t> chunks;
  std::stringstream stream(text);
  std::string item;
  while (std::getline(stream, item, ','))
    chunks.push_back(std::stoul(item));
  if (chunks.empty() || std::count(chunks.begin(), chunks.end(), size_t(0)))
    throw std::invalid_argument("chunk sizes must be positive");
  return chunks;
}

}  // namespace

int main(int argc, char **argv) {
  if (argc != 3 && argc != 5) {
    std::cerr << "usage: sil_sha256_digest FILE CHUNK[,CHUNK...] "
                 "[--backend kernel|minimal]\n";
    return 2;
  }
  try {
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) throw std::runtime_error(std::string("cannot open ") + argv[1]);
    const std::vector<char> bytes((std::istreambuf_iterator<char>(input)),
                                  std::istreambuf_iterator<char>());
    const std::vector<size_t> chunks = parse_chunks(argv[2]);
    const std::string backend = argc == 5 ? argv[4] : "kernel";

    const std::clock_t cpu_start = std::clock();
    const auto wall_start = std::chrono::steady_clock::now();
    const std::string hex =
        backend == "minimal" ? digest<sil_reference::MinimalSha256>(bytes, chunks)
                             : digest<sil::Sha256>(bytes, chunks);
    const auto wall = std::chrono::steady_clock::now() - wall_start;
    const std::clock_t cpu = std::clock() - cpu_start;

    std::cout << hex;
    if (argc == 5)
      std::cout << ' ' << (double(cpu) / CLOCKS_PER_SEC) * 1e9 << ' '
                << std::chrono::nanoseconds(wall).count();
    std::cout << "\n";
  } catch (const std::exception &e) {
    std::cerr << "sil_sha256_digest: " << e.what() << "\n";
    return 1;
  }
  return 0;
}
