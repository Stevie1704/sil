#include "manifest.hpp"

#include <fstream>
#include <sstream>

#include <nlohmann/json.hpp>

#include "sha256.hpp"

namespace sil {

using nlohmann::json;

namespace {

constexpr int kManifestVersion = 1;

const std::map<std::string, size_t> kFieldSizes = {
    {"u8", 1},  {"u16", 2}, {"u32", 4}, {"u64", 8}, {"i8", 1},
    {"i16", 2}, {"i32", 4}, {"i64", 8}, {"f32", 4}, {"f64", 8}};

[[noreturn]] void fail(const std::string &msg) {
  throw ManifestError("manifest error: " + msg);
}

const json &require(const json &obj, const char *key, const std::string &ctx) {
  auto it = obj.find(key);
  if (it == obj.end()) fail(ctx + ": missing required key '" + key + "'");
  return *it;
}

uint64_t positive_u64(const json &v, const std::string &ctx) {
  if (!v.is_number_unsigned() || v.get<uint64_t>() == 0)
    fail(ctx + ": must be a positive integer");
  return v.get<uint64_t>();
}

SchemaSpec parse_schema(const std::string &name, const json &js) {
  SchemaSpec spec;
  const json &fields = require(js, "fields", "schema '" + name + "'");
  if (!fields.is_array() || fields.empty())
    fail("schema '" + name + "': fields must be a non-empty array");
  for (const json &f : fields) {
    std::string fname = require(f, "name", "schema '" + name + "' field");
    std::string ftype = require(f, "type", "schema '" + name + "' field");
    auto it = kFieldSizes.find(ftype);
    if (it == kFieldSizes.end())
      fail("schema '" + name + "' field '" + fname + "': unknown type '" +
           ftype + "'");
    spec.fields.push_back({fname, ftype});
    spec.byte_size += it->second;
  }
  spec.canonical_json = js.dump();
  return spec;
}

ParticipantSpec parse_participant(const std::string &name, const json &js,
                                  const Manifest &m) {
  const std::string ctx = "participant '" + name + "'";
  auto check_channels = [&](const json &arr, const char *key) {
    std::vector<std::string> out;
    for (const json &ch : arr) {
      std::string cname = ch.get<std::string>();
      if (!m.find_channel(cname))
        fail(ctx + " " + key + " references unknown channel '" + cname + "'");
      out.push_back(cname);
    }
    return out;
  };

  ParticipantSpec p;
  p.name = name;
  std::string type = require(js, "type", ctx);
  if (type == "native") {
    NativeSpec n;
    n.library = require(js, "library", ctx).get<std::string>();
    n.config_json = js.value("config", json::object()).dump();
    p.impl = std::move(n);
  } else if (type == "process") {
    ProcessSpec ps;
    const json &cmd = require(js, "command", ctx);
    if (!cmd.is_array() || cmd.empty())
      fail(ctx + ": command must be a non-empty array");
    for (const json &c : cmd) ps.command.push_back(c.get<std::string>());
    ps.step_period_ns =
        positive_u64(require(js, "step_period_ns", ctx), ctx + " step_period_ns");
    ps.subscribes = check_channels(js.value("subscribes", json::array()), "subscribes");
    ps.publishes = check_channels(js.value("publishes", json::array()), "publishes");
    ps.priority = js.value("priority", 0);
    p.impl = std::move(ps);
  } else {
    fail(ctx + ": unknown type '" + type + "'");
  }
  return p;
}

}  // namespace

const ChannelSpec *Manifest::find_channel(const std::string &name) const {
  for (const ChannelSpec &c : channels)
    if (c.name == name) return &c;
  return nullptr;
}

Manifest load_manifest(const std::filesystem::path &path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) fail("cannot open manifest file: " + path.string());
  std::stringstream buf;
  buf << in.rdbuf();
  const std::string bytes = buf.str();

  json doc;
  try {
    doc = json::parse(bytes);
  } catch (const json::exception &e) {
    fail(std::string("invalid JSON: ") + e.what());
  }
  if (!doc.is_object()) fail("top level must be an object");

  const json &version = require(doc, "sil_manifest", "manifest");
  if (!version.is_number_integer() || version.get<int>() != kManifestVersion)
    fail("unsupported sil_manifest version " + version.dump() + ", expected " +
         std::to_string(kManifestVersion));

  Manifest m;
  m.hash_hex = sha256_hex(bytes);
  m.base_dir = std::filesystem::absolute(path).parent_path();
  m.duration_ns =
      positive_u64(require(doc, "duration_ns", "manifest"), "duration_ns");

  for (const auto &[name, js] : require(doc, "schemas", "manifest").items())
    m.schemas.emplace(name, parse_schema(name, js));

  // nlohmann objects iterate key-sorted: channel/participant order is
  // name-determined, never author-order, so hashes and IDs are stable.
  for (const auto &[name, js] : require(doc, "channels", "manifest").items()) {
    ChannelSpec c;
    c.name = name;
    c.schema = require(js, "schema", "channel '" + name + "'").get<std::string>();
    if (!m.schemas.count(c.schema))
      fail("channel '" + name + "' references unknown schema '" + c.schema + "'");
    if (js.contains("latency_ns")) {
      const json &lat = js["latency_ns"];
      if (!lat.is_number_unsigned())
        fail("channel '" + name + "': latency_ns must be a non-negative integer");
      c.latency_ns = lat.get<uint64_t>();
    }
    m.channels.push_back(std::move(c));
  }

  for (const auto &[name, js] : require(doc, "participants", "manifest").items())
    m.participants.push_back(parse_participant(name, js, m));

  return m;
}

}  // namespace sil
