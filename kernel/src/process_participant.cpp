#include "process_participant.hpp"

#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <limits>
#include <map>
#include <string_view>
#include <utility>

#include <nlohmann/json.hpp>

#include "clock_shim.hpp"
#include "copy_counters.hpp"
#include "owned_directory.hpp"

namespace sil {

using nlohmann::json;

namespace {

const char kB64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string b64_encode(const std::vector<uint8_t> &in) {
  std::string out;
  out.reserve((in.size() + 2) / 3 * 4);
  size_t i = 0;
  for (; i + 3 <= in.size(); i += 3) {
    uint32_t v = in[i] << 16 | in[i + 1] << 8 | in[i + 2];
    out.push_back(kB64[v >> 18]);
    out.push_back(kB64[(v >> 12) & 63]);
    out.push_back(kB64[(v >> 6) & 63]);
    out.push_back(kB64[v & 63]);
  }
  if (i + 1 == in.size()) {
    uint32_t v = in[i] << 16;
    out.push_back(kB64[v >> 18]);
    out.push_back(kB64[(v >> 12) & 63]);
    out += "==";
  } else if (i + 2 == in.size()) {
    uint32_t v = in[i] << 16 | in[i + 1] << 8;
    out.push_back(kB64[v >> 18]);
    out.push_back(kB64[(v >> 12) & 63]);
    out.push_back(kB64[(v >> 6) & 63]);
    out.push_back('=');
  }
  return out;
}

int8_t b64_value(uint8_t ch) {
  static int8_t table[256];
  static bool init = [] {
    for (int i = 0; i < 256; i++) table[i] = -1;
    for (int i = 0; i < 64; i++) table[uint8_t(kB64[i])] = int8_t(i);
    return true;
  }();
  (void)init;
  return table[ch];
}

// What b64_decode would produce, without decoding and without allocating.
// The budget check runs before the payloads exist, so walking every character
// a second time would put the whole inline decode cost on the line twice; the
// count is arithmetic instead. b64_decode emits one byte per 8 accumulated
// bits and stops at the first '=', so n source characters yield floor(3n/4).
// Splitting n into quotient and remainder keeps the product inside size_t.
// Characters outside the alphabet are not rejected here: b64_decode raises
// that, and a Step that trips this budget fails the Run either way.
size_t b64_decoded_size(const std::string &in) {
  const size_t chars = std::min(in.find('='), in.size());
  return chars / 4 * 3 + chars % 4 * 3 / 4;
}

std::vector<uint8_t> b64_decode(const std::string &in) {
  std::vector<uint8_t> out;
  uint32_t acc = 0;
  int bits = 0;
  for (char ch : in) {
    if (ch == '=') break;
    int8_t v = b64_value(uint8_t(ch));
    if (v < 0) throw RunError("invalid base64 in participant message");
    acc = acc << 6 | uint32_t(v);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back(uint8_t(acc >> bits));
    }
  }
  return out;
}

std::vector<std::string> resolve_command(
    const std::vector<std::string> &command,
    const std::filesystem::path &invocation_directory) {
  std::vector<std::string> resolved = command;
  for (size_t index = 0; index < resolved.size(); ++index) {
    if (resolved[index].empty()) continue;
    std::filesystem::path argument(resolved[index]);
    if (argument.is_absolute()) continue;

    const std::filesystem::path from_invocation =
        invocation_directory / argument;
    std::error_code error;
    const bool names_existing_path =
        std::filesystem::exists(from_invocation, error);
    const bool should_resolve = index == 0
                                    ? argument.has_parent_path()
                                    : !error && names_existing_path;
    if (should_resolve)
      resolved[index] = from_invocation.lexically_normal().string();
  }
  return resolved;
}

std::string participant_directory_name(const std::string &name) {
  static constexpr char hex[] = "0123456789abcdef";
  std::string encoded = "participant-";
  encoded.reserve(encoded.size() + name.size() * 2);
  for (const unsigned char ch : name) {
    encoded.push_back(hex[ch >> 4]);
    encoded.push_back(hex[ch & 0x0f]);
  }
  return encoded;
}

}  // namespace

