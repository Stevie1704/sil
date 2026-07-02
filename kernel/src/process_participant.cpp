#include "process_participant.hpp"

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>

#include <nlohmann/json.hpp>

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

ProcessParticipant::ProcessParticipant(Engine &engine, const std::string &name,
                                       const ProcessSpec &spec)
    : engine_(engine), name_(name), period_ns_(spec.step_period_ns),
      publishes_(spec.publishes) {
  for (const std::string &ch : spec.subscribes)
    inputs_.emplace_back(ch, engine.subscribe(name, ch));

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
    channels[ch] = {{"schema", c->schema}, {"direction", direction}};
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
}

ProcessParticipant::~ProcessParticipant() {
  shutdown();
}

void ProcessParticipant::step(uint64_t now_ns) {
  // Merge visible inputs across channels in global publish order.
  json in = json::array();
  for (;;) {
    SubQueue *best = nullptr;
    for (auto &[ch, q] : inputs_)
      if (q->visible_at(now_ns) &&
          (!best || q->front_seq() < best->front_seq()))
        best = q;
    if (!best) break;
    PendingMessage msg;
    engine_.take(*best, msg);
    in.push_back({{"ch", best->channel()},
                  {"t", msg.publish_ns},
                  {"data", b64_encode(msg.bytes)}});
  }

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
    std::vector<uint8_t> bytes = b64_decode(out.at("data").get<std::string>());
    engine_.publish(name_, ch, bytes.data(), bytes.size());
  }
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
    if (r == pid_) return;
    usleep(10000);
  }
  kill(pid_, SIGKILL);
  waitpid(pid_, nullptr, 0);
}

}  // namespace sil
