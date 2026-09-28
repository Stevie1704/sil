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
| cost | one protocol round trip per Step | a function call per task |

Use the Native ABI for a library you build against SiL and trust not to
crash or hang. Use this adapter for an existing library with its own API.