// --- step codec (issue #41) -------------------------------------------------
//
// The seam is private to this implementation and scoped to a whole Step, so no
// `begin_step`-style reset appears in its interface. Three normative contracts
// hold here, and docs/step-protocol.md is their written form:
//
//   1. Inputs arrive already merged in global publish order; the codec never
//      reorders them.
//   2. A Channel's declared Arena slots are filled in that order, and every
//      Message past them falls back inline. The receiver never infers
//      Transport from the Channel declaration.
//   3. Arena create failures are Manifest errors (exit 2); stale-seq and
//      capacity violations at run time are Run failures (exit 1). Both are
//      raised by ChannelArenas, which owns the Arena layout.
//
// The codec depends on the Arenas and the negotiated protocol level, never on
// the participant that owns it: the Step line is a pure function of those two
// plus the Messages, which is what keeps this seam honest.
class ProcessParticipant::StepCodec {
 private:
  // The inline representation: the payload base64-encoded into the Step line.
  // The default Transport, and the fallback for every Message a Channel's
  // Arena slots cannot hold.
  class InlineAdapter {
   public:
    static void encode(json &item, const std::vector<uint8_t> &bytes) {
      counters::count(counters::Site::kInlineEncode, bytes.size());
      item["data"] = b64_encode(bytes);
    }

    static size_t decoded_size(const json &item) {
      return b64_decoded_size(
          item.at("data").get_ref<const std::string &>());
    }

    static std::vector<uint8_t> decode(const json &item) {
      std::vector<uint8_t> bytes =
          b64_decode(item.at("data").get_ref<const std::string &>());
      counters::count(counters::Site::kInlineDecode, bytes.size());
      return bytes;
    }
  };

  // The Arena representation: the payload in a slot, named on the Step line by
  // its seq and — once indexed slots are negotiated — its index.
  class ArenaAdapter {
   public:
    ArenaAdapter(ChannelArenas &arenas, int protocol)
        : arenas_(arenas), protocol_(protocol) {}

    // Arena slots this Channel may use at the negotiated level: 0 when it has
    // no Arena, 1 before indexed slots were negotiated.
    size_t available_slots(const std::string &channel) const {
      const size_t declared = arenas_.slots(channel);
      if (declared == 0) return 0;
      return protocol_ >= kIndexedSlotsProtocol ? declared : 1;
    }

    void encode(json &item, const std::string &channel, size_t slot,
                const std::vector<uint8_t> &bytes) {
      if (protocol_ >= kIndexedSlotsProtocol) item["shm_slot"] = slot;
      item["shm_seq"] = arenas_.write(channel, slot, bytes);
    }

    std::vector<uint8_t> decode(const json &item, const std::string &channel) {
      const size_t slot = item.value("shm_slot", size_t{0});
      const size_t available = available_slots(channel);
      if (slot >= available)
        throw RunError(arenas_.describe(channel) + ": arena slot " +
                       std::to_string(slot) +
                       " exceeds negotiated slot count " +
                       std::to_string(available));
      std::vector<uint8_t> bytes;
      arenas_.read(channel, slot, item.at("shm_seq").get<uint64_t>(), bytes);
      return bytes;
    }

   private:
    ChannelArenas &arenas_;
    int protocol_;
  };

 public:
  StepCodec(ChannelArenas &arenas, int protocol) : arena_(arenas, protocol) {}

  // The field on the line is authoritative, so the inline budget measures
  // exactly the Messages the decode pass will inline. Both passes ask here.
  static bool is_inline(const json &item) { return !item.contains("shm_seq"); }

  json encode_inputs(const std::vector<StepInput> &messages) {
    json in = json::array();
    // These indices belong to one codec call, so slot reuse is inherently
    // scoped to one Step and cannot leak into the next one.
    std::map<std::string, size_t> next_slot_by_channel;
    for (const StepInput &message : messages) {
      json item = {{"ch", message.channel}, {"t", message.publish_ns}};
      const size_t slot = next_slot_by_channel[message.channel];
      if (slot < arena_.available_slots(message.channel)) {
        next_slot_by_channel[message.channel] = slot + 1;
        arena_.encode(item, message.channel, slot, message.bytes);
      } else {
        InlineAdapter::encode(item, message.bytes);
      }
      in.push_back(std::move(item));
    }
    return in;
  }

  size_t inline_payload_bytes(const json &outputs) const {
    size_t total = 0;
    for (const json &item : outputs) {
      if (!is_inline(item)) continue;
      const size_t bytes = InlineAdapter::decoded_size(item);
      if (bytes > std::numeric_limits<size_t>::max() - total)
        throw RunError("inline payload byte count overflows size_t");
      total += bytes;
    }
    return total;
  }

