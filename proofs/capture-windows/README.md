# Late CPU capture-window experiment (#361)

This proof compares whole-Run CPU collection/post-filtering with explicit
Virtual-time `[610000000,611000000)` control on one fresh Run and one perf
collector. It adds no supported CLI, scheduler wait, ordinary-run hook,
Manifest field, ABI change, trigger language, ring buffer or heap window.

`workload.c`, compiled with `-O2 -g -fno-omit-frame-pointer`, performs fixed
integer work on 620 Activations per Task. Task `late` enters the known
`late_hot_function` only at index 610; Native Task `peer` shares its Virtual
times. `process.py` runs the same compiled work through the Step protocol.
Independent workload entry/exit markers remain separate from diagnostics
notices, completed Activation observations and sampled stacks. Published
Task/index values do not depend on any host observation.

The proof-only CMake overlay copies and patches just the diagnostics
translation unit into the build directory. Constructor/exit notifications use
a nonblocking Unix datagram send, with explicit drops and a finite receiver
budget. The ordinary target continues to compile the repository's untouched
sources. A supervisor serializes named perf commands and records its receipt,
write and acknowledgment times. No Activation waits for a collector.

On Linux x86-64/glibc with perf, a compiler, CMake and the Python package:

```sh
python proofs/capture-windows/qualify.py build "$PWD/build/window-work"
python proofs/capture-windows/qualify.py run "$PWD/build/window-work" \
  --out "$PWD/build/window-evidence" --environment native
```

Both directories must be fresh. Commands, package versions, host policy,
binary hashes/build IDs, raw perf data, metadata, samples, controls, file
growth, workload markers, diagnostics and Recordings are retained. Three
fresh repetitions compare ordinary, diagnostics, observer, whole and bounded
modes. Per-Run/Step guards stay 60 seconds/10 seconds. Times and file sizes are
observations, never timing correctness thresholds or an overhead correction.
The 128 MiB capture limit, 100 ms guard polling and existing 8 MiB storage
reserve remain explicit; a stop preserves Run execution. Export has a separate
256 MiB/180-second budget. There is no automatic permission change.

The workflow records native/container availability before changing host
policy. The capability experiment explicitly uses CAP_PERFMON and an
unconfined seccomp profile. Native sampling explicitly sets paranoid 2.
A separate 16 MiB tmpfs tests active collection crossing the existing 8 MiB
reserve. A second case deliberately consumes the remaining space between
guard polls to expose actual collector ENOSPC behavior. Run outputs live on
the main filesystem. This injected competing writer can defeat a reserve;
its measured Run/collector outcomes remain separate.

Cases include equal-time Tasks, range and exact-Task edges, empty/unreached
selection, delayed enable and a host stop during ongoing work, failed and
SIGKILL-unclosed work, socket backpressure, capture-limit overshoot and reserve
stops. Intentional CPU disable, observer loss, unknown control transitions and
perf loss/truncation must remain distinct. Acknowledgment is neither the
first/last sample nor a drain guarantee; zero samples do not prove complete
coverage. See [source findings](../../docs/research/perf-capture-window-control.md).

Results and the build/defer/no-change decision are recorded after Linux
qualification. A positive feasibility result alone cannot ship a collector:
it needs separate coverage/schema and implementation tickets.
