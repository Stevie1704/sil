#include "interceptor.hpp"

#include <cmath>
#include <cstdint>
#include <cstring>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "manifest.hpp"

namespace {

using sil::FieldSpec;
using sil::InterceptorPlan;
using sil::InterceptorSpec;
using sil::SchemaSpec;

/** Fail the test process with a useful message when an invariant is false. */
void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

/** Build the minimal schema metadata consumed by the plan compiler. */
SchemaSpec schema(std::initializer_list<FieldSpec> fields) {
  SchemaSpec result;
  result.fields = fields;
  return result;
}

/** Compile declarations into the runtime seam under test. */
std::shared_ptr<InterceptorPlan> plan(
    uint64_t duration_ns, const SchemaSpec &schema,
    std::vector<InterceptorSpec> specs) {
  return sil::compile_interceptor_plan(duration_ns, schema, specs);
}

/** Build a bounded drop declaration for a test window. */
InterceptorSpec drop(uint64_t start_ns, uint64_t end_ns) {
  InterceptorSpec spec;
  spec.kind = "drop";
  spec.start_ns = start_ns;
  spec.end_ns = end_ns;
  return spec;
}

/** Build an open-ended delay declaration for a test. */
InterceptorSpec delay(uint64_t delay_ns) {
  InterceptorSpec spec;
  spec.kind = "delay";
  spec.delay_ns = delay_ns;
  return spec;
}

/** Build a bounded drop_nth declaration for a test window. */
InterceptorSpec drop_nth(uint64_t start_ns, uint64_t end_ns, uint64_t n) {
  InterceptorSpec spec;
  spec.kind = "drop_nth";
  spec.start_ns = start_ns;
  spec.end_ns = end_ns;
  spec.n = n;
  return spec;
}

/** Build an override declaration for one scalar schema field. */
InterceptorSpec override_field(const char *field, double value) {
  InterceptorSpec spec;
  spec.kind = "override";
  spec.field = field;
  spec.value = value;
  return spec;
}

/** Verify that interceptor windows include start and exclude end. */
void test_half_open_window_edges() {
  const SchemaSpec s = schema({{"value", "u8", 0}});
  auto p = plan(100, s, {drop(10, 20)});
  std::vector<uint8_t> bytes(1);

  check(!p->apply(9, bytes).suppressed, "window suppressed before start");
  check(p->apply(10, bytes).suppressed, "window did not include start");
  check(p->apply(19, bytes).suppressed, "window did not include end - 1");
  check(!p->apply(20, bytes).suppressed, "window included its exclusive end");
}

/** Verify that delay overflow clamps instead of wrapping into the run. */
void test_delay_saturates() {
  const SchemaSpec s = schema({{"value", "u8", 0}});
  auto p = plan(UINT64_MAX, s, {delay(UINT64_MAX - 5)});
  std::vector<uint8_t> bytes(1);

  // Wrapping would make 10 + (MAX - 5) equal 4 and incorrectly survive.
  const auto verdict = p->apply(10, bytes);
  check(verdict.suppressed, "overflowing delay wrapped into the run");
  check(verdict.visible_ns == UINT64_MAX,
        "overflowing delay did not clamp to UINT64_MAX");
}

/** Verify direct and delayed messages respect the duration boundary. */
void test_duration_is_half_open() {
  const SchemaSpec s = schema({{"value", "u8", 0}});
  auto p = plan(100, s, {});
  std::vector<uint8_t> bytes(1);

  check(!p->apply(99, bytes).suppressed, "last in-run timestamp was dropped");
  check(p->apply(100, bytes).suppressed,
        "duration boundary was not truncated");

  auto delayed = plan(100, s, {delay(10)});
  check(delayed->apply(90, bytes).suppressed,
        "message landing at duration was not truncated");
}

/** Verify all matching delays compose in declaration order. */
void test_delays_compose_in_declared_order() {
  const SchemaSpec s = schema({{"value", "u8", 0}});
  auto p = plan(100, s, {delay(7), delay(11)});
  std::vector<uint8_t> bytes(1);

  const auto verdict = p->apply(3, bytes);
  check(!verdict.suppressed, "composed delays unexpectedly suppressed");
  check(verdict.visible_ns == 21, "delays did not compose in declaration order");
}

/** Verify each drop_nth step owns its own in-window counter. */
void test_drop_nth_has_independent_window_state() {
  const SchemaSpec s = schema({{"value", "u8", 0}});
  auto p = plan(100, s,
                {drop_nth(0, 3, 2), drop_nth(3, 6, 2)});
  std::vector<uint8_t> bytes(1);

  // The plan receives publish time and bytes, never a channel sequence. Each
  // interceptor starts counting at its own first in-window message.
  const std::vector<bool> suppressed = {
      p->apply(0, bytes).suppressed, p->apply(1, bytes).suppressed,
      p->apply(2, bytes).suppressed, p->apply(3, bytes).suppressed,
      p->apply(4, bytes).suppressed, p->apply(5, bytes).suppressed};
  check(suppressed == std::vector<bool>({false, true, false, false, true, false}),
        "drop_nth counters were not independent per interceptor");
}

/** Write an integer bit pattern to a byte vector in little-endian order. */
void put_le(std::vector<uint8_t> &bytes, size_t offset, uint64_t value,
            size_t width) {
  for (size_t i = 0; i < width; i++)
    bytes[offset + i] = static_cast<uint8_t>(value >> (8 * i));
}

/** Add a float's bit pattern to the little-endian expected payload. */
void put_float(std::vector<uint8_t> &bytes, size_t offset, float value) {
  uint32_t bits = 0;
  std::memcpy(&bits, &value, sizeof(value));
  put_le(bytes, offset, bits, sizeof(bits));
}

/** Add a double's bit pattern to the little-endian expected payload. */
void put_double(std::vector<uint8_t> &bytes, size_t offset, double value) {
  uint64_t bits = 0;
  std::memcpy(&bits, &value, sizeof(value));
  put_le(bytes, offset, bits, sizeof(bits));
}

/** Verify load-time field offsets and encodings for every field type. */
void test_override_offsets_and_encodings() {
  const SchemaSpec s = schema({
      {"u8", "u8", 0},   {"u16", "u16", 0}, {"u32", "u32", 0},
      {"u64", "u64", 0}, {"i8", "i8", 0},   {"i16", "i16", 0},
      {"i32", "i32", 0}, {"i64", "i64", 0}, {"f32", "f32", 0},
      {"f64", "f64", 0}});
  auto p = plan(
      100, s,
      {override_field("u8", 0xab),
       override_field("u16", 0x1234),
       override_field("u32", 0x12345678),
       override_field("u64", static_cast<double>(0x01020304050608ULL)),
       override_field("i8", -2), override_field("i16", -0x1234),
       override_field("i32", -0x123456),
       override_field("i64", -static_cast<double>(0x0102030405ULL)),
       override_field("f32", 1.5), override_field("f64", -2.25)});
  std::vector<uint8_t> bytes(1 + 2 + 4 + 8 + 1 + 2 + 4 + 8 + 4 + 8, 0xa5);
  const auto verdict = p->apply(0, bytes);
  check(!verdict.suppressed, "override plan unexpectedly suppressed");

  std::vector<uint8_t> expected(bytes.size(), 0xa5);
  size_t offset = 0;
  put_le(expected, offset, 0xab, 1);
  offset += 1;
  put_le(expected, offset, 0x1234, 2);
  offset += 2;
  put_le(expected, offset, 0x12345678, 4);
  offset += 4;
  put_le(expected, offset, 0x01020304050608ULL, 8);
  offset += 8;
  put_le(expected, offset, static_cast<uint64_t>(-2LL), 1);
  offset += 1;
  put_le(expected, offset, static_cast<uint64_t>(-0x1234LL), 2);
  offset += 2;
  put_le(expected, offset, static_cast<uint64_t>(-0x123456LL), 4);
  offset += 4;
  put_le(expected, offset, static_cast<uint64_t>(-0x0102030405LL), 8);
  offset += 8;
  put_float(expected, offset, 1.5f);
  offset += 4;
  put_double(expected, offset, -2.25);
  check(bytes == expected, "override offset or little-endian encoding mismatch");
}

/** Verify rounded integer endpoints clamp before conversion. */
void test_override_integer_endpoint_clamping() {
  const SchemaSpec s = schema({{"u64", "u64", 0}, {"i64", "i64", 0}});
  auto p = plan(100, s,
                {override_field("u64", std::ldexp(1.0, 64)),
                 override_field("i64", std::ldexp(1.0, 63))});
  std::vector<uint8_t> bytes(16, 0xa5);
  check(!p->apply(0, bytes).suppressed,
        "endpoint-clamped override plan unexpectedly suppressed");

  std::vector<uint8_t> expected(16, 0xa5);
  put_le(expected, 0, std::numeric_limits<uint64_t>::max(), 8);
  put_le(expected, 8, static_cast<uint64_t>(std::numeric_limits<int64_t>::max()),
         8);
  check(bytes == expected, "integer endpoint was not clamped before encoding");
}

/** Verify malformed caller payloads fail before an override writes out of range. */
void test_override_rejects_short_payload() {
  const SchemaSpec s = schema({{"value", "u32", 0}});
  auto p = plan(100, s, {override_field("value", 7)});
  std::vector<uint8_t> bytes(2, 0xa5);
  const std::vector<uint8_t> original = bytes;
  bool threw = false;
  try {
    p->apply(0, bytes);
  } catch (const std::out_of_range &) {
    threw = true;
  }
  check(threw, "short payload did not reject an out-of-range override");
  check(bytes == original, "buffer was modified after out-of-range exception");
}

}  // namespace

int main() {
  try {
    test_half_open_window_edges();
    test_delay_saturates();
    test_duration_is_half_open();
    test_delays_compose_in_declared_order();
    test_drop_nth_has_independent_window_state();
    test_override_offsets_and_encodings();
    test_override_integer_endpoint_clamping();
    test_override_rejects_short_payload();
  } catch (const std::exception &e) {
    std::cerr << "interceptor test failed: " << e.what() << '\n';
    return 1;
  }
  return 0;
}
