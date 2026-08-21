#include "process_participant.hpp"

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <set>
#include <utility>

#include <nlohmann/json.hpp>

#include "sil/arena.h"

#include "clock_shim.hpp"

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

}  // namespace

class ProcessParticipant::StepCodec {
 private:
  class InlineAdapter {
   public:
    static void encode(nlohmann::json &item,
                       const std::vector<uint8_t> &bytes) {
      item["data"] = b64_encode(bytes);
    }

    static std::vector<uint8_t> decode(const nlohmann::json &item) {
      return b64_decode(item.at("data").get<std::string>());
    }
  };

  class ArenaAdapter {
   public:
    explicit ArenaAdapter(ProcessParticipant &owner) : owner_(owner) {}

    void encode(nlohmann::json &item, const std::string &channel,
                const std::vector<uint8_t> &bytes) {
      item["shm_seq"] = owner_.write_arena(channel, bytes);
    }

    std::vector<uint8_t> decode(const nlohmann::json &item,
                                const std::string &channel) {
      std::vector<uint8_t> bytes;
      owner_.read_arena(channel, item.at("shm_seq").get<uint64_t>(), bytes);
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
    // This set belongs to one codec call, so the one-slot fallback is
    // inherently scoped to one step and cannot leak into the next one.
    std::set<std::string> arena_used;
    for (const ProcessParticipant::StepInput &message : messages) {
      nlohmann::json item = {
          {"ch", message.channel}, {"t", message.publish_ns}};
      if (owner_.arenas_.count(message.channel) &&
          arena_used.insert(message.channel).second) {
        arena_.encode(item, message.channel, message.bytes);
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
      // inline fallbacks after its arena-backed first message.
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
  // A plain mmap'd file (not POSIX shm) for portability — the child's shim maps
  // it by path, so it must stay on the filesystem until the child has mapped
  // it; teardown_clock_region unlinks it at run end. Created in the OS temp dir.
  const char *tmp = getenv("TMPDIR");
  std::string tpl =
      (tmp && *tmp ? std::string(tmp) : std::string("/tmp")) + "/sil_clock_XXXXXX";
  std::vector<char> path(tpl.begin(), tpl.end());
  path.push_back('\0');
  region_fd_ = mkstemp(path.data());
  if (region_fd_ < 0)
    throw RunError("participant '" + name_ + "': clock region: mkstemp failed");
  region_path_ = path.data();
  if (ftruncate(region_fd_, sizeof(sil_clock_region)) != 0) {
    teardown_clock_region();
    throw RunError("participant '" + name_ + "': clock region: ftruncate failed");
  }
  void *p = mmap(nullptr, sizeof(sil_clock_region), PROT_READ | PROT_WRITE,
                 MAP_SHARED, region_fd_, 0);
  if (p == MAP_FAILED) {
    teardown_clock_region();
    throw RunError("participant '" + name_ + "': clock region: mmap failed");
  }
  region_ = static_cast<volatile sil_clock_region *>(p);
  // t is set per step; epoch is fixed for the run. Start frozen at t=0 so the
  // child's load-time reads (before its first step) see run start, not garbage.
  region_->t = 0;
  region_->epoch = epoch_ns_;

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
  setenv(SIL_CLOCK_REGION_ENV, region_path_.c_str(), 1);
#if defined(__APPLE__)
  setenv("DYLD_INSERT_LIBRARIES", shim_lib_.c_str(), 1);
#else
  setenv("LD_PRELOAD", shim_lib_.c_str(), 1);
#endif
}

void ProcessParticipant::write_clock_region(uint64_t now_ns) {
  if (!region_) return;
  region_->t = now_ns;  // epoch is invariant across steps; only t advances
}

void ProcessParticipant::teardown_clock_region() {
  if (region_) {
    munmap(const_cast<sil_clock_region *>(region_), sizeof(sil_clock_region));
    region_ = nullptr;
  }
  if (region_fd_ >= 0) {
    close(region_fd_);
    region_fd_ = -1;
  }
  if (!region_path_.empty()) {
    unlink(region_path_.c_str());
    region_path_.clear();
  }
}

// --- channel arenas (issue #35) ------------------------------
//
// One arena per arena-backed channel, an mmap'd temp file mapped MAP_SHARED
// before fork
// so the child maps the same file by path at load. A create/map failure is an
// environment problem, not a bad manifest expressed in code — but the issue
// requires it to surface as a startup config error (exit 2), so we throw
// ManifestError, which main() maps to exit 2 (RunError would be exit 1).

void ProcessParticipant::setup_arenas(const ProcessSpec &spec) {
  const Manifest &m = engine_.manifest();

  // A single-slot arena carries one direction. A participant that both
  // subscribes and publishes the same arena-backed channel would race an input
  // and an output write through one header — out of scope (no feedback loop over the
  // large-payload path), so reject it at load rather than silently corrupt.
  for (const std::string &ch : spec.publishes) {
    const ChannelSpec *c = m.find_channel(ch);
    if (c && c->transport == Transport::Shm &&
        std::find(spec.subscribes.begin(), spec.subscribes.end(), ch) !=
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
    const size_t map_size = sizeof(sil_arena) + capacity;

    const char *tmp = getenv("TMPDIR");
    std::string tpl = (tmp && *tmp ? std::string(tmp) : std::string("/tmp")) +
                      "/sil_arena_XXXXXX";
    std::vector<char> path(tpl.begin(), tpl.end());
    path.push_back('\0');
    Arena a;
    a.fd = mkstemp(path.data());
    if (a.fd < 0)
      throw ManifestError("participant '" + name_ + "' channel '" + ch +
                          "': arena: mkstemp failed");
    a.path = path.data();
    a.capacity = capacity;
    a.map_size = map_size;
    if (ftruncate(a.fd, off_t(map_size)) != 0) {
      close(a.fd);
      unlink(a.path.c_str());
      throw ManifestError("participant '" + name_ + "' channel '" + ch +
                          "': arena: ftruncate failed");
    }
    a.base = mmap(nullptr, map_size, PROT_READ | PROT_WRITE, MAP_SHARED, a.fd, 0);
    if (a.base == MAP_FAILED) {
      close(a.fd);
      unlink(a.path.c_str());
      throw ManifestError("participant '" + name_ + "' channel '" + ch +
                          "': arena: mmap failed");
    }
    auto *hdr = static_cast<sil_arena *>(a.base);
    hdr->seq = 0;
    hdr->len = 0;
    arenas_.emplace(ch, std::move(a));
  };
  for (const std::string &ch : spec.subscribes) map_channel(ch);
  for (const std::string &ch : spec.publishes) map_channel(ch);
}

uint64_t ProcessParticipant::write_arena(const std::string &channel,
                                         const std::vector<uint8_t> &bytes) {
  Arena &a = arenas_.at(channel);
  if (bytes.size() > a.capacity)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': payload exceeds arena capacity");
  auto *hdr = static_cast<sil_arena *>(a.base);
  std::memcpy(static_cast<uint8_t *>(a.base) + sizeof(sil_arena),
              bytes.data(), bytes.size());
  hdr->len = bytes.size();
  hdr->seq = ++a.seq;
  return a.seq;
}

void ProcessParticipant::read_arena(const std::string &channel, uint64_t seq,
                                    std::vector<uint8_t> &out) {
  Arena &a = arenas_.at(channel);
  auto *hdr = static_cast<sil_arena *>(a.base);
  if (hdr->seq != seq)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': stale arena (expected seq " + std::to_string(seq) +
                   ", got " + std::to_string(hdr->seq) + ")");
  if (hdr->len > a.capacity)
    throw RunError("participant '" + name_ + "' channel '" + channel +
                   "': arena len exceeds capacity");
  const auto *payload =
      static_cast<const uint8_t *>(a.base) + sizeof(sil_arena);
  out.assign(payload, payload + hdr->len);
}

void ProcessParticipant::teardown_arenas() {
  for (auto &[ch, a] : arenas_) {
    if (a.base && a.base != MAP_FAILED) munmap(a.base, a.map_size);
    if (a.fd >= 0) close(a.fd);
    if (!a.path.empty()) unlink(a.path.c_str());
  }
  arenas_.clear();
}

ProcessParticipant::ProcessParticipant(Engine &engine, const std::string &name,
                                       const ProcessSpec &spec)
    : engine_(engine), name_(name), period_ns_(spec.step_period_ns),
      publishes_(spec.publishes), epoch_ns_(engine.manifest().epoch_ns) {
  for (const std::string &ch : spec.subscribes)
    inputs_.emplace_back(ch, engine.subscribe(name, ch));

  // Shimmed participants get a shared time region mapped before fork, so the
  // child can map it read-only at load and the kernel can write virtual time
  // into it before each step. The region path and shim preload are injected
  // into the child's environment below. Set up before fork; on any failure
  // between here and a fully-live participant the region must be released, as a
  // throwing constructor never runs the destructor.
  const bool shimmed = spec.shim;
  if (shimmed) setup_clock_region();

  // Map the arenas before fork so the child inherits nothing but a path it can
  // re-open. Any failure here throws ManifestError (exit 2) and must release
  // the arenas + clock region, since a throwing constructor skips the dtor.
  try {
    setup_arenas(spec);
  } catch (...) {
    teardown_arenas();
    teardown_clock_region();
    throw;
  }
  codec_ = std::make_unique<StepCodec>(*this);

  try {
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
      if (shimmed) inject_shim_env();
      std::vector<char *> argv;
      for (const std::string &arg : spec.command)
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
        entry["shm_path"] = a.path;
        entry["shm_capacity"] = a.capacity;
      }
      channels[ch] = entry;
      schemas[c->schema] = json::parse(m.schemas.at(c->schema).canonical_json);
    };
    for (const std::string &ch : spec.subscribes) add_channel(ch, "in");
    for (const std::string &ch : spec.publishes) add_channel(ch, "out");

    json init = {{"op", "init"},
                 {"name", name},
                 {"channels", channels},
                 {"schemas", schemas}};
    send_line(init.dump());
    json ready = json::parse(read_line());
    if (ready.value("op", "") != "ready")
      throw RunError("participant '" + name + "': expected ready, got " +
                     ready.dump());
  } catch (...) {
    teardown_arenas();
    teardown_clock_region();
    throw;
  }
}

ProcessParticipant::~ProcessParticipant() {
  shutdown();
}

void ProcessParticipant::step(uint64_t now_ns) {
  // Merge visible inputs across channels in global publish order.
  std::vector<StepInput> messages;
  for (;;) {
    SubQueue *best = nullptr;
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

void ProcessParticipant::shutdown() {
  if (!alive_) return;
  alive_ = false;
  json bye = {{"op", "shutdown"}};
  std::string data = bye.dump() + "\n";
  // Best effort: child may already be gone.
  (void)!write(child_stdin_, data.data(), data.size());
  close(child_stdin_);
  close(child_stdout_);

  for (int i = 0; i < 200; i++) {
    int status = 0;
    pid_t r = waitpid(pid_, &status, WNOHANG);
    if (r == pid_) {
      teardown_clock_region();
      teardown_arenas();
      return;
    }
    usleep(10000);
  }
  kill(pid_, SIGKILL);
  waitpid(pid_, nullptr, 0);
  teardown_clock_region();
  teardown_arenas();
}

}  // namespace sil
