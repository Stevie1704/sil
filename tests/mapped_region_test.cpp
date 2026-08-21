#include "mapped_region.hpp"

#include <sys/stat.h>

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>

namespace {

using sil::MappedRegion;

/** Fail the test process with a useful message when an invariant is false. */
void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

bool exists(const std::string &path) {
  struct stat st;
  return !path.empty() && stat(path.c_str(), &st) == 0;
}

/** Create a region and fail the test if the environment cannot supply one. */
MappedRegion make(size_t size) {
  std::string error;
  MappedRegion region = MappedRegion::create("sil_region_test_", size, error);
  check(bool(region), "could not create a region: " + error);
  check(error.empty(), "a successful create reported '" + error + "'");
  return region;
}

void test_region_is_mapped_and_named_while_it_lives() {
  MappedRegion region = make(64);
  check(exists(region.path()), "region file is missing while the region lives");
  check(region.path().find("sil_region_test_") != std::string::npos,
        "region file does not carry the requested prefix");
  // Writing through the mapping must reach the file the child would open.
  std::memset(region.base(), 0xAB, 64);
  check(static_cast<uint8_t *>(region.base())[63] == 0xAB,
        "mapping is not writable");
}

void test_destruction_unlinks_the_file() {
  std::string path;
  {
    MappedRegion region = make(64);
    path = region.path();
  }
  check(!exists(path), "region file survived the region: " + path);
}

void test_move_transfers_ownership_exactly_once() {
  MappedRegion source = make(64);
  const std::string path = source.path();

  MappedRegion moved = std::move(source);
  check(!source, "moved-from region still claims a mapping");
  check(source.path().empty(), "moved-from region still claims a path");
  check(exists(path), "moving a region unlinked its file");
  check(moved.path() == path, "moved region lost its path");

  {
    MappedRegion sink = make(64);
    const std::string replaced = sink.path();
    sink = std::move(moved);
    check(!exists(replaced),
          "move-assignment leaked the region it replaced: " + replaced);
    check(sink.path() == path, "move-assignment lost the source path");
  }
  check(!exists(path), "region file survived the last owner: " + path);
}

void test_failed_create_reports_and_leaves_nothing() {
  // An unusable temp directory makes mkstemp fail; an unmappable size makes
  // ftruncate or mmap fail. Either way the caller gets an empty region and a
  // reason, and picks its own error class.
  const char *saved = getenv("TMPDIR");
  const std::string previous = saved ? saved : "";
  setenv("TMPDIR", "/sil-no-such-directory", 1);
  std::string error;
  MappedRegion region = MappedRegion::create("sil_region_test_", 64, error);
  if (previous.empty()) unsetenv("TMPDIR");
  else setenv("TMPDIR", previous.c_str(), 1);
  check(!region, "create succeeded in a directory that does not exist");
  check(error == "mkstemp failed", "unexpected failure reason: " + error);
  check(region.path().empty(), "a failed create still claims a path");

  std::string huge_error;
  MappedRegion huge =
      MappedRegion::create("sil_region_test_", SIZE_MAX / 2, huge_error);
  check(!huge, "create succeeded for an unmappable size");
  check(!huge_error.empty(), "an unmappable size reported no reason");
  check(huge.path().empty(), "a failed create still claims a path");
}

}  // namespace

int main() {
  try {
    test_region_is_mapped_and_named_while_it_lives();
    test_destruction_unlinks_the_file();
    test_move_transfers_ownership_exactly_once();
    test_failed_create_reports_and_leaves_nothing();
  } catch (const std::exception &e) {
    std::cerr << "mapped region test failed: " << e.what() << '\n';
    return 1;
  }
  return 0;
}
