#include "channel_arenas.hpp"

#include <cstring>
#include <limits>
#include <utility>

#include "sil/arena.h"

#include "copy_counters.hpp"
#include "engine.hpp"    // RunError
#include "manifest.hpp"  // ManifestError

namespace sil {

namespace {

// One slot is its header followed by the schema-sized payload; a multi-slot
// Arena repeats that stride, which keeps slot zero compatible with
// protocol-1 participants.
size_t stride_of(size_t capacity) { return sizeof(sil_arena) + capacity; }

sil_arena *header_at(void *base, size_t offset) {
  return reinterpret_cast<sil_arena *>(static_cast<uint8_t *>(base) + offset);
}

}  // namespace

std::string ChannelArenas::describe(const std::string &channel) const {
  return "participant '" + participant_ + "' channel '" + channel + "'";
}

void ChannelArenas::map(const std::string &channel, size_t capacity,
                        size_t slots) {
  constexpr size_t kMax = std::numeric_limits<size_t>::max();
  if (capacity > kMax - sizeof(sil_arena) || slots > kMax / stride_of(capacity))
    throw ManifestError(describe(channel) +
                        ": arena mapping size overflows size_t");

  // The failure taxonomy is this call's: an Arena the environment cannot
  // supply is a Manifest error (exit 2). Arenas already created, and the clock
  // region, are released by their own destructors as this throws.
  std::string error;
  Arena arena;
  arena.region =
      MappedRegion::create("sil_arena_", stride_of(capacity) * slots, error);
  if (!arena.region)
    throw ManifestError(describe(channel) + ": arena: " + error);
  arena.capacity = capacity;
  arena.slots = slots;
  for (size_t slot = 0; slot < slots; ++slot) {
    sil_arena *header =
        header_at(arena.region.base(), slot * stride_of(capacity));
    header->seq = 0;
    header->len = 0;
  }
  arenas_.emplace(channel, std::move(arena));
}

const ChannelArenas::Arena &ChannelArenas::at(const std::string &channel) const {
  auto it = arenas_.find(channel);
  if (it == arenas_.end())
    throw RunError(describe(channel) + ": channel has no arena");
  return it->second;
}

ChannelArenas::Arena &ChannelArenas::at(const std::string &channel) {
  return const_cast<Arena &>(std::as_const(*this).at(channel));
}

size_t ChannelArenas::slot_offset(const Arena &arena,
                                  const std::string &channel,
                                  size_t slot) const {
  if (slot >= arena.slots)
    throw RunError(describe(channel) + ": arena slot out of range");
  return slot * stride_of(arena.capacity);
}

size_t ChannelArenas::slots(const std::string &channel) const {
  auto it = arenas_.find(channel);
  return it == arenas_.end() ? 0 : it->second.slots;
}

ChannelArenas::Layout ChannelArenas::layout(const std::string &channel) const {
  const Arena &arena = at(channel);
  return Layout{arena.region.path(), arena.capacity, arena.slots};
}

size_t ChannelArenas::max_slots() const {
  size_t most = 0;
  for (const auto &[channel, arena] : arenas_)
    if (arena.slots > most) most = arena.slots;
  return most;
}

uint64_t ChannelArenas::write(const std::string &channel, size_t slot,
                              const std::vector<uint8_t> &bytes) {
  Arena &arena = at(channel);
  const size_t offset = slot_offset(arena, channel, slot);
  if (bytes.size() > arena.capacity)
    throw RunError(describe(channel) + ": payload exceeds arena capacity");

  counters::count(counters::Site::kArenaWrite, bytes.size());
  std::memcpy(
      static_cast<uint8_t *>(arena.region.base()) + offset + sizeof(sil_arena),
      bytes.data(), bytes.size());
  sil_arena *header = header_at(arena.region.base(), offset);
  header->len = bytes.size();
  // seq counts writes across the whole Arena, so a slot's header proves it was
  // written for the Message naming it rather than merely that it was written.
  header->seq = ++arena.seq;
  return arena.seq;
}

void ChannelArenas::read(const std::string &channel, size_t slot, uint64_t seq,
                         std::vector<uint8_t> &out) const {
  const Arena &arena = at(channel);
  const size_t offset = slot_offset(arena, channel, slot);
  const sil_arena *header = header_at(arena.region.base(), offset);
  if (header->seq != seq)
    throw RunError(describe(channel) + ": stale arena slot " +
                   std::to_string(slot) + " (expected seq " +
                   std::to_string(seq) + ", got " +
                   std::to_string(header->seq) + ")");
  if (header->len > arena.capacity)
    throw RunError(describe(channel) + ": arena len exceeds capacity");

  const auto *payload = static_cast<const uint8_t *>(arena.region.base()) +
                        offset + sizeof(sil_arena);
  counters::count(counters::Site::kArenaRead, header->len);
  out.assign(payload, payload + header->len);
}

}  // namespace sil
