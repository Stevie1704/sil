#include "owned_directory.hpp"

#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <utility>
#include <vector>

namespace sil {

OwnedDirectory::~OwnedDirectory() { release(); }

OwnedDirectory::OwnedDirectory(OwnedDirectory &&other) noexcept
    : path_(std::move(other.path_)) {
  other.path_.clear();
}

OwnedDirectory &OwnedDirectory::operator=(OwnedDirectory &&other) noexcept {
  if (this != &other) {
    release();
    path_ = std::move(other.path_);
    other.path_.clear();
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
  std::error_code ignored;
  std::filesystem::remove_all(path_, ignored);
  path_.clear();
}

}  // namespace sil