  std::vector<StepOutput> decode_outputs(const json &encoded) {
    std::vector<StepOutput> outputs;
    for (const json &item : encoded) {
      StepOutput output;
      output.channel = item.at("ch").get<std::string>();
      // A Channel can legally carry inline fallbacks once its Arena slots
      // are full, so the Transport is read per Message, never per Channel.
      output.bytes = is_inline(item)
                         ? InlineAdapter::decode(item)
                         : arena_.decode(item, output.channel);
      outputs.push_back(std::move(output));
    }
    return outputs;
  }

 private:
  ArenaAdapter arena_;
};

// --- virtual clock shim wiring (issue #28) ---------------------------------
//
// The kernel owns a small fixed-layout time region (include/sil/clock_region.h)
// per shimmed participant: a memory-mapped temp file it writes and the child's
// preloaded shim maps read-only. The shim has no knowledge of the kernel; the
// only contract is the region layout and the SIL_CLOCK_REGION environment var.

void ProcessParticipant::setup_clock_region() {
  // The child's shim maps the region by path, so the file must stay on the
  // filesystem until the child has mapped it. The region owns that lifetime and
  // unlinks the file when it is released at run end. The failure taxonomy is
  // this call site's: an unmappable clock region is a run failure (exit 1).
  std::string error;
  clock_region_ =
      MappedRegion::create("sil_clock_", sizeof(sil_clock_region), error);
  if (!clock_region_)
    throw RunError("participant '" + name_ + "': clock region: " + error);
  auto *region = static_cast<volatile sil_clock_region *>(clock_region_.base());
  // t is set per step; epoch is fixed for the run. Start frozen at t=0 so the
  // child's load-time reads (before its first step) see run start, not garbage.
  region->t = 0;
  region->epoch = epoch_ns_;
  // Fixed for the run, like epoch: the shim reads it on every sleep call but
  // the kernel writes it once, here (issue #52).
  region->sleep_policy = sleep_policy_ == SleepPolicy::Reject
                             ? SIL_SLEEP_REJECT
                             : SIL_SLEEP_IMMEDIATE;

  // Resolve the shim library path here, in the parent: inject_shim_env runs
  // between fork and exec, where allocation and filesystem canonicalization are
  // not async-signal-safe, so nothing heavier than setenv may happen there.
  shim_lib_ = clock_shim_library_path().string();
}

void ProcessParticipant::inject_shim_env() const {
  // Runs in the forked child before exec. Both strings are already built in the
  // parent (setup_clock_region), so this only calls setenv — names the region
  // file and preloads the shim so the child's own POSIX clock reads are
  // interposed. LD_PRELOAD (Linux) / DYLD_INSERT_LIBRARIES (macOS): the same
  // split the shim's unit tests use.
  setenv(SIL_CLOCK_REGION_ENV, clock_region_.path().c_str(), 1);
#if defined(__APPLE__)
  setenv("DYLD_INSERT_LIBRARIES", shim_lib_.c_str(), 1);
#else
  setenv("LD_PRELOAD", shim_lib_.c_str(), 1);
#endif
}

void ProcessParticipant::write_clock_region(uint64_t now_ns) {
  if (!clock_region_) return;
  // epoch is invariant across steps; only t advances.
  static_cast<volatile sil_clock_region *>(clock_region_.base())->t = now_ns;
}

// --- channel arenas (issue #35) ------------------------------
//
// Translating the Manifest into Arenas is this class's job; the Arena layout
// itself, and every read and write through it, belongs to ChannelArenas.
// A create failure is an environment problem, not a bad manifest expressed in
// code — but the issue requires it to surface as a startup Manifest error
// (exit 2), which is why ChannelArenas throws ManifestError there.

