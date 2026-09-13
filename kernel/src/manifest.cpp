#include "manifest.hpp"

#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <initializer_list>
#include <limits>
#include <map>
#include <stdexcept>
#include <sstream>
#include <set>
#include <type_traits>
#include <utility>

#include <nlohmann/json.hpp>

#include "interceptor.hpp"
#include "sha256.hpp"

namespace sil {

using nlohmann::json;

namespace {

constexpr int kManifestVersion = 1;

struct FieldLayout {
  size_t size;
  enum class Representation { Unsigned, Signed, Float } representation;
};

// The loader owns the schema type table. It supplies both schema widths and
// the representation needed to pre-encode override constants into a plan.
const std::map<std::string, FieldLayout> kFieldLayouts = {
    {"u8", {1, FieldLayout::Representation::Unsigned}},
    {"u16", {2, FieldLayout::Representation::Unsigned}},
    {"u32", {4, FieldLayout::Representation::Unsigned}},
    {"u64", {8, FieldLayout::Representation::Unsigned}},
    {"i8", {1, FieldLayout::Representation::Signed}},
    {"i16", {2, FieldLayout::Representation::Signed}},
    {"i32", {4, FieldLayout::Representation::Signed}},
    {"i64", {8, FieldLayout::Representation::Signed}},
    {"f32", {4, FieldLayout::Representation::Float}},
    {"f64", {8, FieldLayout::Representation::Float}}};

[[noreturn]] void fail(const std::string &msg) {
  throw ManifestError("manifest error: " + msg);
}

std::string describe_json(const json &value) {
  return std::string(value.type_name()) + " " + value.dump();
}

[[noreturn]] void type_error(const json &value, const std::string &ctx,
                             const std::string &expected) {
  fail(ctx + ": expected " + expected + ", got " + describe_json(value));
}

const json &require_object(const json &value, const std::string &ctx) {
  if (!value.is_object()) type_error(value, ctx, "an object");
  return value;
}

const json &require_array(const json &value, const std::string &ctx) {
  if (!value.is_array()) type_error(value, ctx, "an array");
  return value;
}

const json &require_value(const json &value, const char *key,
                          const std::string &ctx) {
  const json &object = require_object(value, ctx);
  auto it = object.find(key);
  if (it == object.end()) fail(ctx + ": missing required key '" + key + "'");
  return it.value();
}

const json *find_value(const json &value, const char *key,
                       const std::string &ctx) {
  const json &object = require_object(value, ctx);
  auto it = object.find(key);
  return it == object.end() ? nullptr : &it.value();
}

void reject_unknown_keys(const json &value, const std::string &ctx,
                         std::initializer_list<const char *> allowed) {
  const json &object = require_object(value, ctx);
  for (auto it = object.begin(); it != object.end(); ++it) {
    bool known = false;
    for (const char *key : allowed)
      if (it.key() == key) known = true;
    if (!known)
      fail(ctx + ": unknown key '" + it.key() + "' (value " +
           describe_json(it.value()) + ")");
  }
}

template <typename T>
std::string integer_range() {
  if constexpr (std::is_unsigned_v<T>) {
    return "[0, " +
           std::to_string(static_cast<unsigned long long>(
               std::numeric_limits<T>::max())) +
           "]";
  } else {
    return "[" +
           std::to_string(static_cast<long long>(
               std::numeric_limits<T>::min())) +
           ", " +
           std::to_string(static_cast<long long>(
               std::numeric_limits<T>::max())) +
           "]";
  }
}

template <typename T>
T extract_integer(const json &value, const std::string &ctx) {
  const std::string expected =
      (std::is_unsigned_v<T> ? "a non-negative integer in " : "an integer in ") +
      integer_range<T>();
  if (!value.is_number_integer()) type_error(value, ctx, expected);

  if constexpr (std::is_unsigned_v<T>) {
    uint64_t raw = 0;
    if (value.is_number_unsigned()) {
      raw = value.get<uint64_t>();
    } else {
      const int64_t signed_raw = value.get<int64_t>();
      if (signed_raw < 0) type_error(value, ctx, expected);
      raw = static_cast<uint64_t>(signed_raw);
    }
    if (raw > static_cast<uint64_t>(std::numeric_limits<T>::max()))
      type_error(value, ctx, expected);
    return static_cast<T>(raw);
  } else {
    if (value.is_number_unsigned()) {
      const uint64_t raw = value.get<uint64_t>();
      if (raw > static_cast<uint64_t>(std::numeric_limits<T>::max()))
        type_error(value, ctx, expected);
      return static_cast<T>(raw);
    }
    const int64_t raw = value.get<int64_t>();
    if (raw < static_cast<int64_t>(std::numeric_limits<T>::min()) ||
        raw > static_cast<int64_t>(std::numeric_limits<T>::max()))
      type_error(value, ctx, expected);
    return static_cast<T>(raw);
  }
}

template <typename T>
T extract_number(const json &value, const std::string &ctx) {
  if (!value.is_number()) type_error(value, ctx, "a number");

  double result = 0.0;
  if (value.is_number_unsigned())
    result = static_cast<double>(value.get<uint64_t>());
  else if (value.is_number_integer())
    result = static_cast<double>(value.get<int64_t>());
  else
    result = value.get<double>();
  if (!std::isfinite(result))
    type_error(value, ctx, "a finite number");
  return static_cast<T>(result);
}

template <typename T>
T extract(const json &value, const std::string &ctx) {
  if constexpr (std::is_same_v<T, std::string>) {
    if (!value.is_string()) type_error(value, ctx, "a string");
    return value.get<std::string>();
  } else if constexpr (std::is_same_v<T, bool>) {
    if (!value.is_boolean()) type_error(value, ctx, "a boolean");
    return value.get<bool>();
  } else if constexpr (std::is_integral_v<T>) {
    return extract_integer<T>(value, ctx);
  } else if constexpr (std::is_floating_point_v<T>) {
    return extract_number<T>(value, ctx);
  } else {
    static_assert(std::is_same_v<T, void>, "unsupported manifest JSON type");
  }
}

template <typename T>
T required(const json &object, const char *key, const std::string &ctx) {
  return extract<T>(require_value(object, key, ctx),
                    ctx + " key '" + key + "'");
}

uint64_t positive_u64(const json &value, const std::string &ctx) {
  const uint64_t result = extract<uint64_t>(value, ctx);
  if (result == 0) type_error(value, ctx, "a positive integer");
  return result;
}

SchemaSpec parse_schema(const std::string &name, const json &js) {
  const std::string ctx = "schema '" + name + "'";
  reject_unknown_keys(js, ctx, {"fields"});
  SchemaSpec spec;
  const json &fields = require_array(
      require_value(js, "fields", ctx), ctx + " key 'fields'");
  if (fields.empty()) fail(ctx + ": fields must be a non-empty array");
  std::set<std::string> field_names;
  for (size_t index = 0; index < fields.size(); index++) {
    const json &f = fields.at(index);
    const std::string field_ctx =
        ctx + " field[" + std::to_string(index) + "]";
    reject_unknown_keys(f, field_ctx, {"name", "type", "count"});
    std::string fname = required<std::string>(f, "name", field_ctx);
    std::string ftype = required<std::string>(f, "type", field_ctx);
    auto it = kFieldLayouts.find(ftype);
    if (it == kFieldLayouts.end())
      fail(ctx + " field '" + fname + "': unknown type '" + ftype + "'");
    // An optional `count` makes the field a fixed-size array of its element
    // type; it must be a positive integer. Absent means a scalar.
    size_t count = 0;
    if (const json *cv = find_value(f, "count", field_ctx)) {
      count = extract<size_t>(*cv, field_ctx + " key 'count'");
      if (count < 1)
        type_error(*cv, field_ctx + " key 'count'", "an integer >= 1");
    }
    if (!field_names.insert(fname).second)
      fail(ctx + " field '" + fname + "': duplicate field name");
    const size_t elements = count == 0 ? 1 : count;
    if (elements > std::numeric_limits<size_t>::max() / it->second.size)
      fail(ctx + " field '" + fname + "': byte size overflows size_t for " +
           "count " + std::to_string(count));
    const size_t field_size = it->second.size * elements;
    if (spec.byte_size > std::numeric_limits<size_t>::max() - field_size)
      fail(ctx + ": total byte size overflows size_t at field '" + fname +
           "'");
    spec.fields.push_back({fname, ftype, count});
    spec.byte_size += field_size;
  }
  spec.canonical_json = js.dump();
  return spec;
}

/** Encode one validated override constant as the schema's little-endian bytes. */
std::vector<uint8_t> encode_override(
    const FieldLayout &layout, const InterceptorSpec::OverrideValue &value) {
  uint64_t bits = 0;
  switch (layout.representation) {
    case FieldLayout::Representation::Unsigned:
      bits = std::get<uint64_t>(value);
      break;
    case FieldLayout::Representation::Signed:
      bits = static_cast<uint64_t>(std::get<int64_t>(value));
      break;
    case FieldLayout::Representation::Float: {
      const double number = std::get<double>(value);
      if (layout.size == 4) {
        const float rounded = static_cast<float>(number);
        uint32_t float_bits = 0;
        std::memcpy(&float_bits, &rounded, sizeof(float_bits));
        bits = float_bits;
      } else {
        std::memcpy(&bits, &number, sizeof(number));
      }
      break;
    }
  }

  std::vector<uint8_t> bytes(layout.size);
  for (size_t i = 0; i < layout.size; i++)
    bytes[i] = static_cast<uint8_t>(bits >> (8 * i));
  return bytes;
}

InterceptorSpec::OverrideValue extract_override_value(
    const json &value, const std::string &ctx, const std::string &type) {
  if (type == "u8") {
    return static_cast<uint64_t>(extract<uint8_t>(value, ctx));
  } else if (type == "u16") {
    return static_cast<uint64_t>(extract<uint16_t>(value, ctx));
  } else if (type == "u32") {
    return static_cast<uint64_t>(extract<uint32_t>(value, ctx));
  } else if (type == "u64") {
    return extract<uint64_t>(value, ctx);
  } else if (type == "i8") {
    return static_cast<int64_t>(extract<int8_t>(value, ctx));
  } else if (type == "i16") {
    return static_cast<int64_t>(extract<int16_t>(value, ctx));
  } else if (type == "i32") {
    return static_cast<int64_t>(extract<int32_t>(value, ctx));
  } else if (type == "i64") {
    return extract<int64_t>(value, ctx);
  }

  const double result = extract<double>(value, ctx);
  if (type == "f32" &&
      std::abs(result) > static_cast<double>(std::numeric_limits<float>::max()))
    type_error(value, ctx, "a number representable as f32");
  return result;
}

void parse_interceptors(ChannelSpec &c, const json &arr,
                        const SchemaSpec &schema) {
  const json &interceptors = require_array(
      arr, "channel '" + c.name + "' key 'interceptors'");
  for (size_t index = 0; index < interceptors.size(); index++) {
    const json &js = interceptors.at(index);
    const std::string ctx = "channel '" + c.name + "' interceptor[" +
                            std::to_string(index) + "]";
    reject_unknown_keys(js, ctx,
                        {"kind", "start_ns", "end_ns", "delay_ns", "n",
                         "field", "value"});
    InterceptorSpec spec;
    spec.kind = required<std::string>(js, "kind", ctx);
    if (spec.kind != "drop" && spec.kind != "drop_nth" &&
        spec.kind != "delay" && spec.kind != "override")
      fail(ctx + ": unknown kind '" + spec.kind + "'");

    if (const json *v = find_value(js, "start_ns", ctx))
      spec.start_ns = extract<uint64_t>(*v, ctx + " key 'start_ns'");
    if (const json *v = find_value(js, "end_ns", ctx)) {
      const uint64_t end = extract<uint64_t>(*v, ctx + " key 'end_ns'");
      if (end <= spec.start_ns)
        fail(ctx + ": window end_ns must be greater than start_ns");
      spec.end_ns = end;
    }

    if (spec.kind == "delay") {
      spec.delay_ns = required<uint64_t>(js, "delay_ns", ctx);
    } else if (spec.kind == "drop_nth") {
      const json &v = require_value(js, "n", ctx);
      const uint64_t n = extract<uint64_t>(v, ctx + " key 'n'");
      if (n < 1) type_error(v, ctx + " key 'n'", "an integer >= 1");
      spec.n = n;
    } else if (spec.kind == "override") {
      spec.field = required<std::string>(js, "field", ctx);
      const FieldSpec *fs = nullptr;
      for (const FieldSpec &f : schema.fields)
        if (f.name == spec.field) fs = &f;
      if (!fs)
        fail(ctx + ": override field '" + spec.field +
             "' is not in the channel's schema");
      if (fs->count != 0)
        fail(ctx + ": override field '" + spec.field +
             "' is a fixed-size array; only scalar fields can be overridden");
      const json &v = require_value(js, "value", ctx);
      const std::string value_ctx = ctx + " key 'value' for field '" +
                                    spec.field + "'";
      spec.value = extract_override_value(v, value_ctx, fs->type);
    }
    c.interceptors.push_back(std::move(spec));
  }
}

ParticipantSpec parse_participant(const std::string &name, const json &js,
                                  const Manifest &m) {
  const std::string ctx = "participant '" + name + "'";
  const json &participant = require_object(js, ctx);
  const auto check_channels = [&](const json &value, const char *key) {
    const json &arr = require_array(value, ctx + " key '" + key + "'");
    std::vector<std::string> out;
    std::set<std::string> seen;
    for (size_t index = 0; index < arr.size(); index++) {
      const json &ch = arr.at(index);
      const std::string item_ctx = ctx + " key '" + key + "'[" +
                                   std::to_string(index) + "]";
      std::string cname = extract<std::string>(ch, item_ctx);
      if (!m.find_channel(cname))
        fail(ctx + " " + key + " references unknown channel '" + cname + "'");
      // A repeated Channel would silently double a participant's delivery or
      // production, so it is a declaration defect wherever it appears.
      if (!seen.insert(cname).second)
        fail(ctx + " " + key + " lists channel '" + cname + "' twice");
      out.push_back(cname);
    }
    return out;
  };
  const auto check_subscriber_routes = [&](const json &value) {
    const char *key = "subscribes";
    const json &arr = require_array(value, ctx + " key 'subscribes'");
    std::vector<SubscriberRouteSpec> out;
    std::set<std::string> seen;
    for (size_t index = 0; index < arr.size(); index++) {
      const json &entry = arr.at(index);
      const std::string item_ctx = ctx + " key 'subscribes'[" +
                                   std::to_string(index) + "]";
      SubscriberRouteSpec route;
      if (entry.is_string()) {
        // Compatibility shape: pre-#75 manifests named only the Channel and
        // therefore retain their unbounded route behavior. An object with
        // neither policy field is the equivalent explicit shape below.
        route.channel = extract<std::string>(entry, item_ctx);
      } else {
        const json &object = require_object(entry, item_ctx);
        reject_unknown_keys(object, item_ctx,
                            {"channel", "capacity", "overflow"});
        route.channel = required<std::string>(object, "channel", item_ctx);
        const json *capacity = find_value(object, "capacity", item_ctx);
        const json *overflow_value = find_value(object, "overflow", item_ctx);
        if (bool(capacity) != bool(overflow_value))
          fail(item_ctx +
               ": 'capacity' and 'overflow' must either both be present or "
               "both be absent for an unbounded route");
        if (capacity) {
          route.capacity =
              extract<size_t>(*capacity, item_ctx + " key 'capacity'");
          if (*route.capacity == 0)
            type_error(*capacity, item_ctx + " key 'capacity'",
                       "a positive integer");
          const std::string overflow = extract<std::string>(
              *overflow_value, item_ctx + " key 'overflow'");
          if (overflow == "fail") {
            route.overflow = OverflowPolicy::Fail;
          } else if (overflow == "drop_newest") {
            route.overflow = OverflowPolicy::DropNewest;
          } else if (overflow == "blocking") {
            fail(item_ctx +
                 ": blocking overflow is unsupported because the sequential "
                 "scheduler cannot activate the consumer while the publisher "
                 "is blocked");
          } else {
            fail(item_ctx + ": unknown overflow policy '" + overflow +
                 "' (expected 'fail' or 'drop_newest')");
          }
        }
      }
      if (!m.find_channel(route.channel))
        fail(ctx + " " + key + " references unknown channel '" +
             route.channel + "'");
      if (!seen.insert(route.channel).second)
        fail(ctx + " " + key + " lists channel '" + route.channel +
             "' twice");
      out.push_back(std::move(route));
    }
    return out;
  };

  ParticipantSpec p;
  p.name = name;
  std::string type = required<std::string>(participant, "type", ctx);
  // The clock shim is a process-participant-only opt-in; on any other type it
  // is a config error (the Python builder cannot even express it there). The
  // sleep policy (#52) is part of that same opt-in and follows it.
  if (type != "process" && find_value(participant, "shim", ctx))
    fail(ctx + ": shim is only valid on process participants");
  if (type != "process" && find_value(participant, "sleep", ctx))
    fail(ctx + ": sleep is only valid on process participants");
  if (type == "native") {
    reject_unknown_keys(participant, ctx,
                        {"type", "library", "config", "subscribes",
                         "publishes", "shim"});
    NativeSpec n;
    n.library = required<std::string>(participant, "library", ctx);
    // Native contract enforcement replaced the pre-#49 behavior, so an
    // absent list cannot default without changing the Run semantics of an
    // existing Manifest hash. Process participants remain tolerant below
    // because absent-means-empty was their prior behavior.
    n.subscribes = check_subscriber_routes(
        require_value(participant, "subscribes", ctx));
    n.publishes = check_channels(
        require_value(participant, "publishes", ctx), "publishes");
    // Native config is the explicit extension point for participant-specific
    // options. Its keys are intentionally not closed by the Manifest format;
    // the container itself is still validated so malformed JSON cannot escape.
    if (const json *config = find_value(participant, "config", ctx)) {
      n.config_json = require_object(*config, ctx + " key 'config'").dump();
    } else {
      n.config_json = json::object().dump();
    }
    p.impl = std::move(n);
  } else if (type == "process") {
    reject_unknown_keys(participant, ctx,
                        {"type", "command", "step_period_ns", "subscribes",
                         "publishes", "priority", "shim", "sleep"});
    ProcessSpec ps;
    const json &cmd = require_array(
        require_value(participant, "command", ctx),
        ctx + " key 'command'");
    if (cmd.empty())
      fail(ctx + ": command must be a non-empty array");
    for (size_t index = 0; index < cmd.size(); index++)
      ps.command.push_back(extract<std::string>(
          cmd.at(index), ctx + " key 'command'[" + std::to_string(index) + "]"));
    ps.step_period_ns = positive_u64(
        require_value(participant, "step_period_ns", ctx),
        ctx + " key 'step_period_ns'");
    // Process participants predate contract enforcement and absent lists
    // therefore retain their established empty-contract behavior.
    if (const json *subscribes = find_value(participant, "subscribes", ctx))
      ps.subscribes = check_subscriber_routes(*subscribes);
    if (const json *publishes = find_value(participant, "publishes", ctx))
      ps.publishes = check_channels(*publishes, "publishes");
    if (const json *priority = find_value(participant, "priority", ctx))
      ps.priority = extract<int32_t>(*priority, ctx + " key 'priority'");
    if (const json *shim = find_value(participant, "shim", ctx))
      ps.shim = extract<bool>(*shim, ctx + " key 'shim'");
    // Sleep policy for the shimmed clock (#52). Absent means "immediate",
    // which is the behavior every pre-#52 Manifest already has, so those keep
    // both their hash and their semantics. The Python builder always emits it
    // and defaults to "reject", so a newly authored Manifest cannot land on
    // the compatibility behavior by accident.
    if (const json *sleep = find_value(participant, "sleep", ctx)) {
      // The policy only exists inside the shim. Silently ignoring it on an
      // unshimmed participant would let a Manifest declare a rejection that
      // never happens.
      if (!ps.shim)
        fail(ctx + ": sleep requires shim (the policy only applies to the "
                   "virtual clock shim)");
      const std::string value = extract<std::string>(*sleep, ctx + " key 'sleep'");
      if (value == "immediate")
        ps.sleep = SleepPolicy::Immediate;
      else if (value == "reject")
        ps.sleep = SleepPolicy::Reject;
      else
        fail(ctx + ": unknown sleep policy '" + value +
             "' (expected 'reject' or 'immediate')");
    }
    p.impl = std::move(ps);
  } else if (type == "replay") {
    reject_unknown_keys(participant, ctx,
                        {"type", "recording", "recording_hash", "channels",
                         "shim"});
    ReplaySpec rs;
    rs.recording = required<std::string>(participant, "recording", ctx);
    rs.recording_hash =
        required<std::string>(participant, "recording_hash", ctx);
    const json &chans = require_array(
        require_value(participant, "channels", ctx),
        ctx + " key 'channels'");
    if (chans.empty())
      fail(ctx + ": channels must be a non-empty array");
    rs.channels = check_channels(chans, "channels");
    p.impl = std::move(rs);
  } else {
    fail(ctx + ": unknown type '" + type + "'");
  }
  return p;
}

}  // namespace

std::shared_ptr<InterceptorPlan> compile_interceptor_plan(
    uint64_t duration_ns, const SchemaSpec &schema,
    const std::vector<InterceptorSpec> &specs) {
  const auto compile_kind = [](const std::string &kind) {
    if (kind == "drop") return InterceptorPlan::Kind::Drop;
    if (kind == "drop_nth") return InterceptorPlan::Kind::DropNth;
    if (kind == "delay") return InterceptorPlan::Kind::Delay;
    if (kind == "override") return InterceptorPlan::Kind::Override;
    throw std::logic_error("invalid interceptor kind during compilation");
  };

  std::vector<InterceptorPlan::Step> steps;
  steps.reserve(specs.size());

  for (const InterceptorSpec &spec : specs) {
    InterceptorPlan::Step step{
        .kind = compile_kind(spec.kind),
        .start_ns = spec.start_ns,
        .end_ns = spec.end_ns.value_or(0),
        .has_end = spec.end_ns.has_value(),
        .parameter = 0,
        .window_count = 0,
        .override_offset = 0,
        .override_bytes = {}};

    if (step.kind == InterceptorPlan::Kind::Delay) {
      if (!spec.delay_ns)
        throw std::logic_error("delay interceptor missing delay_ns");
      step.parameter = *spec.delay_ns;
    } else if (step.kind == InterceptorPlan::Kind::DropNth) {
      if (!spec.n || *spec.n < 1)
        throw std::logic_error("drop_nth interceptor n must be positive");
      step.parameter = *spec.n;
    } else if (step.kind == InterceptorPlan::Kind::Override) {
      size_t offset = 0;
      const FieldSpec *field = nullptr;
      FieldLayout layout{};
      for (const FieldSpec &candidate : schema.fields) {
        auto type = kFieldLayouts.find(candidate.type);
        if (type == kFieldLayouts.end())
          throw std::logic_error("unknown field type during compilation");
        if (candidate.name == spec.field) {
          field = &candidate;
          layout = type->second;
          break;
        }
        offset += type->second.size *
                  (candidate.count == 0 ? 1 : candidate.count);
      }
      if (!field)
        throw std::logic_error("override field missing during compilation");
      if (field->count != 0)
        throw std::logic_error("array override during compilation");
      step.override_offset = offset;
      step.override_bytes = encode_override(layout, spec.value);
    }
    steps.push_back(std::move(step));
  }

  return std::shared_ptr<InterceptorPlan>(
      new InterceptorPlan(duration_ns, std::move(steps)));
}

const ChannelSpec *Manifest::find_channel(const std::string &name) const {
  for (const ChannelSpec &c : channels)
    if (c.name == name) return &c;
  return nullptr;
}

Manifest load_manifest(const std::filesystem::path &path) {
  try {
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
    require_object(doc, "manifest");
    reject_unknown_keys(doc, "manifest",
                        {"sil_manifest", "duration_ns", "epoch_ns", "schemas",
                         "channels", "participants"});

    const int version = required<int>(doc, "sil_manifest", "manifest");
    if (version != kManifestVersion)
      fail("unsupported sil_manifest version " + std::to_string(version) +
           ", expected " + std::to_string(kManifestVersion));

    Manifest m;
    m.hash_hex = sha256_hex(bytes);
    m.base_dir = std::filesystem::absolute(path).parent_path();
    m.duration_ns = positive_u64(
        require_value(doc, "duration_ns", "manifest"),
        "manifest key 'duration_ns'");

    // Optional realtime epoch for shimmed participants; absent means 0.
    if (const json *epoch = find_value(doc, "epoch_ns", "manifest"))
      m.epoch_ns = extract<uint64_t>(*epoch, "manifest key 'epoch_ns'");

    const json &schemas = require_object(
        require_value(doc, "schemas", "manifest"),
        "manifest key 'schemas'");
    for (auto it = schemas.begin(); it != schemas.end(); ++it)
      m.schemas.emplace(it.key(), parse_schema(it.key(), it.value()));

    // nlohmann objects iterate key-sorted: channel/participant order is
    // name-determined, never author-order, so hashes and IDs are stable.
    const json &channels = require_object(
        require_value(doc, "channels", "manifest"),
        "manifest key 'channels'");
    for (auto it = channels.begin(); it != channels.end(); ++it) {
      const std::string &name = it.key();
      const json &js = it.value();
      const std::string ctx = "channel '" + name + "'";
      reject_unknown_keys(js, ctx,
                          {"schema", "latency_ns", "transport", "slots",
                           "interceptors"});
      ChannelSpec c;
      c.name = name;
      c.schema = required<std::string>(js, "schema", ctx);
      auto schema_it = m.schemas.find(c.schema);
      if (schema_it == m.schemas.end())
        fail(ctx + " references unknown schema '" + c.schema + "'");
      if (const json *latency = find_value(js, "latency_ns", ctx))
        c.latency_ns = extract<uint64_t>(*latency, ctx + " key 'latency_ns'");

      // Inline is the default (and omitted from the canonical doc). Only
      // "shm" is otherwise valid; anything else is a config error before setup.
      if (const json *transport = find_value(js, "transport", ctx)) {
        const std::string transport_value =
            extract<std::string>(*transport, ctx + " key 'transport'");
        if (transport_value == "inline")
          c.transport = Transport::Inline;
        else if (transport_value == "shm")
          c.transport = Transport::Shm;
        else
          fail(ctx + ": unknown transport '" + transport_value +
               "' (expected 'inline' or 'shm')");
      }
      if (const json *slots = find_value(js, "slots", ctx)) {
        if (c.transport != Transport::Shm)
          fail(ctx + ": slots is only valid with shm transport");
        c.slots = extract<size_t>(*slots, ctx + " key 'slots'");
        if (c.slots == 0)
          type_error(*slots, ctx + " key 'slots'", "a positive integer");
      }
      if (schema_it->second.byte_size >
          std::numeric_limits<size_t>::max() / c.slots)
        fail(ctx + ": arena byte size overflows size_t for slots " +
             std::to_string(c.slots));
      if (const json *interceptors = find_value(js, "interceptors", ctx))
        parse_interceptors(c, *interceptors, schema_it->second);
      c.interceptor_plan = compile_interceptor_plan(
          m.duration_ns, schema_it->second, c.interceptors);
      m.channels.push_back(std::move(c));
    }

    const json &participants = require_object(
        require_value(doc, "participants", "manifest"),
        "manifest key 'participants'");
    for (auto it = participants.begin(); it != participants.end(); ++it)
      m.participants.push_back(parse_participant(it.key(), it.value(), m));

    return m;
  } catch (const json::exception &e) {
    // Every conversion in this seam should already carry its field context.
    // Keep this guard for future nlohmann operations added to the loader.
    throw ManifestError("manifest error: JSON validation failed while loading '" +
                        path.string() + "': " + e.what());
  }
}

}  // namespace sil
