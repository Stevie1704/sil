#include "channel_arenas.hpp"

#include <sys/stat.h>

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "sil/arena.h"

#include "engine.hpp"    // RunError
#include "manifest.hpp"  // ManifestError

namespace {

using sil::ChannelArenas;
using sil::ManifestError;
using sil::RunError;

/** Fail the test process with a useful message when an invariant is false. */
void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

bool exists(const std::string &path) {
  struct stat st;
  return !path.empty() && stat(path.c_str(), &st) == 0;
}

std::vector<uint8_t> bytes(std::initializer_list<uint8_t> values) {
  return std::vector<uint8_t>(values);
}

/** The reason carried by `call`, or "" when it did not throw that type. */
template <typename Error, typename Body>
std::string reason_of(Body body) {
  try {
    body();
  } catch (const Error &e) {
    return e.what();
  } catch (...) {
    return "";
  }
  return "";
}

void test_a_written_slot_reads_back_at_its_seq() {
  ChannelArenas arenas("probe");
  arenas.map("payload", 8, 1);

  const uint64_t seq = arenas.write("payload", 0, bytes({1, 2, 3}));
  check(seq == 1, "the first write did not stamp seq 1");

  std::vector<uint8_t> out;
  arenas.read("payload", 0, seq, out);
  check(out == bytes({1, 2, 3}), "payload did not survive the arena");
  // A short payload leaves the rest of the slot untouched; `len` is what says
  // where it ends, so the reader must not hand back the whole capacity.
  check(out.size() == 3, "read returned the slot capacity, not the payload");
}

void test_seq_counts_writes_across_every_slot() {
  ChannelArenas arenas("probe");
  arenas.map("burst", 4, 3);

  check(arenas.write("burst", 0, bytes({1})) == 1, "slot 0 did not stamp 1");
  check(arenas.write("burst", 1, bytes({2})) == 2, "slot 1 did not stamp 2");
  check(arenas.write("burst", 2, bytes({3})) == 3, "slot 2 did not stamp 3");

  // Every slot keeps the seq it was written under, so a Burst's slots stay
  // individually addressable rather than sharing one Arena-wide stamp.
  std::vector<uint8_t> out;
  arenas.read("burst", 0, 1, out);
  check(out == bytes({1}), "slot 0 lost its payload to a later write");
  arenas.read("burst", 2, 3, out);
  check(out == bytes({3}), "slot 2 did not keep its own payload");
}

void test_a_stale_slot_is_refused_by_seq() {
  ChannelArenas arenas("probe");
  arenas.map("payload", 4, 1);
  arenas.write("payload", 0, bytes({9}));
  const uint64_t fresh = arenas.write("payload", 0, bytes({8}));

  std::vector<uint8_t> out;
  const std::string reason =
      reason_of<RunError>([&] { arenas.read("payload", 0, fresh - 1, out); });
  check(reason.find("stale arena slot 0") != std::string::npos,
        "a stale seq was accepted: " + reason);
  check(reason.find("participant 'probe'") != std::string::npos,
        "the diagnostic does not name the participant: " + reason);
}

void test_slots_beyond_the_arena_are_refused() {
  ChannelArenas arenas("probe");
  arenas.map("payload", 4, 2);

  check(reason_of<RunError>([&] {
          arenas.write("payload", 2, bytes({1}));
        }).find("arena slot out of range") != std::string::npos,
        "a write past the last slot was accepted");
  std::vector<uint8_t> out;
  check(reason_of<RunError>([&] {
          arenas.read("payload", 2, 1, out);
        }).find("arena slot out of range") != std::string::npos,
        "a read past the last slot was accepted");
}

void test_an_oversized_payload_is_refused() {
  ChannelArenas arenas("probe");
  arenas.map("payload", 2, 1);
  check(reason_of<RunError>([&] {
          arenas.write("payload", 0, bytes({1, 2, 3}));
        }).find("payload exceeds arena capacity") != std::string::npos,
        "a payload larger than the slot was accepted");
}

void test_an_inline_channel_declares_no_slots() {
  ChannelArenas arenas("probe");
  arenas.map("payload", 4, 2);

  check(arenas.contains("payload"), "an arena-backed channel reports no arena");
  check(!arenas.contains("inline"), "an inline channel reports an arena");
  // This is the production question the codec asks about every Channel in a
  // Step: a zero answer is what sends the Message to the inline path, so it
  // must be an answer rather than a diagnostic.
  check(arenas.slots("inline") == 0, "an inline channel declared slots");
  check(arenas.slots("payload") == 2, "an arena channel lost its slot count");
}

void test_the_unknown_channel_backstop_names_the_participant() {
  // Every caller establishes the Channel is arena-backed before asking, so
  // this path is a backstop against a future caller that forgets. It is worth
  // a case only because an anonymous container error here would reach the
  // operator with nothing to act on.
  ChannelArenas arenas("probe");
  check(reason_of<RunError>([&] {
          (void)arenas.layout("absent");
        }).find("participant 'probe' channel 'absent'") != std::string::npos,
        "the unknown-channel backstop gave an anonymous diagnostic");
}

void test_max_slots_reports_the_widest_channel() {
  ChannelArenas arenas("probe");
  check(arenas.max_slots() == 0, "an empty set of arenas declared slots");
  arenas.map("single", 4, 1);
  check(arenas.max_slots() == 1, "one single-slot channel reported otherwise");
  arenas.map("burst", 4, 4);
  check(arenas.max_slots() == 4, "the widest channel was not reported");
}

void test_release_unlinks_every_region() {
  ChannelArenas arenas("probe");
  arenas.map("left", 4, 1);
  arenas.map("right", 4, 1);
  const std::string left = arenas.layout("left").path;
  const std::string right = arenas.layout("right").path;
  check(exists(left) && exists(right), "an arena has no region file");

  arenas.release();
  check(!exists(left), "release left a file behind: " + left);
  check(!exists(right), "release left a file behind: " + right);
  check(!arenas.contains("left"), "release kept the channel's arena");
}

void test_an_overflowing_arena_is_a_manifest_error() {
  ChannelArenas arenas("probe");
  // The stride times the slot count must stay inside size_t: an arena the
  // environment could never supply is a Manifest error (exit 2), never a
  // silently truncated region.
  const std::string reason = reason_of<ManifestError>(
      [&] { arenas.map("huge", SIZE_MAX / 2, 8); });
  check(reason.find("overflows size_t") != std::string::npos,
        "an overflowing arena was accepted: " + reason);
  check(!arenas.contains("huge"), "a rejected arena was kept");
}

}  // namespace

int main() {
  try {
    test_a_written_slot_reads_back_at_its_seq();
    test_seq_counts_writes_across_every_slot();
    test_a_stale_slot_is_refused_by_seq();
    test_slots_beyond_the_arena_are_refused();
    test_an_oversized_payload_is_refused();
    test_an_inline_channel_declares_no_slots();
    test_the_unknown_channel_backstop_names_the_participant();
    test_max_slots_reports_the_widest_channel();
    test_release_unlinks_every_region();
    test_an_overflowing_arena_is_a_manifest_error();
  } catch (const std::exception &e) {
    std::cerr << "channel arenas test failed: " << e.what() << '\n';
    return 1;
  }
  return 0;
}
