#include "mapped_region.hpp"

#include <sys/mman.h>
#include <unistd.h>

#include <cstdlib>
#include <utility>
#include <vector>

namespace sil {

MappedRegion::~MappedRegion() { release(); }

MappedRegion::MappedRegion(MappedRegion &&other) noexcept
    : fd_(other.fd_), path_(std::move(other.path_)), base_(other.base_),
      size_(other.size_) {
  other.fd_ = -1;
  other.path_.clear();  // a moved-from std::string is only valid, not empty
  other.base_ = nullptr;
  other.size_ = 0;
}

MappedRegion &MappedRegion::operator=(MappedRegion &&other) noexcept {
  if (this != &other) {
    release();
    fd_ = std::exchange(other.fd_, -1);
    path_ = std::move(other.path_);
    other.path_.clear();
    base_ = std::exchange(other.base_, nullptr);
    size_ = std::exchange(other.size_, size_t(0));
  }
  return *this;
}

MappedRegion MappedRegion::create(const std::string &prefix, size_t size,
                                  std::string &error) {
  // A plain temp file (not POSIX shm) for portability: the child maps it by
  // path, so it must be nameable on the filesystem. Created in the OS temp dir.
  const char *tmp = getenv("TMPDIR");
  const std::string tpl =
      (tmp && *tmp ? std::string(tmp) : std::string("/tmp")) + "/" + prefix +
      "XXXXXX";
  std::vector<char> path(tpl.begin(), tpl.end());
  path.push_back('\0');

  MappedRegion region;
  region.fd_ = mkstemp(path.data());
  if (region.fd_ < 0) {
    error = "mkstemp failed";
    return region;
  }
  region.path_ = path.data();
  if (ftruncate(region.fd_, off_t(size)) != 0) {
    error = "ftruncate failed";
    region.release();
    return region;
  }
  void *base =
      mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, region.fd_, 0);
  if (base == MAP_FAILED) {
    error = "mmap failed";
    region.release();
    return region;
  }
  region.base_ = base;
  region.size_ = size;
  return region;
}

void MappedRegion::release() noexcept {
  if (base_) {
    munmap(base_, size_);
    base_ = nullptr;
  }
  size_ = 0;
  if (fd_ >= 0) {
    close(fd_);
    fd_ = -1;
  }
  if (!path_.empty()) {
    unlink(path_.c_str());
    path_.clear();
  }
}

}  // namespace sil