void ProcessParticipant::setup_arenas(const ProcessSpec &spec) {
  const Manifest &m = engine_.manifest();

  // One Arena mapping has one writer direction. Supporting the same Channel in
  // both directions requires two mappings in the init shape; do not rely on a
  // third-party participant fully consuming inputs before it starts outputs.
  for (const std::string &ch : spec.publishes) {
    const ChannelSpec *c = m.find_channel(ch);
    if (c && c->transport == Transport::Shm &&
        std::find_if(spec.subscribes.begin(), spec.subscribes.end(),
                     [&ch](const auto &route) {
                       return route.channel == ch;
                     }) !=
            spec.subscribes.end())
      throw ManifestError("participant '" + name_ + "': channel '" + ch +
                          "' cannot be both subscribed and published over "
                          "shm transport");
  }

  auto map_channel = [&](const std::string &ch) {
    const ChannelSpec *c = m.find_channel(ch);
    if (!c || c->transport != Transport::Shm) return;
    arenas_.map(ch, m.schemas.at(c->schema).byte_size, c->slots);
  };
  for (const SubscriberRouteSpec &route : spec.subscribes)
    map_channel(route.channel);
  for (const std::string &ch : spec.publishes) map_channel(ch);
}

ProcessParticipant::ProcessParticipant(Engine &engine, const std::string &name,
                                       const ProcessSpec &spec,
                                       std::optional<std::chrono::milliseconds>
                                           participant_timeout,
                                       RunBoundaryLimits limits)
    : engine_(engine), name_(name), period_ns_(spec.step_period_ns),
      publishes_(spec.publishes), epoch_ns_(engine.manifest().epoch_ns),
      sleep_policy_(spec.sleep), arenas_(name),
      participant_timeout_(participant_timeout), limits_(limits) {
  try {
    const std::filesystem::path invocation_directory =
        std::filesystem::current_path();
    std::vector<std::string> command = spec.resolved_command;
    if (command.empty())
      command = resolve_command(spec.command, invocation_directory);
    std::string directory_error;
    OwnedDirectory directory = OwnedDirectory::create_child(
        engine.run_working_directory(), participant_directory_name(name),
        directory_error);
    if (!directory)
      throw ManifestError("participant '" + name +
                          "': cannot create working directory: " +
                          directory_error);
    working_directory_ =
        std::make_unique<OwnedDirectory>(std::move(directory));

    for (const SubscriberRouteSpec &route : spec.subscribes)
      inputs_.emplace_back(route.channel, engine.subscribe(name, route));

    // Shimmed participants get a shared time region mapped before fork, so the
    // child can map it read-only at load and the kernel can write virtual time
    // into it before each step. The region path and shim preload are injected
    // into the child's environment below.
    const bool shimmed = spec.shim;
    if (shimmed) setup_clock_region();

    // Create the Arenas before fork so the child inherits nothing but a path it
    // can re-open. Failures anywhere below leave no region behind: a throwing
    // constructor skips this class's destructor but still destroys the members
    // built so far, and each Mapped region releases its own file.
    setup_arenas(spec);
    // A Manifest that declares more than one slot on any Channel needs the
    // indexed-slot level to address the rest of them.
    if (arenas_.max_slots() > 1) protocol_ = kIndexedSlotsProtocol;
    const int offered_protocol = protocol_;

    int to_child[2], from_child[2];
    if (pipe(to_child) != 0 || pipe(from_child) != 0)
      throw RunError("participant '" + name + "': pipe failed");

    pid_ = fork();
    if (pid_ < 0) throw RunError("participant '" + name + "': fork failed");
    if (pid_ == 0) {
      dup2(to_child[0], STDIN_FILENO);
      dup2(from_child[1], STDOUT_FILENO);
      close(to_child[0]);
      close(to_child[1]);
      close(from_child[0]);
      close(from_child[1]);
      if (chdir(working_directory_->path().c_str()) != 0) {
        perror("sil: chdir participant");
        _exit(127);
      }
      if (shimmed) inject_shim_env();
      std::vector<char *> argv;
      for (const std::string &arg : command)
        argv.push_back(const_cast<char *>(arg.c_str()));
      argv.push_back(nullptr);
      execvp(argv[0], argv.data());
      perror("sil: exec participant");
      _exit(127);
    }
    close(to_child[0]);
    close(from_child[1]);
    child_stdin_ = to_child[1];
    child_stdout_ = from_child[0];
    alive_ = true;

    const Manifest &m = engine.manifest();
    json channels = json::object();
    json schemas = json::object();
    auto add_channel = [&](const std::string &ch, const char *direction) {
      const ChannelSpec *c = m.find_channel(ch);
      json entry = {{"schema", c->schema}, {"direction", direction}};
      // For arena-backed channels, hand the child the arena path + capacity so it maps
      // the same MAP_SHARED region and moves payloads through it. Absent
      // "transport" means inline (the base64/JSON path), keeping existing
      // manifests byte-identical on the wire.
      if (c->transport == Transport::Shm) {
        const ChannelArenas::Layout layout = arenas_.layout(ch);
        entry["transport"] = "shm";
        entry["shm_path"] = layout.path;
        entry["shm_capacity"] = layout.capacity;
        entry["shm_slots"] = layout.slots;
      }
      channels[ch] = entry;
      schemas[c->schema] = json::parse(m.schemas.at(c->schema).canonical_json);
    };
    for (const SubscriberRouteSpec &route : spec.subscribes)
      add_channel(route.channel, "in");
    for (const std::string &ch : spec.publishes) add_channel(ch, "out");

    json init = {{"op", "init"},
                 {"name", name},
                 {"protocol", offered_protocol},
                 {"channels", channels},
                 {"schemas", schemas}};
    json ready = json::parse(request_response(init.dump(), std::nullopt));
    const std::string op = ready.value("op", "");
    // `fail` in answer to `init` is a Manifest error (exit 2), unless the child
    // explicitly marks a failure from its own initialization work as a Run
    // failure (exit 1). The same line after a Step is always a Run failure.
    // See docs/step-protocol.md.
    if (op == "fail") {
      const std::string reason =
          ready.value("reason", "rejected its init line");
      if (ready.value("failure", "") == "run")
        throw RunError("participant '" + name + "' failed: " + reason);
      throw ManifestError("participant '" + name + "': " + reason);
    }
    if (op != "ready")
      throw RunError("participant '" + name + "': expected ready, got " +
                     ready.dump());
    int announced_protocol = kSingleSlotProtocol;
    if (ready.contains("protocol")) {
      const json &value = ready.at("protocol");
      if (!value.is_number_integer())
        throw ManifestError("participant '" + name +
                            "': ready protocol must be a positive integer");
      const int64_t announced = value.get<int64_t>();
      if (announced < 1 || announced > std::numeric_limits<int>::max())
        throw ManifestError("participant '" + name +
                            "': ready protocol must be a positive integer");
      announced_protocol = static_cast<int>(announced);
    }
    protocol_ = std::min(offered_protocol, announced_protocol);
    codec_ = std::make_unique<StepCodec>(arenas_, protocol_);
  } catch (...) {
    // This intentionally covers every init-path throw, not only timeouts: an
    // object whose child has already been spawned is not fully constructed,
    // so its destructor cannot reap the child. Keep the same cleanup policy
    // used for every later Run failure here.
    (void)terminate_child();
    throw;
  }
}

