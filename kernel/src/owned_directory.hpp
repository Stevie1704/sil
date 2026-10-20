#pragma once

#include <filesystem>
#include <string>

namespace sil {

// A directory tree with one kernel owner. Destruction recursively removes the
// tree, so setup and Run failures need no separate rollback path.
class OwnedDirectory {
 public:
  OwnedDirectory() = default;
  ~OwnedDirectory();

  OwnedDirectory(OwnedDirectory &&other) noexcept;
  OwnedDirectory &operator=(OwnedDirectory &&other) noexcept;
  OwnedDirectory(const OwnedDirectory &) = delete;
  OwnedDirectory &operator=(const OwnedDirectory &) = delete;

  // Creates <parent>/<prefix>XXXXXX. Failure returns an empty owner and writes
  // the OS diagnostic to error.
  static OwnedDirectory create_unique(const std::filesystem::path &parent,
                                      const std::string &prefix,
                                      std::string &error);

  // Creates one exact child directory. The caller supplies a collision-free,
  // filesystem-safe name.
  static OwnedDirectory create_child(const std::filesystem::path &parent,
                                     const std::string &name,
                                     std::string &error);

#ifdef SIL_RUN_DIAGNOSTICS
  // Diagnostics runner only (#315): the next directory made by create_unique
  // is moved to `target` when it is released, instead of removed, so a
  // profiler can capture modules loaded from it. Its children are then not
  // removed one by one; they move with it. If the move fails, the tree is
  // removed as without retention.
  static void retain_next_unique_at(const std::filesystem::path &target);
#endif

  explicit operator bool() const { return !path_.empty(); }
  const std::filesystem::path &path() const { return path_; }

 private:
  void release() noexcept;

  std::filesystem::path path_;
  bool retained_ = false;  // moves to the retention target on release
};

}  // namespace sil
