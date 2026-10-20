#include "owned_directory.hpp"

#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <utility>
#include <vector>

namespace sil {

#ifdef SIL_RUN_DIAGNOSTICS
namespace {

std::filesystem::path g_retention_target;
bool g_retention_claimed = false;

// rename(2) when both paths are on one file system, else a copy. The source
// tree is removed in both cases.
void move_tree(const std::filesystem::path &from,
               const std::filesystem::path &to) noexcept {
  std::error_code failure;
  std::filesystem::rename(from, to, failure);
  if (!failure) return;
  std::filesystem::copy(from, to,
                        std::filesystem::copy_options::recursive |
                            std::filesystem::copy_options::copy_symlinks,
                        failure);
  if (failure) std::filesystem::remove_all(to, failure);
  std::filesystem::remove_all(from, failure);
}

}  // namespace

void OwnedDirectory::retain_next_unique_at(
    const std::filesystem::path &target) {
  g_retention_target = target;
  g_retention_claimed = false;
}
#endif

OwnedDirectory::~OwnedDirectory() { release(); }

OwnedDirectory::OwnedDirectory(OwnedDirectory &&other) noexcept
    : path_(std::move(other.path_)), retained_(other.retained_) {
  other.path_.clear();
  other.retained_ = false;
}

OwnedDirectory &OwnedDirectory::operator=(OwnedDirectory &&other) noexcept {
  if (this != &other) {
    release();
    path_ = std::move(other.path_);
    retained_ = other.retained_;
    other.path_.clear();
    other.retained_ = false;
  }
  return *this;
}

OwnedDirectory OwnedDirectory::create_unique(
    const std::filesystem::path &parent, const std::string &prefix,
    std::string &error) {
  std::string pattern = (parent / (prefix + "XXXXXX")).string();
  std::vector<char> writable(pattern.begin(), pattern.end());
  writable.push_back('\0');

  OwnedDirectory directory;
  if (!mkdtemp(writable.data())) {
    error = std::strerror(errno);
    return directory;
  }
  directory.path_ = writable.data();
#ifdef SIL_RUN_DIAGNOSTICS
  if (!g_retention_target.empty() && !g_retention_claimed) {
    directory.retained_ = true;
    g_retention_claimed = true;
  }
#endif
  return directory;
}

OwnedDirectory OwnedDirectory::create_child(
    const std::filesystem::path &parent, const std::string &name,
    std::string &error) {
  OwnedDirectory directory;
  const std::filesystem::path path = parent / name;
  std::error_code failure;
  if (!std::filesystem::create_directory(path, failure)) {
    error = failure ? failure.message() : "directory already exists";
    return directory;
  }
  directory.path_ = path;
  return directory;
}

void OwnedDirectory::release() noexcept {
  if (path_.empty()) return;
#ifdef SIL_RUN_DIAGNOSTICS
  if (retained_) {
    move_tree(path_, g_retention_target);
    retained_ = false;
    path_.clear();
    return;
  }
  // A child of the retained tree moves with it.
  if (g_retention_claimed && !g_retention_target.empty()) {
    path_.clear();
    return;
  }
#endif
  std::error_code ignored;
  std::filesystem::remove_all(path_, ignored);
  path_.clear();
}

}  // namespace sil