ProcessParticipant::~ProcessParticipant() {
  // Destruction is also the cleanup path after an already-reported RunError,
  // so it reaps the child without inspecting the exit status: a diagnostic
  // thrown from here would replace the original one, and a destructor must
  // not throw at all.
  terminate_child();
}

void ProcessParticipant::step(uint64_t now_ns) {
  // Merge visible inputs across channels in global publish order.
  std::vector<StepInput> messages;
  for (;;) {
    SubscriberRoute *best = nullptr;
    for (auto &[ch, q] : inputs_)
      if (q->visible_at(now_ns) &&
          (!best || q->front_seq() < best->front_seq()))
        best = q;
    if (!best) break;
    PendingMessage msg;
    engine_.take(*best, msg);
    messages.push_back(
        {best->channel(), msg.publish_ns, std::move(msg.bytes)});
  }

  json in = codec_->encode_inputs(messages);

  // Freeze this step's virtual time into the shared region before the child
  // runs, so every clock read inside it (through the shim) returns exactly
  // now_ns until the next step advances it. No-op for unshimmed participants.
  write_clock_region(now_ns);

  json step = {
      {"op", "step"}, {"t", now_ns}, {"dt", period_ns_}, {"in", in}};
  json done = json::parse(request_response(step.dump(), now_ns));

  std::string op = done.value("op", "");
  if (op == "fail") {
    engine_.fail(name_, done.value("reason", "(no reason)"));
    return;
  }
  if (op != "step_done")
    throw RunError("participant '" + name_ + "': expected step_done, got " +
                   done.dump());

  static const json kNoOutputs = json::array();
  const json &outputs = done.contains("out") ? done.at("out") : kNoOutputs;
  if (!outputs.is_array())
    throw RunError("participant '" + name_ +
                   "': step_done out must be an array");
  if (outputs.size() > limits_.max_step_output_messages)
    throw RunError(
        "participant '" + name_ +
        "': maximum output-Message count per Step exceeded during Step at "
        "virtual time " + std::to_string(now_ns) + " ns: configured " +
        std::to_string(limits_.max_step_output_messages) + " Messages, "
        "observed " + std::to_string(outputs.size()) + " Messages");

  for (const json &out : outputs) {
    std::string ch = out.at("ch").get<std::string>();
    if (std::find(publishes_.begin(), publishes_.end(), ch) ==
        publishes_.end()) {
      engine_.fail(name_, "published on undeclared channel '" + ch + "'");
      return;
    }
  }

  const size_t inline_bytes = codec_->inline_payload_bytes(outputs);
  if (inline_bytes > limits_.max_step_inline_payload_bytes)
    throw RunError(
        "participant '" + name_ +
        "': maximum total inline payload bytes per Step exceeded during Step "
        "at virtual time " + std::to_string(now_ns) + " ns: configured " +
        std::to_string(limits_.max_step_inline_payload_bytes) + " bytes, "
        "observed " + std::to_string(inline_bytes) + " bytes");

  for (StepOutput &output : codec_->decode_outputs(outputs))
    engine_.publish(name_, output.channel, output.bytes.data(),
                    output.bytes.size());
}

