# Shared-library participants

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Replay recorded input into a shared library

An existing library often has its own C interface: an init, a cyclic step,
an output read and a terminate call. It does not export
`sil_participant_init`. [examples/library/](../examples/library/) runs such a
library as a Process participant without changing it:

| File | Role |
| --- | --- |
| `speed_filter.h`, `speed_filter.c` | the library under test: its own API, global state, prints to stdout |
| `binding.py` | the per-library binding: symbols, C types, error codes |
| `adapter.py` | the Process participant: Step protocol, lifecycle, routes, failures |
| `filter_test.py` | the Test participant: computes every output independently |
| `signals.csv`, `mapping.json` | the recorded input and its `sil-csv` mapping |
| `manifest.py` | the Run: Replay, two library instances, Test participant |

With the staged installation on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d "$HOME/sil-library.XXXXXX")
cc -shared -fPIC -O2 -o "$workdir/speed_filter.so" examples/library/speed_filter.c
sil-csv examples/library/mapping.json examples/library/signals.csv \
    -o "$workdir/signals.mcap" --receipt "$workdir/signals.receipt.json"
python examples/library/manifest.py "$workdir/library.json" \
    --recording "$workdir/signals.mcap" --library "$workdir/speed_filter.so"
sil-run "$workdir/library.json" -o "$workdir/run-1.mcap" \
    --participant-timeout-ms 10000
