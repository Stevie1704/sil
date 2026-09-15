#include "process_participant.hpp"

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <limits>
#include <map>
#include <utility>

#include <nlohmann/json.hpp>

#include "sil/arena.h"

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

std::vector<uint8_t> b64_decode(const std::string &in) {
  static int8_t table[256];
  static bool init = [] {
    for (int i = 0; i < 256; i++) table[i] = -1;
    for (int i = 0; i < 64; i++) table[uint8_t(kB64[i])] = int8_t(i);
    return true;
  }();
  (void)init;

  std::vector<uint8_t> out;
  uint32_t acc = 0;
  int bits = 0;
  for (char ch : in) {
    if (ch == '=') break;
    int8_t v = table[uint8_t(ch)];
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

class ProcessParticipant::StepCodec {
 private:
  class InlineAdapter {
   public:
    static void encode(nlohmann::json &item,
                       const std::vector<uint8_t> &bytes) {
      counters::count(counters::Site::kInlineEncode, bytes.size());
      item["data"] = b64_encode(bytes);
    }

    static std::vector<uint8_t> decode(const nlohmann::json &item) {
      std::vector<uint8_t> bytes = b64_decode(item.at("data").get<std::string>());
      counters::count(counters::Site::kInlineDecode, bytes.size());
      return bytes;
    }
  };

  class ArenaAdapter {
   public:
    explicit ArenaAdapter(ProcessParticipant &owner) : owner_(owner) {}

    void encode(nlohmann::json &item, const std::string &channel, size_t slot,
                const std::vector<uint8_t> &bytes) {
      if (owner_.protocol_ >= ProcessParticipant::kIndexedSlotsProtocol)
        item["shm_slot"] = slot;
      item["shm_seq"] = owner_.write_arena(channel, slot, bytes);
    }

    std::vector<uint8_t> decode(const nlohmann::json &item,
                                const std::string &channel) {
      std::vector<uint8_t> bytes;
      const size_t slot = item.value("shm_slot", size_t{0});
      const Arena &arena = owner_.arenas_.at(channel);
      const size_t available =
          owner_.protocol_ >= ProcessParticipant::kIndexedSlotsProtocol
              ? arena.slots
              : 1;
      if (slot >= available)
        throw RunError("participant '" + owner_.name_ + "' channel '" +
                       channel + "': arena slot " + std::to_string(slot) +
                       " exceeds negotiated slot count " +
                       std::to_string(available));
      owner_.read_arena(channel, slot,
                        item.at("shm_seq").get<uint64_t>(), bytes);
      return bytes;
    }

   private:
    ProcessParticipant &owner_;
  };

 public:
  explicit StepCodec(ProcessParticipant &owner)
      : owner_(owner), arena_(owner) {}

  nlohmann::json encode_inputs(
      const std::vector<ProcessParticipant::StepInput> &messages) {
    nlohmann::json in = nlohmann::json::array();
    // These indices belong to one codec call, so slot reuse is inherently
    // scoped to one Step and cannot leak into the next one.
    std::map<std::string, size_t> next_slot_by_channel;
    for (const ProcessParticipant::StepInput &message : messages) {
      nlohmann::json item = {
          {"ch", message.channel}, {"t", message.publish_ns}};
      auto arena_it = owner_.arenas_.find(message.channel);
      const size_t slot = next_slot_by_channel[message.channel];
      const size_t available =
          arena_it == owner_.arenas_.end()
              ? 0
              : (owner_.protocol_ >= ProcessParticipant::kIndexedSlotsProtocol
                     ? arena_it->second.slots
                     : 1);
      if (slot < available) {
        next_slot_by_channel[message.channel] = slot + 1;
        arena_.encode(item, message.channel, slot, message.bytes);
      } else {
        inline_.encode(item, message.bytes);
      }
      in.push_back(std::move(item));
    }
    return in;
  }

  std::vector<ProcessParticipant::StepOutput> decode_outputs(
      const nlohmann::json &message) {
    std::vector<ProcessParticipant::StepOutput> outputs;
    for (const nlohmann::json &item :
         message.value("out", nlohmann::json::array())) {
      ProcessParticipant::StepOutput output;
      output.channel = item.at("ch").get<std::string>();
      // The field on the line is authoritative. A channel can legally carry
      // inline fallbacks after its Arena slots are full.
      if (item.contains("shm_seq")) {
        output.bytes = arena_.decode(item, output.channel);
      } else {
        output.bytes = inline_.decode(item);
      }
      outputs.push_back(std::move(output));
    }
    return outputs;
  }

 private:
  ProcessParticipant &owner_;
  InlineAdapter inline_;
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
// One arena per arena-backed channel, an mmap'd temp file mapped MAP_SHARED
// before fork
// so the child maps the same file by path at load. A create/map failure is an
// environment problem, not a bad manifest expressed in code — but the issue
// requires it to surface as a startup Manifest error (exit 2), so we throw
// ManifestError, which main() maps to exit 2 (RunError would be exit 1).

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
    if (arenas_.count(ch)) return;  // idempotent across the pub/sub passes below
    const ChannelSpec *c = m.find_channel(ch);
    if (!c || c->transport != Transport::Shm) return;
    const size_t capacity = m.schemas.at(c->schema).byte_size;
    if (capacity > std::numeric_limits<size_t>::max() - sizeof(sil_arena) ||
        c->slots > std::numeric_limits<size_t>::max() /
                       (sizeof(sil_arena) + capacity))
      throw ManifestError("participant '" + name_ + "' channel '" + ch +
                          "': arena mapping size overflows size_t");
    const size_t stride = sizeof(sil_arena) + capacity;

    // The failure taxonomy is this call site's: an arena the environment cannot
    // supply is a Manifest error (exit 2). Arenas already mapped in this loop, and
    // the clock region, are released by their own destructors as this throws.
    std::string error;
    Arena a;
    a.region = MappedRegion::create("sil_arena_", stride * c->slots, error);
    if (!a.region)
      throw ManifestError("participant '" + name_ + "' channel '" + ch +
                          "': arena: " + error);
    a.capacity = capacity;
    a.slots = c->slots;
    for (size_t slot = 0; slot < a.slots; ++slot) {
      auto *hdr = reinterpret_cast<sil_arena *>(
          static_cast<uint8_t *>(a.region.base()) + slot * stride);
      hdr->seq = 0;
      hdr->len = 0;
    }
    arenas_.emplace(ch, std::move(a));
  };
  for (const SubscriberRouteSpec &route : spec.subscribes)
    map_channel(route.channel);
  for (const std::string &ch : spec.publishes) map_channel(ch);
}

uint64_t ProcessParticipant::write_arena(const std::string &channel,
                                         size_t slot,
                                         const std::vector<uint8_t> &bytes) {
  Arena &a = arenas_.at(channel);
  if (slot >= a.slots)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': arena slot out of range");
  if (bytes.size() > a.capacity)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': payload exceeds arena capacity");
  const size_t offset = slot * (sizeof(sil_arena) + a.capacity);
  auto *hdr = reinterpret_cast<sil_arena *>(
      static_cast<uint8_t *>(a.region.base()) + offset);
  counters::count(counters::Site::kArenaWrite, bytes.size());
  std::memcpy(static_cast<uint8_t *>(a.region.base()) + offset +
                  sizeof(sil_arena),
              bytes.data(), bytes.size());
  hdr->len = bytes.size();
  hdr->seq = ++a.seq;
  return a.seq;
}

void ProcessParticipant::read_arena(const std::string &channel, size_t slot,
                                    uint64_t seq,
                                    std::vector<uint8_t> &out) {
  Arena &a = arenas_.at(channel);
  if (slot >= a.slots)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': arena slot out of range");
  const size_t offset = slot * (sizeof(sil_arena) + a.capacity);
  auto *hdr = reinterpret_cast<sil_arena *>(
      static_cast<uint8_t *>(a.region.base()) + offset);
  if (hdr->seq != seq)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': stale arena slot " + std::to_string(slot) +
                   " (expected seq " + std::to_string(seq) +
                   ", got " + std::to_string(hdr->seq) + ")");
  if (hdr->len > a.capacity)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': arena len exceeds capacity");
  const auto *payload =
      static_cast<const uint8_t *>(a.region.base()) + offset +
      sizeof(sil_arena);
  counters::count(counters::Site::kArenaRead, hdr->len);
  out.assign(payload, payload + hdr->len);
}