void ProcessParticipant::send_line(const std::string &line) {
  std::string data = line + "\n";
  size_t off = 0;
  while (off < data.size()) {
    ssize_t n = write(child_stdin_, data.data() + off, data.size() - off);
    if (n < 0)
      throw RunError("participant '" + name_ + "': write failed (exited?)");
    off += size_t(n);
  }
}

std::string ProcessParticipant::request_response(
    const std::string &line, std::optional<uint64_t> step_time) {
  std::optional<Clock::time_point> deadline;
  if (participant_timeout_) {
    const Clock::time_point start = Clock::now();
    const auto max_timeout = std::chrono::duration_cast<
        std::chrono::milliseconds>(Clock::time_point::max() - start);
    if (*participant_timeout_ >= max_timeout) {
      // A milliseconds duration can outlive the clock's nanosecond time_point
      // range. Treat that valid CLI value as an effectively unbounded deadline
      // rather than allowing the time_point addition to overflow.
      deadline = Clock::time_point::max();
    } else {
      deadline = start +
                 std::chrono::duration_cast<Clock::duration>(
                     *participant_timeout_);
    }
  }
  send_line(line);
  return read_line(deadline, step_time);
}

std::string ProcessParticipant::read_line(
    const std::optional<Clock::time_point> &deadline,
    std::optional<uint64_t> step_time) {
  const auto line_limit = [&](size_t observed) -> RunError {
    if (!step_time)
      return RunError(
          "participant '" + name_ +
          "': maximum protocol line length exceeded while waiting for "
          "initialization ready response: configured " +
          std::to_string(limits_.max_protocol_line_bytes) +
          " bytes, observed " + std::to_string(observed) + " bytes");
    return RunError(
        "participant '" + name_ +
        "': maximum protocol line length exceeded during Step at virtual "
        "time " + std::to_string(*step_time) + " ns: configured " +
        std::to_string(limits_.max_protocol_line_bytes) +
        " bytes, observed " + std::to_string(observed) + " bytes");
  };

  const auto over_limit_observed = [&](size_t base, size_t added) {
    if (base > std::numeric_limits<size_t>::max() - added)
      return std::numeric_limits<size_t>::max();
    return base + added;
  };

  // Inspect the bytes before appending them. The local read buffer is fixed at
  // 4 KiB, but a participant cannot make the persistent response buffer grow
  // beyond the configured line limit while it withholds the newline.
  const auto append_checked = [&](const char *data, size_t length) {
    size_t chunk_start = 0;
    bool first_line = true;
    for (;;) {
      const size_t newline =
          std::string_view(data + chunk_start, length - chunk_start)
              .find('\n');
      if (newline == std::string_view::npos) {
        const size_t base = first_line ? read_buffer_.size() : 0;
        if (base > limits_.max_protocol_line_bytes ||
            length - chunk_start >
                limits_.max_protocol_line_bytes - base)
          throw line_limit(over_limit_observed(base, length - chunk_start));
        break;
      }

      const size_t base = first_line ? read_buffer_.size() : 0;
      if (base > limits_.max_protocol_line_bytes ||
          newline > limits_.max_protocol_line_bytes - base)
        throw line_limit(over_limit_observed(base, newline));
      first_line = false;
      chunk_start += newline + 1;
      if (chunk_start == length) break;
    }
    read_buffer_.append(data, length);
  };

  const auto timeout = [&]() -> RunError {
    if (!step_time)
      return RunError("participant '" + name_ +
                      "': timeout waiting for initialization ready response");
    return RunError(
        "participant '" + name_ +
        "': timeout waiting for step_done response during Step at virtual "
        "time " +
        std::to_string(*step_time) + " ns");
  };

  for (;;) {
    size_t nl = read_buffer_.find('\n');
    if (nl != std::string::npos) {
      std::string line = read_buffer_.substr(0, nl);
      read_buffer_.erase(0, nl + 1);
      return line;
    }

    if (!deadline) {
      char buf[4096];
      ssize_t n = read(child_stdout_, buf, sizeof buf);
      if (n < 0 && errno == EINTR) continue;
      if (n <= 0)
        throw RunError("participant '" + name_ + "' exited unexpectedly");
      append_checked(buf, size_t(n));
      continue;
    }

    const Clock::duration remaining = *deadline - Clock::now();
    if (remaining <= Clock::duration::zero()) throw timeout();
    auto wait_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        remaining);
    if (wait_ms.count() == 0) wait_ms = std::chrono::milliseconds(1);
    const auto max_poll_ms = std::chrono::milliseconds(
        std::numeric_limits<int>::max());
    if (wait_ms > max_poll_ms) wait_ms = max_poll_ms;

    pollfd descriptor{child_stdout_, POLLIN, 0};
    const int ready =
        poll(&descriptor, 1, static_cast<int>(wait_ms.count()));
    if (ready < 0 && errno == EINTR) continue;
    if (ready == 0) throw timeout();
    if (ready < 0)
      throw RunError("participant '" + name_ + "': poll failed");

    char buf[4096];
    ssize_t n = read(child_stdout_, buf, sizeof buf);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0)
      throw RunError("participant '" + name_ + "' exited unexpectedly");
    append_checked(buf, size_t(n));
  }
}

