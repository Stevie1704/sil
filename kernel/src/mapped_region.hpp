#pragma once

#include <cstddef>
#include <string>

namespace sil {

// A memory-mapped temp file that owns its whole lifetime: the mapping, the file
// descriptor, and the directory entry. Destruction releases all three, so a
// scope that throws before the region reaches its owner needs no hand-written
// rollback — in particular a constructor that maps several regions and then
// fails releases the earlier ones through their own destructors.
//
// The file stays on the filesystem for as long as the region lives, because a
// forked child maps the same file by path at load. The unlink happens at
// destruction, never earlier.
//
// The region does not choose an error taxonomy: `create` reports failure
// through `error` and returns an empty region, and the call site raises the
// exception class that call site already uses.
//
// Move-only: exactly one owner unlinks the file.
class MappedRegion {
 public:
  MappedRegion() = default;
  ~MappedRegion();

  MappedRegion(MappedRegion &&other) noexcept;
  MappedRegion &operator=(MappedRegion &&other) noexcept;
  MappedRegion(const MappedRegion &) = delete;
  MappedRegion &operator=(const MappedRegion &) = delete;

  // Creates "<TMPDIR or /tmp>/<prefix>XXXXXX", sizes it to `size` bytes, and
  // maps it MAP_SHARED read/write. On failure returns an empty region, leaves
  // no file behind, and sets `error` to the failing step ("mkstemp failed",
  // "ftruncate failed", "mmap failed").
  static MappedRegion create(const std::string &prefix, size_t size,
                             std::string &error);

  explicit operator bool() const { return base_ != nullptr; }
  // Start of the mapping; null for an empty region.
  void *base() const { return base_; }
  // The file name a child maps to reach the same region; empty when unmapped.
  const std::string &path() const { return path_; }

 private:
  void release() noexcept;

  int fd_ = -1;
  std::string path_;
  void *base_ = nullptr;
  size_t size_ = 0;
};

}  // namespace sil