sil-run "$workdir/library.json" -o "$workdir/run-2.mcap" \
    --participant-timeout-ms 10000
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
```

`make example-library` runs the same sequence from the source tree.
`--participant-timeout-ms` gives each library answer 10 s of wall-clock
time. A library that hangs then fails the Run instead of stopping it. The
deadline is not Manifest data, so it does not change the Recording. For the
deliberately incorrect case, build the library with `-DSPEED_FILTER_DEFECT`
and build the Manifest over that library. The Test participant then fails
the Run (exit 1) at the first output that differs:

```text
filter.fast at t=0 ns: filtered_speed_mps 4.0 differs from the independently computed 2.666666666666667
```

What the Manifest makes explicit:

- **Period.** Every participant steps every 10 ms. The adapter gets the same
  value as `--period-ns` and passes it to the library's init. The init line
  does not carry the Period, so the adapter checks `dt` at each Step and
  fails the Run when it is different.
- **Initial values.** `--parameter initial_speed_mps=…` is the library's
  state before its first cycle. `--initial speed_mps=…` is the input before
  the first recorded Message is visible. After that, each input is held
  until a newer Message replaces it.
- **Latency.** `ego.speed` declares `latency_ns` of one Period, so a
  recorded Message is visible at the first Step after its publication. The
  output Channels declare `latency_ns=0`, and the Test participant has a
  higher `priority` value than the library instances, so it runs after them
  in each Slot. It checks every output in the Slot that publishes it, the
  output of the last Step included.
- **Finite routes.** Every subscriber route fails on overflow. `ego.speed`
  routes have capacity 2: a Message waits one Period, and the next one can
  arrive before it is taken. Output routes have capacity 1.
- **Parameters.** `--parameter time_constant_s=…` configures the library.
  The two instances have different parameters and publish on different
  Channels.

**Lifecycle and isolation.** Each Step runs one library cycle and publishes
the result at the Step's time. `shutdown` calls the library's terminate. The
library keeps its state in globals and refuses a second init, but each
instance is a separate process: each has its own globals and each starts
with cycle 1. A new Run starts new processes.

**Failures.**

| What goes wrong | Result |
| --- | --- |
| the library does not load, for example a missing dependency | Manifest error (exit 2): `cannot load library '<path>': <loader message>` |
| the library does not export a symbol the binding calls | Manifest error (exit 2): `library '<path>' does not export '<symbol>'` |
| the library rejects its configuration | Manifest error (exit 2): the library path, the parameters, and the library's error code with its meaning |
| a cycle returns an error code | Run failure (exit 1) with the virtual time and the error |
| the library crashes | Run failure (exit 1): `participant '<name>' exited unexpectedly` |
| the library hangs | Run failure (exit 1) at the `--participant-timeout-ms` response deadline; without the option, the runner waits |
| the output is incorrect | Run failure (exit 1) from the Test participant |

On every path, the runner reaps the adapter process and its cooperative
descendants, and removes the Run working directory and the mapped regions
([Process participant descendants](running.md#process-participant-descendants)).

**stdout.** The Step protocol owns stdout. Before the library loads, the
adapter moves the protocol to a private descriptor and points descriptor 1
at stderr. What the library prints stays visible on the runner's stderr and
does not corrupt the protocol. This is the lesson of the esmini proof.

### Binding another library

Replace `binding.py`. Keep its names: `Binding(library)`, `init(period_s,
parameters)`, `step(inputs)`, `output()`, `terminate()`, and the
`PARAMETERS`, `INPUTS` and `OUTPUTS` tuples. Write the `ctypes` declarations
from your library's header: every argument type, every result type, every
struct field in header order. ctypes assumes `int` for an undeclared result,
which silently truncates a `double`. Raise `BindingError` with the library's
own error code and its meaning. Do not `print` from the binding: Python's
`sys.stdout` is the protocol descriptor. Write diagnostics to `sys.stderr`.

`adapter.py` does not change. It checks that the command line gives exactly
the binding's parameters and initial inputs, and that the input and output
Channels' Schema fields are exactly `INPUTS` and `OUTPUTS`. Then declare your
Schemas, Channels and adapter commands in your Manifest. The adapter binds
one input Channel and one output Channel.

Nothing is discovered: the binding states the ABI because only the library's
header states it. There is no symbol inference.

### Library dependencies

The adapter loads the library with `dlopen`, through `ctypes.CDLL`. The
dynamic loader resolves the library's own dependencies:

- Prefer a run path in the library, for example `-Wl,-rpath,'$ORIGIN'` with
  the dependencies beside it.
- Otherwise, set `LD_LIBRARY_PATH` for `sil-run`. Each child inherits the
  runner's environment, and the kernel does not change it. Use absolute
  entries: a child starts in its own Run working directory.
- In the Example image, install the dependencies in the image, as the esmini
  proof does.

The runner resolves a relative library path in the command against the
Manifest's directory. The example's `manifest.py` writes the absolute
path. The library bytes are not in the Manifest hash, but
the Run's provenance side-car records the SHA-256 of the library, because
the command names it. The side-car does not record the library's
dependencies or the environment: pin them with the image, or declare them
in a [regression bundle](regression-bundles.md).

The adapter itself needs `python3` with the `sil` wheel on `PATH`.

### When the Clock shim applies

Add `shim=True` to the adapter's `add_process` when the library reads the
wall clock (`clock_gettime`, `gettimeofday`, `time`) or sleeps. For a library
that sleeps, also choose the `sleep` policy: the default `"reject"` fails
each sleep with `ENOSYS`, and `"immediate"` returns at once. The shim is
preloaded into the adapter process, so it also answers the loaded library's
calls. The example library reads no clock and needs no shim. The shim's
boundaries apply: a statically linked clock read, a direct syscall, or the
vDSO is not virtualized.

The Clock shim applies only to Process participants. A Native participant
runs in the kernel's own process, and the runner does not preload the shim
into that process. A Native library that reads the clock or sleeps uses real
time. The Run is then not deterministic, and nothing warns you before the
determinism check fails.

Do not preload the shim into the runner. The runner measures the response
deadline with its own monotonic clock. The shim freezes that clock within a
Step, so the deadline does not expire and a hung Process participant hangs
the Run.

A Native library must take time only from the `now_ns` argument of its Task
or from `sil_api_v1.now_ns`. Put a library that reads the clock in a Process
participant with `shim=True`. The shim answers only the intercepted calls in
[Virtual clock shim](running.md#virtual-clock-shim-for-opaque-posix-vecus).
The library must also behave deterministically in all other respects.

### Native participant alternative

A library can also export `sil_participant_init` from
[include/sil/participant.h](../include/sil/participant.h) and run in the
kernel's own process. That removes the process boundary and the per-Step
protocol line, but has limits:

| | Process participant (this adapter) | Native participant |
| --- | --- | --- |
| API | the library's own; bind it in `binding.py` | must export `sil_participant_init` and register tasks |
| crash | the child exits; Run failure; the runner cleans up | the crash ends the runner: the Recording is not finished and the runner cannot clean up |
| hang | Run failure at the response deadline | the runner hangs; the response deadline applies only to Process participants |
| global state | one set per process, so any number of instances | one set per runner process: at most one instance per Run unless state is behind the `user` pointer |
| stdout | moved off the protocol by the adapter | shared with the runner |
| clock | `shim=True` answers the intercepted clock reads from Virtual time and applies the `sleep` policy | no Clock shim: a clock read or sleep uses real time, so take time only from `now_ns` |
| cost | one protocol round trip per Step | a function call per task |

Use the Native ABI for a library you build against SiL and trust not to
crash or hang. Use this adapter for an existing library with its own API.
[The Native ADAS reference application](adas-reference.md) shows the Native
path: a C application with its own API, and a small adapter that exports
`sil_participant_init` and keeps one instance per Participant.

## Large interfaces

A Native participant exchanges SiL Schemas. A SiL Schema is flat: primitive
fields and fixed `count` arrays, packed and little-endian. A production
library often has large, deeply nested input and output structs. Do not
write their Schemas by hand. `sil-schema-import` reads the layout that the
compiler recorded in DWARF and writes:

1. A flat Schema whose packed layout is byte-identical to the compiled
   layout of the type. Padding becomes explicit padding fields.
2. A C layout-check header. It checks `sizeof` of each type and `offsetof`
   and `sizeof` of each imported member, against the project's own type.

The adapter then copies between the project struct and the SiL struct with
`memcpy`. It needs no conversion code.

The command needs the optional extra: `pip install 'sil[dwarf]'`. It reads
ELF objects only (Linux). It needs no compiler, `libclang` or `pahole`.

```sh
sil-schema-import build/interface_types.o \
    --type 'AdasInput=adas.Input' --type 'AdasOutput=adas.Output' \
    -o schemas.json --layout-check adas_layout_check.h
