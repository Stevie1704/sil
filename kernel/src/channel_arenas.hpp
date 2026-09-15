#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "mapped_region.hpp"

namespace sil {

// The Arenas of one Process participant, one per arena-backed Channel.
//
// Each Arena is a Mapped region created before fork, so the child reaches the
// same region by path at load. Its layout is `slots` repetitions of one
// `sil_arena` header followed by a `capacity`-byte payload
// (include/sil/arena.h). The Step protocol is fully sequential, so no slot
// needs locking: `seq` is what proves a slot was written for the Message
// naming it.
//
// This type knows the Arena layout and nothing else. It does not read the
// Manifest, does not know which Step protocol was negotiated, and never
// touches the child. ProcessParticipant translates a Manifest declaration into
// `map(channel, capacity, slots)`, and the Step codec clamps a slot index to
// the negotiated protocol before asking for it. That is what lets the layout
// be tested without spawning a participant.
//
// The failure taxonomy is deliberately split, because the two failures are
// repaired differently: an Arena the environment cannot supply is a
// ManifestError (exit 2, nothing ran), while a slot, capacity, or freshness
// violation during a Step is a RunError (exit 1).
class ChannelArenas {
 public:
  // What a child needs to reach one Arena, handed to it in the init line.
  struct Layout {
    std::string path;
    size_t capacity;
    size_t slots;
  };

  explicit ChannelArenas(std::string participant)
      : participant_(std::move(participant)) {}

  // Creates one Channel's Arena, sized `slots * (header + capacity)`. Called
  // exactly once per arena-backed Channel: a Manifest rejects a Channel
  // declared twice in one direction, and one declared in both directions over
  // shm transport, so the caller's subscribe and publish passes cannot collide.
  // Throws ManifestError when the size overflows or the region cannot be made.
  void map(const std::string &channel, size_t capacity, size_t slots);

  bool contains(const std::string &channel) const {
    return arenas_.count(channel) != 0;
  }
  // Declared slot count for `channel`, or 0 when it is not arena-backed. This
  // one answers for any Channel, because the codec asks it about every Channel
  // in a Step to decide which ones can use an Arena at all.
  size_t slots(const std::string &channel) const;
  // The rest of what the child is told about an arena-backed Channel. Unlike
  // `slots`, this is only ever asked for a Channel already known to have an
  // Arena, so an unknown one is a RunError rather than an empty answer.
  Layout layout(const std::string &channel) const;
  // Greatest declared slot count across every Channel; 0 when none has an
  // Arena. The caller reads this to decide which Step protocol level to offer.
  size_t max_slots() const;

  // Writes `bytes` into a slot and answers the Arena's post-write seq.
  // Throws RunError for an out-of-range slot or an oversized payload.
  uint64_t write(const std::string &channel, size_t slot,
                 const std::vector<uint8_t> &bytes);

  // Reads a slot's payload into `out`, refusing a slot whose header does not
  // carry `seq`. Throws RunError for an out-of-range slot, a stale slot, or a
  // length the Arena cannot hold.
  void read(const std::string &channel, size_t slot, uint64_t seq,
            std::vector<uint8_t> &out) const;

  // Releases every Arena and unlinks its file. Called at Run end rather than at
  // destruction, so a finished Run leaves nothing in the temp directory even
  // while its participant is still alive.
  void release() { arenas_.clear(); }

  // "participant 'p' channel 'c'" — the prefix every Arena and Step-codec
  // diagnostic about this participant opens with. Public so the codec's own
  // diagnostics read identically to these without restating the format.
  std::string describe(const std::string &channel) const;

 private:
  struct Arena {
    MappedRegion region;  // slots * (sizeof(sil_arena) + capacity)
    size_t capacity = 0;  // schema byte_size per slot
    size_t slots = 1;
    uint64_t seq = 0;     // last seq stamped across all slots
  };

  // The Arena for `channel`. Every caller has already established the Channel
  // is arena-backed — the codec through `slots`, the init line through the
  // Channel's declared Transport — so the RunError is a backstop against a
  // future caller that forgets, never a fault a child can provoke.
  const Arena &at(const std::string &channel) const;
  Arena &at(const std::string &channel);
  // Byte offset of `slot` within the Arena, after bounds-checking it.
  size_t slot_offset(const Arena &arena, const std::string &channel,
                     size_t slot) const;

  std::string participant_;
  std::map<std::string, Arena> arenas_;  // by channel name
};

}  // namespace sil
