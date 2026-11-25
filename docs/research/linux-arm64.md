# Linux arm64 qualification: findings for #407

This note answers the open questions of spec
[#311](https://github.com/Stevie1704/sil/issues/311) for Linux arm64, with
evidence, before the CI and release work starts. It is research input for
[#408](https://github.com/Stevie1704/sil/issues/408) to
[#413](https://github.com/Stevie1704/sil/issues/413), not a support claim.
`SUPPORT.md` stays the contract.

## Summary

| Question | Answer |
| --- | --- |
| Does the kernel build on arm64? | Yes, with no source change, on Debian bookworm and Ubuntu 24.04. The warnings are the same as on x86-64. |
| Do the C++ tests pass? | Yes, 12 of 12 on both distributions. |
| Does the determinism gate pass? | Yes, all 10 references on both distributions. |
| Does `pytest tests/` pass? | All but two arm64-specific groups. The FMI 2.0 tests fail because Linux aarch64 has no FMI 2.0 platform directory (#409). `test_schema_import.py` failed on plain `char` signedness; this ticket fixes the test. The other failures and errors come from the test environment, not from arm64 (see [Test suite](#test-suite)). |
| Are two Runs on arm64 bit-identical? | Yes, for each of the 10 references. |
| Are arm64 and x86-64 Recordings equal? | 8 of 10 references are equal. 2 differ in the last bit of some `f64` values. The cause is fused multiply-add (FMA) contraction in the C example libraries. With `-ffp-contract=off`, all 10 are equal. |
| Is the Arena, clock-region or Native ABI layout architecture-dependent? | No. Both classes are LP64 little-endian. This ticket adds `static_assert` checks of size and offsets. `SIL_ABI_VERSION` stays 1. |
| Does the clock shim work for an opaque Process participant? | Yes. `tests/test_clock_shim.py` and `TestClockShimRunBoundary` pass. |
| Does the FMI 3.0 platform tuple `aarch64-linux` work? | Yes, with no change. The Importer already selects `aarch64-linux` from `platform.machine()`. |
| Does FMI 2.0 work? | No. The Importer refuses with a clear message, because FMI 2.0 has no standard Linux arm64 platform name. |

## Environment and commands

The host is an Apple silicon Mac with Colima (Virtualization.framework,
Linux 6.8 aarch64, 4 CPUs, 8 GiB). arm64 containers run natively. x86-64
containers run under qemu user emulation (binfmt), which is slow but gives
exact IEEE 754 results.

| Environment | Compiler | CMake | Python | glibc |
| --- | --- | --- | --- | --- |
| `debian:bookworm`, `linux/arm64` | GCC 12.2.0 | 3.25.1 | 3.11.2 | 2.36 |
| `ubuntu:24.04`, `linux/arm64` | GCC 13.3.0 | 3.28.3 | 3.12.3 | 2.39 |
| `debian:bookworm`, `linux/amd64` (qemu) | GCC 12.2.0 | 3.25.1 | 3.11.2 | 2.36 |

Each image adds only the build packages of `ci.yml` and `uv`:

```dockerfile
FROM debian:bookworm            # or ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake libssl-dev python3 python3-venv python3-dev \
    ca-certificates curl git rsync file binutils
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
```

The container gets the checkout read-only at `/src`, copies it to `/work`
without `build/` and `.venv/`, and runs as an unprivileged user. The build
must pass before the other steps. After the build, each step runs and records
its exit status, also when a step before it fails. Thus the known pytest
failures do not stop the determinism gate:

```sh
docker run --rm --platform linux/arm64 --user 501:20 -e HOME=/tmp/h \
  -v sil-work:/work -v "$PWD:/src:ro" sil-qualify:bookworm-arm64 sh -c '
    rsync -a --exclude build --exclude .venv /src/ /work/ && cd /work &&
    make venv && make build JOBS=4 || exit 1
    ctest --test-dir build --output-on-failure; ctest=$?
    SIL_SKIP_BUILD=1 .venv/bin/python -m pytest tests/ -q -rs; pytest=$?
    PYTHONPATH=/work/python/src .venv/bin/python tools/determinism_gate.py
    gate=$?
    echo "ctest=$ctest pytest=$pytest gate=$gate"'
```

For the Recording comparison, a small driver keeps both Recordings of each
reference of the determinism gate and writes their SHA-256 digests. It
replaces `check` in `tools/determinism_gate.py` with `check(..., keep=DIR)`,
so the reference list stays the list of the gate. The comparison covers only
these 10 gate references, not the other Recordings of the test suite. Save
the driver as `digests.py` outside the checkout:

```python
"""Keep both Recordings of each gate reference and write their digests."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, "tools")
out = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("gate", "tools/determinism_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
real_check = gate.check
results = {}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def keeping_check(runner, manifest, **kwargs):
    keep = out / Path(manifest).stem
    code = real_check(runner, manifest, keep=keep, **kwargs)
    results[Path(manifest).stem] = {
        "code": code,
        "run1": sha256(keep / "run1.mcap"),
        "run2": sha256(keep / "run2.mcap"),
    }
    return code


gate.check = keeping_check
code = gate.main()
(out / "digests.json").write_text(json.dumps(results, indent=2) + "\n")
raise SystemExit(code)
```

Run it in each container after `make build JOBS=4`, from `/work`, with the
directory of the driver mounted read-only at `/env` and a log directory at
`/logs`:

```sh
PYTHONPATH=/work/python/src .venv/bin/python /env/digests.py \
  /logs/<class>/recordings
```

Then compare `run1` and `run2` of each reference in the `digests.json` files
of the two classes. A message-level diff of one reference reads both
`run1.mcap` files with `mcap.reader.make_reader` and compares the metadata,
the Schemas and the `(topic, log_time, data)` of each message in order.

## Build

- `make build` passes on both distributions with no source change.
- Without `JOBS`, `make build` runs `cmake --build build -j` with no limit.
  In an 8 GiB VM, `cc1plus` was killed (out of memory). `make build JOBS=4`
  passes. This is not an arm64 defect, but an arm64 runner or a small board
  shows it first.
- The 4 `-Waddress-of-packed-member` warnings in `source_scope/observer.c`
  occur on x86-64 too. The counters that they name are at 8-byte aligned
  offsets of a page-aligned mapping, so the atomic operations are aligned at
  run time on both classes. On arm64 an unaligned atomic would fault, so a
  later change to `struct header` must keep the `uint64_t` fields 8-byte
  aligned.
- Ubuntu 24.04 (GCC 13) adds 2 `-Wunused-result` warnings. They are not
  arm64-specific.

## Test suite

| Run | Result |
| --- | --- |
| bookworm arm64 | 4004 passed, 41 failed, 14 errors, 233 skipped (8 min) |
| Ubuntu 24.04 arm64 | 4006 passed, 39 failed, 14 errors, 233 skipped (9.5 min) |

The raw logs give 41 failures and 14 errors on bookworm, and 39 failures
and 14 errors on Ubuntu 24.04. Each failure and error, and its cause:

| Tests | bookworm | Ubuntu 24.04 | Cause | arm64-specific | Action |
| --- | --- | --- | --- | --- | --- |
| `test_fmi2.py` `TestLifecycle` (16), `TestExtraction` (2), `TestRunBoundary` (6) | 24 failed | 24 failed | The fixtures build a host FMI 2.0 FMU into `binaries/<fmi2_platform_directory()>/`. On Linux aarch64 that function returns `None`, and the Importer refuses: `FMU 'Fmi2Ok' is FMI 2.0, whose profile loads binaries/linux64 on Linux x86-64; this host (Linux aarch64) has no FMI 2.0 platform directory`. | Yes | Skip in #408 while `fmi2_platform_directory()` is `None`; #409 decides the FMI 2.0 answer. |
| `test_fmi2_osmp.py` `TestMapping` (3), `TestStep` (7), `TestRunBoundary` (3) | 13 failed | 13 failed | The same refusal, for the OSMP fixture FMUs (`OsmpOk` and others). | Yes | The same. |
| `test_schema_import.py::TestFlatSchema::test_each_rule_flattens_the_fixture_in_offset_order` | 1 failed | 1 failed | The fixture member `char cls` is plain `char`. DWARF says `DW_ATE_unsigned_char` on AArch64 and `DW_ATE_signed_char` on x86-64, so the import gives `u8` on arm64. The import is correct; the test expected `i8` on every host. | Yes | Fixed here: the expected type follows the target ABI. |
| `test_hash_baseline.py` | 9 errors, 1 failed | 9 errors, 1 failed | `git rev-parse HEAD` fails, because the copied worktree has no `.git` directory. The 9 errors are at fixture setup; `test_a_workload_selection_measures_only_that_workload` calls the tool directly and fails. | No | None. In a git checkout on arm64 all of them pass. |
| `test_profile_cost_measure.py` | 5 errors | 5 errors | `tools/profile_cost_measure.py` exits 1 at fixture setup, because its `git rev-parse HEAD` fails for the same reason. | No | None at the time of this note: in a git checkout on arm64 all of them passed. Later, #311 added the arm64 refusal of the cpu collector, so these 5 tests now skip on arm64 (see the skip list). |
| `test_host_scheduling_proof.py::test_retained_qualification_replays_offline` (2 parameters) | 2 failed | passed | `TarFile.extractall(filter=...)` needs Python 3.11.4 or later; bookworm has 3.11.2. | No | None for #408, which uses Ubuntu 24.04 (Python 3.12). |
| Total | 41 failed, 14 errors | 39 failed, 14 errors | | | |

After the schema-import fix and with a git snapshot of `/work`, the rerun of
`test_hash_baseline.py`, `test_profile_cost_measure.py`,
`test_schema_import.py`, `test_clock_shim.py`,
`test_run_boundary.py::TestClockShimRunBoundary` and
`test_host_scheduling_proof.py` on bookworm arm64 gives 119 passed, 1 skipped
and only the 2 tarfile failures.

## Recording bytes

### Within the arm64 class

Each of the 10 references of the determinism gate gives two bit-identical
Recordings on bookworm arm64 and on Ubuntu 24.04 arm64. The Recordings of
the two distributions are also equal for all 10 references, although GCC
(12.2, 13.3) and glibc (2.36, 2.39) differ. `SUPPORT.md` counts the libc in
the machine class, so this is an observation, not a claim.

### Between arm64 and x86-64

Both classes use the same Manifest paths (`/work/...`), the same toolchain
release (bookworm GCC 12.2) and the same Python dependency versions.

This comparison covers only the 10 references of the determinism gate.
Other Recordings were not compared across the classes.

| Reference | arm64 = x86-64 | Participants |
| --- | --- | --- |
| `pipeline` | equal | Native C++ toys |
| `acc`, `acc-delayed` | equal | Python |
| `fmu` | equal | Vendored FMI 3.0 Reference FMU `Feedthrough`; each class loads its own binary (`aarch64-linux`, `x86_64-linux`) |
| `csv-replay` | equal | Recorded input, Python |
| `fmu-replay` | **differs**: 47 of 110 messages on `ego.motion` | FMI 3.0 FMU from `examples/fmu-replay/ego_motion.c` |
| `library` | equal | Shared library `speed_filter.c` |
| `library-window` | **differs**: 83 of 600 messages on `filter.fast`, `filter.slow` | the same `speed_filter.c`, other input |
| `gap` | equal | Shared library `gap_monitor.c` |
| `adas-reference` | equal | Native C, built with `-ffp-contract=off` |

Every difference is one unit in the last place of an `f64`. For example, the
`ego.motion` payload at 530 ms is `2485eb51b85e3440 13265305a3f22f40` on
arm64 and `2485eb51b85e3440 14265305a3f22f40` on x86-64: the low byte of the
second little-endian `f64` differs by 1. The metadata (`manifest_hash`), the Schemas, the topics, the log times and the
message count are equal.

**Cause: FMA contraction.** GCC compiles C in GNU mode with
`-ffp-contract=fast`. On AArch64 it fuses `a += b * c` into one `fmadd`
instruction, which rounds once. The x86-64 baseline ISA has no FMA, so the
same source rounds twice. `speed_filter.c` (`filtered += gain * (speed -
filtered)`) and `ego_motion.c` (`position += speed * step + ...`) have this
form. `library` uses the same library as `library-window`, but its input does
not reach a value where the two roundings differ.

**Evidence.** A second arm64 build with
`-DCMAKE_C_FLAGS=-ffp-contract=off -DCMAKE_CXX_FLAGS=-ffp-contract=off`
gives all 10 Recordings bit-identical to x86-64. `adas_reference` already
builds with `-ffp-contract=off` for this reason (`examples/CMakeLists.txt`).

**Python participants.** The ACC Participants use CPython `float` arithmetic
and glibc `libm`. Their Recordings are equal across the classes in this run.
This is an observation for these inputs, not a guarantee: `libm` functions
are not required to be correctly rounded, and glibc can use other code per
architecture.

**Struct layout.** `silschema` generates packed structs with a size
`static_assert`, little-endian, so the layout is the same on both classes.
`sil schema import` reads the compiled layout from DWARF. The member offsets
and sizes are the same on both LP64 classes, but the signedness of plain
`char` (and of `wchar_t`) is not: plain `char` imports as `i8` on x86-64 and
`u8` on arm64. A Schema imported on one class is valid on the other, but an
import on each class gives two different Schemas, so the Manifest hash
differs.

Not changed here: SiL does not promise identity between classes (#311). An
adopter who wants it can build with `-ffp-contract=off`. `docs/fmi.md` or
`SUPPORT.md` can state this in #413.

## Binary layouts

The Step protocol is JSON lines, so its only binary parts are the Arena and
the clock region. The Native participant ABI is a C function table.

| Layout | Fields | Size | Same on both classes because |
| --- | --- | --- | --- |
| `sil_arena` (`include/sil/arena.h`) | `seq` @0, `len` @8 | 16 | two `uint64_t`; Python packs it as `"<QQ"` |
| `sil_clock_region` (`include/sil/clock_region.h`) | `t` @0, `epoch` @8, `sleep_policy` @16, `thread_policy` @24 | 32 | four `uint64_t` |
| `sil_api_v1` (`include/sil/participant.h`) | `abi_version` @0, `ctx` @8, `register_task` @16, `subscribe` @24, `publish` @32, `take` @40, `now_ns` @48, `fail` @56 | 64 | LP64 on both: 8-byte pointers, 4 bytes of padding after `abi_version` |

New `static_assert` checks fail the build when one of these changes:

- `kernel/src/channel_arenas.cpp`: `sil_arena` size and offsets.
- `kernel/src/process_participant.cpp`: `sil_clock_region` size and offsets
  (writer).
- `shim/clock_shim.c`: the same checks with `_Static_assert` (reader, C).
- `kernel/src/native_participant.cpp`: `SIL_ABI_VERSION == 1`, LP64, and
  `sil_api_v1` size and offsets.

They pass on macOS arm64, Linux arm64 (bookworm, Ubuntu 24.04) and Linux
x86-64 (bookworm under qemu).

**Alignment.** A slot stride is `16 + capacity`. When `capacity` is not a
multiple of 8, the header of slot 1 and later is not 8-byte aligned. The
kernel then reads `seq` and `len` through an unaligned `sil_arena *`. On
AArch64 Linux, ordinary unaligned loads and stores to normal memory do not
fault, so this works on both classes. It is undefined behavior in C++, and an
atomic access there would fault on arm64. #415 fixed this: the kernel pads
`shm_capacity` to a multiple of 8. Children already stride by
`16 + shm_capacity`, so the fix needs no protocol or `SIL_ABI_VERSION` change.

**Memory ordering.** No Arena or clock-region field is an atomic. The
kernel writes the region or the slot, then writes the Step line to the pipe;
the child reads the line, then the region. The `write` and `read` system
calls order the accesses on both architectures, so the weaker AArch64 memory
model needs no barrier. Each 8-byte aligned `uint64_t` store is single-copy
atomic on both, so the shim never reads a torn `t`.

## Clock shim

`tests/test_clock_shim.py` (31 tests) and
`tests/test_run_boundary.py::TestClockShimRunBoundary` (6 tests, including
`test_two_shimmed_runs_are_bit_identical` and
`test_sil_check_passes_on_shimmed_manifest`) pass on bookworm arm64 and Ubuntu
24.04 arm64. `LD_PRELOAD` interposes `clock_gettime`, `gettimeofday`, `time`,
the sleep family and `pthread_create` for an opaque Process participant with
no change.

## FMI platforms

- **FMI 3.0.** `platform_directory()` maps `platform.machine()` `aarch64` to
  `aarch64-linux`. The FMI 3.0 tests (`test_fmi*.py` other than the FMI 2.0
  files) pass on arm64. The `fmu` reference loads the `aarch64-linux` binary
  of the vendored Reference FMU `Feedthrough`
  (`tests/fixtures/reference-fmus/3.0/`), so a third-party FMI 3.0 binary
  works, not only host-built ones. `fmu-replay` packages a host-built
  library.
- **FMI 2.0.** `_FMI2_PLATFORMS` has no `("Linux", "aarch64")` entry, because
  FMI 2.0 names only `linux32` and `linux64` (x86). The Importer refuses with
  a ManifestError that names the host. #409 decides whether to accept a
  non-standard directory, for example `aarch64-linux`.
- `sil.fmi.inspection._unverified` falls back to `linux64` in its text when
  the platform directory is `None`. This is only a label, but #409 should
  check it.
- The FMUs that `proofs/` downloads were not checked for `aarch64-linux`
  binaries. Their proofs pin `linux/amd64` (see the skip list).

## Profiling, tracing and debugging

| Feature | arm64 result | Recommendation |
| --- | --- | --- |
| Source scopes (`sil/profile_scopes.py`) | Refuses: `source scopes need Linux x86-64/glibc`. Tests skip. | Stays x86-64 only. |
| CPU profile (perf) | Not gated in the product: `sil profile --collector cpu` does not refuse on arm64. With Debian `linux-perf` 6.1 as root in a privileged container (the VM has `perf_event_paranoid` 4), a copy of `test_profile_perf_integration.py` without its x86-64 gate gives 12 passed, 2 failed: `test_matching_debug_file_gives_source_lines_of_the_stripped_library` (no source file in the report) and `test_inlined_frames_are_marked_and_belong_to_their_physical_frame`. The perf version differs from CI (Ubuntu `linux-tools` 7.0), so the cause is open. | Keep x86-64 only, and add the refusal that #311 asks for. |
| Heap profile (Heaptrack) | `SIL_PROFILE_HEAP=available` with Debian Heaptrack 1.4.0: `test_profile_heap_integration.py` and `test_profile_heap_run.py` give 47 passed, 1 skipped. | Works with no change. #311 can keep it x86-64 only for the first release, or qualify it on the arm64 runner with Heaptrack 1.5. |
| Function tracing (`proofs/function-tracing`) | `proof.py` refuses non-x86-64 hosts (XRay). | Stays x86-64 only. |
| Source debugging (GDB) | `tests/test_source_debugging_proof.py` passes with GDB 13.1 (7 passed). `proofs/source-debugging/proof.py` refuses non-x86-64 and packages `x86_64-linux` binaries. | Stays x86-64 only. |

## Skip list for #408

The CI job of #408 runs on `ubuntu-24.04-arm`. These tests must skip there,
with the reason given.

**Must be marked (they fail today):**

| Tests | Skip reason |
| --- | --- |
| `tests/test_fmi2.py`: `TestLifecycle`, `TestExtraction`, `TestRunBoundary` (24 tests) | `this host has no FMI 2.0 platform directory` (condition: `fmi2_platform_directory() is None`), until #409. |
| `tests/test_fmi2_osmp.py`: `TestMapping`, `TestStep`, `TestRunBoundary` (13 tests) | The same. |

**Already skip on arm64, no change:**

| Tests | Existing reason |
| --- | --- |
| `tests/test_fmi2.py::TestReferenceFmus` | `the Reference FMUs ship FMI 2.0 binaries for x86-64 only` |
| `tests/test_fmi_resources.py` (3) | `External fixture qualified on Linux x86-64` |
| `tests/test_source_scope_capture.py` (most) | `needs Linux x86-64/glibc` |
| `tests/test_profile_perf_integration.py` | gated to Linux x86-64 by `platform.machine()` |
| `tests/test_adas_matrix.py` | needs `SIL_ADAS_MATRIX_IMAGE` (an x86-64 image) |
| `tests/test_schema_import.py::...big_endian...` | needs clang |

**Must not be skipped:** `test_schema_import.py` (fixed here),
`test_hash_baseline.py` (it needs only a git checkout), `test_clock_shim.py`
and `TestClockShimRunBoundary` (the spec requires them on arm64).

**Skip after the cpu collector refusal:** the 5 tests of
`test_profile_cost_measure.py` that read the `cpu` and `cpu-activations`
cases. They use the fake perf, but `sil profile --collector cpu` refuses on
another Linux machine class before it looks for perf, so the fake perf
cannot pass the refusal. They carry the `x86_64_only` marker. The other
tests of that file need no collector and run on arm64.

**Browser tests.** `ci.yml` sets `SIL_TEST_CHROME=google-chrome`, which makes
the HTML report browser tests fail, not skip, when Chrome is missing. Google
publishes no Linux arm64 Chrome, so the arm64 job must name a Chromium
binary or leave the variable unset. This was not tested here: the containers
had no browser, and those tests skipped.

**Proofs.** No proof runs in `ci.yml`. Each proof workflow stays x86-64:

| Proofs | Reason |
| --- | --- |
| `acc-fmi`, `esmini`, `fmi-ls-bus`, `libsafety`, `openacc-controller`, `public-workloads` | Pinned third-party FMUs or tools with `x86_64-linux` binaries only; scripts pin `linux/amd64`. |
| `fmi2-importer`, `osmp-importer`, `osmp-qualification`, `osmp-sensor` | FMI 2.0 `linux64` binaries; `linux/amd64` pinned. |
| `adas-closed-loop`, `adas-cost`, `adas-equivalence`, `adas-fmu`, `adas-matrix` | `run-proof.sh` pins `linux/amd64`; evidence is x86-64 (`adas-fmu`, `adas-closed-loop` package `x86_64-linux`). |
| `profiling`, `profiling-qualification`, `capture-windows`, `host-scheduling`, `actionable-profiling`, `heaptrack-shim` | perf or Heaptrack evidence of the x86-64 class; `capture-windows/qualify.py` refuses non-x86-64. |
| `function-tracing`, `source-debugging`, `source-scopes` | x86-64 only by design (XRay, `x86_64-linux` debug FMUs, source scopes). |
| `agent-investigation`, `recording-query` | Retained x86-64 evidence; their `tests/` checks are offline and pass on arm64. |

## Notes for later tickets

- **#409:** FMI 3.0 needs no change. FMI 2.0 needs a decision and then a
  `("Linux", "aarch64")` entry or a documented refusal.
- **#410 and #411:** `docker build --platform linux/arm64 --target runtime
  --build-arg SIL_VERSION=0.3.2 .` passes natively with no change. The
  pinned base digest resolves its arm64 layer, and `sil-run --version` in the
  image prints `0.3.2` on `aarch64`. The `build` stage that the native
  archive uses is part of this build. `tools/release.py` validation and the
  smoke tests were not run on arm64.
- **#408:** `ci.yml` builds with `cmake --build build -j`. The GitHub arm64
  runner has enough memory, but keep the job count in mind on smaller hosts.
- **#413:** state the FMA cause of cross-class differences and the plain
  `char` difference of `sil schema import`.