silschema schemas.json adas_messages.h
```

Each `--type` is `<C type>=<Schema name>`. The C type is a `typedef` name or
a `struct` tag. The command prints the field count and the byte size of each
Schema: an unrolled array of structs can give thousands of fields.

Include the project header, then `adas_layout_check.h`, in the adapter.
Compile the adapter with the release flags. If the Schema and the real
layout differ, the build fails and names the member. The result of the
compiler is final, not the result of the import.

```c
#include "interface_types.h"
#include "adas_layout_check.h"
#include "adas_messages.h"

AdasInput in;          /* filled through the project's own members */
adas_Input message;
memcpy(&message, &in, sizeof message);
api->publish(api->ctx, "adas.input", &message, sizeof message);
```

### Flattening rules

| C construct (from DWARF) | Schema result |
| --- | --- |
| Nested struct member | member path joined by `_` (`ego.pose.x` becomes `ego_pose_x`) |
| Array of primitives, any number of dimensions | one field, `count` is the product of the dimensions |
| Array of structs | one set of fields per element: `objects[3].x` becomes `objects_3_x`. Each element stays a scalar, so `sil-csv`, an Interceptor `override` and `sil-compare` can address it |
| Padding, between members and at the end | `u8` field with `count` equal to the gap, named `_sil_pad_<offset>` |
| `enum` | integer of the enum's size and DWARF signedness |
| `_Bool` / `bool` | `u8` |
| `char`, `signed char`, `unsigned char` | `i8` or `u8`, from the DWARF encoding |
| `float`, `double`, fixed-width integers, `typedef`s | the matching primitive, after `typedef` resolution |

Fields are in offset order. The same object and the same arguments give
the same output bytes.

The import is rejected (exit 2) and writes no file when a type contains a
union, a bit-field, a pointer, a flexible array member, `long double`,
`__int128`, `_Float16` or a vector type; when two flattened names collide
(`a_b.c` and `a.b_c`); when a name breaks the `silschema` naming rule (see
[SUPPORT.md](../SUPPORT.md)); when the object is big-endian; or when the
object has no DWARF type of a requested name. Each diagnostic names the
type and the member path:

```text
sil-schema-import: type 'Frame' member 'payload.value': union is not supported
sil-schema-import: type 'Frame' members 'a_b.c' and 'a.b_c' both flatten to 'a_b_c'
```

### Where the DWARF comes from

- `-g` does not need `-O0`. `-O2 -g` keeps the complete type information and
  does not change the generated code.
- Take the layout from the same release configuration that SiL runs. A debug
  build can have different members through `#ifdef DEBUG` or `NDEBUG`.
- Recommended: compile one translation unit that includes the interface
  header, with the release flags and defines plus `-g -c`, and import from
  that `.o`. The library itself needs no change. A compiler does not keep an
  unused type in DWARF, so define one object of each imported type:

  ```c
  #include "interface_types.h"
  AdasInput sil_import_input;
  struct AdasOutput sil_import_output;
  ```

- Alternatively, build the library with `-g` and keep the debug information
  in a separate ELF file with `objcopy --only-keep-debug`. Import from that
  file. The import cannot read `-gsplit-dwarf` output (`.dwo` files). A
  stripped `.so` has no DWARF types.
- Take the layout from the host build that SiL loads (Linux x86-64 or
  arm64), never from the ECU toolchain. An ECU target can have a different
  integer and pointer size, alignment and endianness.

### When an opaque payload is better

A Schema field is one value that a Recording, `sil-csv`, an Interceptor and
`sil-compare` can address. Use an opaque `u8` field with a `count` instead
when:

- the type contains a construct that the import rejects, for example a
  union or a bit-field, and no tool has to read inside it;
- the bytes are an encoded message (for example an OSMP or a CAN frame) that
  another tool decodes;
- the struct is so large that thousands of fields are of no use, and only
  some of its values have to be checked. Import a smaller struct with those
  values, or carry the rest as one opaque field.
