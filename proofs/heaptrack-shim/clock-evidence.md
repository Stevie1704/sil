# Measured timestamp backend and origin calibration

`clock_probe.py OUTPUT --build BUILD` independently measures the installed
Heaptrack/Clock shim combination. It produces a paired unshimmed/shimmed
calibration; it does not retrofit clock measurements into older retained Runs.
The CI artifact includes the calibration alongside the Run matrix.

The C fixture has no `clock_gettime` calls of its own. It advances a private
Clock region through 0, 100, 200, 400 and 900 million nanoseconds, with 20 ms
host delays implemented by `SYS_nanosleep`. Both fixture and audit library are
built with `-O2 -g`. The audit library precedes Heaptrack and the Clock shim in
`LD_PRELOAD`; it delegates to the next `clock_gettime` and brackets its return
with direct `SYS_clock_gettime(CLOCK_MONOTONIC)` calls. Thus the bracket clock
cannot be intercepted by the Clock shim. No production code or shim hook is
added.

`audit.bin` retains each changed return value per thread, capped at 1,024 fixed
64-byte little-endian records: sequence, thread ID, clock ID, return address,
returned nanoseconds, host bracket start/end, and status. Identical repeated
reads are suppressed, so a timer flood cannot fill the audit by repeatedly
reading one frozen Virtual time. `maps.txt` and `caller-symbols.json` resolve
the observed return addresses into installed library symbols. The raw
Heaptrack capture is separately capped at 32 MiB and five host seconds;
exceeding either bound leaves the calibration incomplete.

`analysis.json` separates observations from inference:

- The backend is identified only when the observed callers resolve into
  libstdc++'s `steady_clock::now` and the actual clock ID is `CLOCK_MONOTONIC`.
  Return values within the syscall brackets are host-domain observations;
  exact controlled region values outside those brackets are virtual-domain
  observations.
- The cached origin is private Heaptrack state. Its domain is inferred by
  trying observed clock values as origin candidates and reproducing **every**
  distinct raw `c` timestamp using signed nanosecond subtraction, millisecond
  truncation toward zero, and unsigned 64-bit conversion. Candidate values
  are retained. If no candidate fits, or candidates span domains, the origin
  remains `unknown`.
- This validates the timestamp arithmetic for this controlled calibration.
  It does not establish a continuous host-time mapping for the Virtual time
  of a Run, nor prove that a different initialization order has the same
  origin. The top-level `timestamp_clock_established_in_calibration` flag
  requires successful bounded captures, a known backend and origin domain in
  both captures, and observation of all five controlled virtual values.

## Observed development smoke

The ARM64 development smoke used Debian's Heaptrack 1.4.0 with GCC 12 and the
existing Linux Clock shim build. Both captures' caller addresses resolved to
`std::chrono::_V2::steady_clock::now()`, passing `CLOCK_MONOTONIC`.

In the shimmed calibration the first read returned host monotonic time inside
its direct syscall brackets; later reads returned exactly the five controlled
Virtual times. A single origin candidate, that first host value, reproduced
all 39,222 raw `c` records. Their five distinct unsigned millisecond values
were near `UINT64_MAX`, consistent with subtracting the host origin from the
much smaller virtual endpoints. The paired unshimmed capture had ten normal
elapsed timestamps, also explained by its first host-domain read. These
numbers describe one smoke capture; native x86-64 CI results and their exact
binary/package digests are authoritative for the qualification machine.

This demonstrates the mixed-origin failure on that measured combination.
The original source-only statement that the origin *may* precede shim mapping
was appropriately conditional; source inspection alone could not establish
which order happened in a particular process.

## Primary-source interpretation

Heaptrack's pinned [1.5.0 engine](https://github.com/KDE/heaptrack/blob/b54e92e88b0895f910a5b107f499d1155a086b80/src/track/libheaptrack.cpp)
caches the first `steady_clock::now()` in `startTime()`. `elapsedTime()`
subtracts that origin; `writeTimestamp()` converts milliseconds to `size_t`.
The pinned [1.4.0 engine](https://github.com/KDE/heaptrack/blob/9e5514ecbbf56c8d5307717e4d1c70cc58dc27fe/src/track/libheaptrack.cpp)
uses the same operations. This source supplies the arithmetic being tested;
it does not substitute for observing backend calls.

GCC 12.2's pinned [chrono implementation](https://github.com/gcc-mirror/gcc/blob/2ee5e4300186a92ad73f1a1a64cb918dc76c8d67/libstdc%2B%2B-v3/src/c%2B%2B11/chrono.cc#L70-L86)
normally implements `steady_clock::now()` with `clock_gettime(CLOCK_MONOTONIC)`;
it also has a direct-syscall configuration. The installed caller symbols and
observed interposed calls determine whether that implementation path applies
to the measured binary. The repository's [Clock shim](../../shim/clock_shim.c)
answers mapped monotonic clocks from the region's `t` and falls back to libc
before mapping. Together these sources explain the measured origin mismatch,
while the audit and raw timestamps establish that it actually occurred.