ProcessParticipant::ProcessParticipant(Engine &engine, const std::string &name,
                                       const ProcessSpec &spec)
    : engine_(engine), name_(name), period_ns_(spec.step_period_ns),
      publishes_(spec.publishes), epoch_ns_(engine.manifest().epoch_ns),
      sleep_policy_(spec.sleep) {
  const std::filesystem::path invocation_directory =
      std::filesystem::current_path();
  const std::vector<std::string> command =
      resolve_command(spec.command, invocation_directory);
  std::string directory_error;
  OwnedDirectory directory = OwnedDirectory::create_child(
      engine.run_working_directory(), participant_directory_name(name),
      directory_error);
  if (!directory)
    throw ManifestError("participant '" + name +
                        "': cannot create working directory: " +
                        directory_error);
  working_directory_ = std::make_unique<OwnedDirectory>(std::move(directory));

  for (const SubscriberRouteSpec &route : spec.subscribes)
    inputs_.emplace_back(route.channel, engine.subscribe(name, route));

  // Shimmed participants get a shared time region mapped before fork, so the
  // child can map it read-only at load and the kernel can write virtual time
  // into it before each step. The region path and shim preload are injected
  // into the child's environment below.
  const bool shimmed = spec.shim;
  if (shimmed) setup_clock_region();

  // Map the arenas before fork so the child inherits nothing but a path it can
  // re-open. Failures anywhere below leave no region behind: a throwing
  // constructor skips this class's destructor but still destroys the members
  // built so far, and each mapped region releases its own file.
  setup_arenas(spec);
  codec_ = std::make_unique<StepCodec>(*this);
  for (const auto &[channel, arena] : arenas_) {
    (void)channel;
    if (arena.slots > 1) protocol_ = kIndexedSlotsProtocol;
  }
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
      const Arena &a = arenas_.at(ch);
      entry["transport"] = "shm";
      entry["shm_path"] = a.region.path();
      entry["shm_capacity"] = a.capacity;
      entry["shm_slots"] = a.slots;
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
  send_line(init.dump());
  json ready = json::parse(read_line());
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
  send_line(step.dump());

  json done = json::parse(read_line());
  std::string op = done.value("op", "");
  if (op == "fail") {
    engine_.fail(name_, done.value("reason", "(no reason)"));
    return;
  }
  if (op != "step_done")
    throw RunError("participant '" + name_ + "': expected step_done, got " +
                   done.dump());
  for (const json &out : done.value("out", json::array())) {
    std::string ch = out.at("ch").get<std::string>();
    if (std::find(publishes_.begin(), publishes_.end(), ch) ==
        publishes_.end()) {
      engine_.fail(name_, "published on undeclared channel '" + ch + "'");
      return;
    }
  }
  for (StepOutput &output : codec_->decode_outputs(done))
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

std::string ProcessParticipant::read_line() {
  for (;;) {
    size_t nl = read_buffer_.find('\n');
    if (nl != std::string::npos) {
      std::string line = read_buffer_.substr(0, nl);
      read_buffer_.erase(0, nl + 1);
      return line;
    }
    char buf[4096];
    ssize_t n = read(child_stdout_, buf, sizeof buf);
    if (n <= 0)
      throw RunError("participant '" + name_ + "' exited unexpectedly");
    read_buffer_.append(buf, size_t(n));
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
  arenas_.clear();
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