int ProcessParticipant::terminate_child() {
  if (!alive_) return 0;
  alive_ = false;
  json bye = {{"op", "shutdown"}};
  std::string data = bye.dump() + "\n";
  // Best effort: child may already be gone.
  (void)!write(child_stdin_, data.data(), data.size());
  close(child_stdin_);
  close(child_stdout_);

  int wait_status = 0;
  auto reap_within = [&](int centiseconds) {
    for (int i = 0; i < centiseconds; i++) {
      if (waitpid(pid_, &wait_status, WNOHANG) == pid_) return true;
      usleep(10000);
    }
    return false;
  };
  // A child that does not answer `shutdown` is asked with SIGTERM before it is
  // killed, so a participant holding run-scoped state of its own — an imported
  // FMU's extracted archive, say — still reaches its own cleanup. SIGKILL is
  // the last resort for a child that ignores both, and leaves that cleanup
  // undone by definition.
  if (!reap_within(200)) {
    kill(pid_, SIGTERM);
    if (!reap_within(100)) {
      kill(pid_, SIGKILL);
      waitpid(pid_, &wait_status, 0);
    }
  }
  // Release the regions at run end rather than at destruction, so a finished
  // run leaves nothing in the temp directory even while the engine still holds
  // the participant. Both releases are the owning type's destructor.
  arenas_.release();
  clock_region_ = MappedRegion();
  working_directory_.reset();
  return wait_status;
}

void ProcessParticipant::shutdown() {
  if (!alive_) return;
  const int wait_status = terminate_child();
  if (WIFEXITED(wait_status) && WEXITSTATUS(wait_status) != 0)
    throw RunError("participant '" + name_ + "' exited with status " +
                   std::to_string(WEXITSTATUS(wait_status)));
  if (WIFSIGNALED(wait_status))
    throw RunError("participant '" + name_ + "' terminated by signal " +
                   std::to_string(WTERMSIG(wait_status)));
}

}  // namespace sil
