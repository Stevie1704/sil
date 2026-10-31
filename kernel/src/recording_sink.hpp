#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>

#include "manifest.hpp"
#include "provenance.hpp"

namespace sil {

// Format-neutral write side of the recording seam. The engine treats a sink as
// a latency-0 subscriber fed in global publish order at the publish choke
// point; how those bytes land on disk is the format's concern. Concrete formats
// must preserve total publish order (or explicitly store it) so a replay
// reproduces the original tie-break for messages sharing a timestamp.
class RecordingSink {
 public:
  virtual ~RecordingSink() = default;

  // Records one published message. channel_index is the manifest (name-sorted)
  // channel index; publish_ns is its visible timestamp; seq is the channel's
  // per-message sequence number.
  virtual void record(uint32_t channel_index, uint64_t publish_ns, uint32_t seq,
                      const void *data, size_t len) = 0;

  // Finalizes the recording. Throws RunError if a byte did not reach the file.
  // Only the first call can throw; later calls do nothing. The destructor
  // finalizes an open sink too, and reports no error.
  virtual void close() = 0;

  // What the side-car says about the output file. Valid after close():
  // Calculated holds the digest of the bytes in the file, Omitted says External
  // mode, and Failed says that SiL has no digest it can claim. Before close()
  // this is Failed, as the file is not final.
  virtual const ProvenanceDigest &digest() const = 0;
};

// Selects a recording format from the output path's extension and opens a sink
// for it. Throws ManifestError (runner exit 2) for an unrecognized extension,
// before any participant is created. In External mode the sink does no digest
// work.
std::unique_ptr<RecordingSink> make_recording_sink(
    const std::filesystem::path &out, const Manifest &manifest,
    ProvenanceMode mode);

}  // namespace sil
