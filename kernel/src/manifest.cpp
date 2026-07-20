#include "manifest.hpp"

#include <cmath>
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
    // An optional `count` makes the field a fixed-size array of its element
    // type; it must be a positive integer. Absent means a scalar.
    size_t count = 0;
    if (f.contains("count")) {
      const json &cv = f["count"];
      if (!cv.is_number_unsigned() || cv.get<uint64_t>() < 1)
        fail("schema '" + name + "' field '" + fname +
             "': count must be an integer >= 1");
      count = cv.get<size_t>();
    }
    spec.fields.push_back({fname, ftype, count});
    spec.byte_size += it->second * (count == 0 ? 1 : count);
  }
  spec.canonical_json = js.dump();
  return spec;
}

// Inclusive [min, max] for integer field types, expressed as doubles so a
// single range check covers both bounds. Float types are not listed and
// accept any numeric value. Mirrors _INT_RANGES in the Python builder.
const std::map<std::string, std::pair<double, double>> kIntRanges = {
    {"u8", {0.0, 255.0}},
    {"u16", {0.0, 65535.0}},
    {"u32", {0.0, 4294967295.0}},
    {"u64", {0.0, 18446744073709551615.0}},
    {"i8", {-128.0, 127.0}},
    {"i16", {-32768.0, 32767.0}},
    {"i32", {-2147483648.0, 2147483647.0}},
    {"i64", {-9223372036854775808.0, 9223372036854775807.0}},
};

void parse_interceptors(ChannelSpec &c, const json &arr, const SchemaSpec &schema) {
  if (!arr.is_array())
    fail("channel '" + c.name + "': interceptors must be an array");
  for (const json &js : arr) {
    const std::string ctx = "interceptor on '" + c.name + "'";
    InterceptorSpec spec;
    spec.kind = require(js, "kind", ctx).get<std::string>();
    if (spec.kind != "drop" && spec.kind != "drop_nth" &&
        spec.kind != "delay" && spec.kind != "override")
      fail(ctx + ": unknown kind '" + spec.kind + "'");

    if (js.contains("start_ns")) {
      const json &v = js["start_ns"];
      if (!v.is_number_unsigned())
        fail(ctx + ": start_ns must be a non-negative integer");
      spec.start_ns = v.get<uint64_t>();
    }
    if (js.contains("end_ns")) {
      const json &v = js["end_ns"];
      if (!v.is_number_unsigned())
        fail(ctx + ": end_ns must be a non-negative integer");
      if (v.get<uint64_t>() <= spec.start_ns)
        fail(ctx + ": window end_ns must be greater than start_ns");
      spec.end_ns = v.get<uint64_t>();
    }

    if (spec.kind == "delay") {
      const json &v = require(js, "delay_ns", ctx + " (delay)");
      if (!v.is_number_unsigned())
        fail(ctx + ": delay_ns must be a non-negative integer");
      spec.delay_ns = v.get<uint64_t>();
    } else if (spec.kind == "drop_nth") {
      const json &v = require(js, "n", ctx + " (drop_nth)");
      if (!v.is_number_unsigned() || v.get<uint64_t>() < 1)
        fail(ctx + ": n must be an integer >= 1");
      spec.n = v.get<uint64_t>();
    } else if (spec.kind == "override") {
      spec.field = require(js, "field", ctx + " (override)").get<std::string>();
      const FieldSpec *fs = nullptr;
      for (const FieldSpec &f : schema.fields)
        if (f.name == spec.field) fs = &f;
      if (!fs)
        fail(ctx + ": override field '" + spec.field +
             "' is not in the channel's schema");
      if (fs->count != 0)
        fail(ctx + ": override field '" + spec.field +
             "' is a fixed-size array; only scalar fields can be overridden");
      const json &v = require(js, "value", ctx + " (override)");
      if (!v.is_number())
        fail(ctx + ": override value for '" + spec.field + "' must be a number");
      spec.value = v.get<double>();
      auto rit = kIntRanges.find(fs->type);
      if (rit != kIntRanges.end() &&
          (spec.value < rit->second.first || spec.value > rit->second.second ||
           spec.value != std::floor(spec.value)))
        fail(ctx + ": override value is unrepresentable in '" + spec.field +
             "' (" + fs->type + ")");
    }
    c.interceptors.push_back(std::move(spec));
  }
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
  // The clock shim is a process-participant-only opt-in; on any other type it
  // is a config error (the Python builder cannot even express it there).
  if (type != "process" && js.contains("shim"))
    fail(ctx + ": shim is only valid on process participants");
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
    if (js.contains("shim")) {
      const json &v = js["shim"];
      if (!v.is_boolean()) fail(ctx + ": shim must be a boolean");
      ps.shim = v.get<bool>();
    }
    p.impl = std::move(ps);
  } else if (type == "replay") {
    ReplaySpec rs;
    rs.recording = require(js, "recording", ctx).get<std::string>();
    rs.recording_hash = require(js, "recording_hash", ctx).get<std::string>();
    const json &chans = require(js, "channels", ctx);
    if (!chans.is_array() || chans.empty())
      fail(ctx + ": channels must be a non-empty array");
    rs.channels = check_channels(chans, "channels");
    p.impl = std::move(rs);
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

  // Optional realtime epoch for shimmed participants; absent means 0. A JSON
  // negative is not is_number_unsigned(), so this rejects negative epochs.
  if (doc.contains("epoch_ns")) {
    const json &e = doc["epoch_ns"];
    if (!e.is_number_unsigned())
      fail("epoch_ns must be a non-negative integer");
    m.epoch_ns = e.get<uint64_t>();
  }

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
    // Inline is the default (and omitted from the canonical doc). Only "shm"
    // is otherwise valid; anything else is a config error before setup.
    if (js.contains("transport")) {
      const json &t = js["transport"];
      if (!t.is_string())
        fail("channel '" + name + "': transport must be a string");
      const std::string tv = t.get<std::string>();
      if (tv == "inline")
        c.transport = Transport::Inline;
      else if (tv == "shm")
        c.transport = Transport::Shm;
      else
        fail("channel '" + name + "': unknown transport '" + tv +
             "' (expected 'inline' or 'shm')");
    }
    if (js.contains("interceptors"))
      parse_interceptors(c, js["interceptors"], m.schemas.at(c.schema));
    m.channels.push_back(std::move(c));
  }

  for (const auto &[name, js] : require(doc, "participants", "manifest").items())
    m.participants.push_back(parse_participant(name, js, m));

  return m;
}

}  // namespace sil
