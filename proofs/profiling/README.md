# perf and Heaptrack feasibility proof (#314)

This proof qualifies Linux perf (CPU sampling) and Heaptrack (allocation
capture) against representative SiL Runs on the supported machine class,
Linux x86-64/glibc. It is part of the profiling spec
[#313](https://github.com/Stevie1704/sil/issues/313). It uses external tools
and disposable adapters only. It does not add a command, a schema, an ADR or
a runtime interface. The production capture and its contract belong to #315
(CPU), #316 (Activation correlation) and #317 (allocation).

## Outcome

| Backend | Decision | Scope |
| --- | --- | --- |
| perf | **proceed** | the runner and all its descendants, native Linux and the documented container setup |
| Heaptrack | **proceed**, with limits | the runner scope, and a named Process participant that is not shimmed (the FMU group) |
| Heaptrack, shimmed Process participant | **defer** | not qualified: see [Requirements for #313](#requirements-for-313) |
| Heaptrack, Activation correlation | **unavailable** | process and call-site analysis only |

## What runs where

The `proof-profiling` workflow runs on a GitHub-hosted `ubuntu-24.04` runner
(x86-64, kernel `6.17.0-1022-azure`). It has one job. The steps run in this
order, because one step changes the host:

| Step | Host state | What runs |
| --- | --- | --- |
| image | as provided | `container.sh image`: [`Dockerfile`](Dockerfile), pinned Python base and Debian snapshot `20260901T000000Z`: GCC 12.2, binutils 2.40, `linux-perf` 6.1.176-1, `heaptrack` 1.4.0-2; SiL built and installed |
| native setup | as provided | [`native-setup.sh`](native-setup.sh): apt `linux-tools-$(uname -r)` (perf 6.17.13) and `heaptrack=1.5.0+dfsg1-2ubuntu3`; SiL built and installed into a prefix; Python 3.12 environment |
| probes | `perf_event_paranoid` 4 (Ubuntu default) | `proof.py probe` natively as the unprivileged `runner` user, and in three containers: default capabilities, `--cap-add PERFMON`, `--security-opt seccomp=unconfined` |
| container proof | paranoid 4 | `container.sh run ... --cap-add PERFMON`: the full proof, `--init --network none` |
| **disclosed host change** | `sudo sysctl -w kernel.perf_event_paranoid=2` | the upstream kernel default; Ubuntu sets 4 |
| probes | paranoid 2 | native, and containers with default capabilities and with seccomp unconfined |
| native proof | paranoid 2 | `proof.py run` as the unprivileged `runner` user |

The probes write `probe.json`. The proofs write `results.json` and
`results.md`. The workflow uploads everything as the `profiling-evidence`
artifact. [`retain.py`](retain.py) copies the retained part into
[`evidence/`](evidence/).

To repeat it: push a change under `proofs/profiling/` in a pull request, or
start the workflow on a branch where it exists. Locally, on an x86-64 Linux
host with Docker: `container.sh image DIR`, then
`container.sh run DIR NAME --cap-add PERFMON`. Natively:
`native-setup.sh PREFIX`, put `PREFIX/python/bin` and `PREFIX/native/bin` on
`PATH`, then `python proof.py run DIR`. No other setup is necessary.

## The workload

[`workload.py`](workload.py) builds every binary with one compiler and
`-O2 -g`, the optimization of a measured build. No binary has frame
pointers. The Run is 1 s of Virtual time with a 10 ms period: 100
activations of each participant.

| Participant | Kind | Hot functions | Allocation site (calls x bytes) |
| --- | --- | --- | --- |
| `native` | Native participant, [`silproof_native.c`](workload/silproof_native.c); stripped, with a separate debug file | `silproof_native_spin_even`, `silproof_native_spin_odd` (alternate activations, both `static`) | `silproof_native_alloc_site` 1600 x 1024; `silproof_native_retain_site` 1 x 65536, never freed |
| `process` | shimmed Process participant: [`process_adapter.py`](workload/process_adapter.py) loads [`silproof_process.c`](workload/silproof_process.c) with ctypes (a library loaded at run time) | `silproof_process_spin` | `silproof_process_alloc_site` 800 x 2048 |
| child | short-lived process that `process` starts at Steps 0, 25, 50 and 75: [`silproof_child.c`](workload/silproof_child.c) | `silproof_child_spin` | none |
| `fmu` | FMI Importer over an FMU that it extracts into the Run working directory: [`silproof_fmu.c`](workload/silproof_fmu.c), the EgoMotion example with hot work in `fmi3DoStep` | `silproof_fmu_spin` | `silproof_fmu_alloc_site` 400 x 4096 |

Each hot call writes its host interval to a sidecar file:
`CLOCK_MONOTONIC` by the raw syscall, so the Clock shim cannot change it, and
the PID and TID. The sidecar directory comes from the runner environment,
not from the Manifest. The shimmed participant publishes its libc
`CLOCK_MONOTONIC` reading and its child's reading. Under the shim both are
Virtual time, so the Recording shows whether the shim stays active.

A second build of the Native library with other code gives the
**mismatched** debug file: same file name, other build ID.

## The Runs

All Runs use the same Manifest bytes, except that the Heaptrack Process
scopes put the launch adapter before the selected participant's command. Every Run has
a 60 s Process response deadline (`--participant-timeout-ms 60000`).

| Run | Collector and options |
| --- | --- |
| `ordinary` | none |
| `perf-dwarf` | `perf record -e cpu-clock:u -F 999 -k monotonic --call-graph dwarf,8192 --buildid-mmap -- sil-run ...` |
| `perf-fp` | the same with `--call-graph fp`, and perf's default build-ID handling |
| `heaptrack-runner` | Heaptrack preloaded into `sil-run`; the matching debug file is beside the stripped library |
| `heaptrack-runner-mismatched` | the same with the mismatched debug file |
| `heaptrack-fmu` | Heaptrack in the `fmu` participant only (live) |
| `heaptrack-shimmed-process` | Heaptrack in the shimmed `process` participant only (live) |
| `heaptrack-shimmed-process-sleep-immediate` | the same with the shim sleep policy `immediate`; also the `fmu` participant raw, interpreted after the Run |

The Heaptrack launch adapter, [`heaptrack-launch.sh`](heaptrack-launch.sh),
puts Heaptrack's preload library before the existing `LD_PRELOAD` and execs
the command, so the participant keeps its PID, pipes, working directory,
arguments and environment. In `live` mode Heaptrack writes to a FIFO. The
proof driver starts `heaptrack_interpret` on that FIFO before the Run,
outside the Run's process group. In `raw` mode Heaptrack writes a file that
the driver interprets after the Run.

## Checks

[`proof.py`](proof.py) exits 1 when a check fails. A **finding** is
recorded with its result and does not fail. No check uses a timing, a sample
percentage, an RSS value or an overhead threshold. A presence check needs at
least one sample; each hot function got 200 or more in each proof.

Both proofs (native and container) passed every check in the retained run.
The full lists are in `evidence/*/run-*/results.md`.

| Check | perf-dwarf | heaptrack |
| --- | --- | --- |
| Run exits 0, no process is left, Recording equals the ordinary Recording | yes | yes (runner, FMU scope) |
| each known function or site is in the evidence | 5 of 5 hot functions | exact calls and size for each site |
| matching separate debug file gives names (perf: and source lines) | yes | yes |
| mismatched debug file gives no name (perf: and no source line) | yes | yes |
| every hot sample is inside one of its own host intervals, same PID | yes, 0 outside, 0 in another function's interval | not applicable |
| every workload process, the four children too, has samples under its own PID | yes | not applicable |
| build IDs of the four workload modules are in the capture | yes | module paths only |
| each hot function shows its source caller (unwinding) | yes | not applicable |
| a capture holds no other scope's allocations | not applicable | yes |

## Coverage

"yes": qualified by a check. "finding": recorded, not qualified. Both
environments gave the same result, except where stated.

| Case | perf | Heaptrack |
| --- | --- | --- |
| Native participant (inside `sil-run`) | yes | yes, runner scope |
| Process participant, shimmed | yes | finding: not qualified (see below) |
| Process participant, unshimmed (FMU group) | yes | yes, named scope |
| library loaded at run time (ctypes, `dlopen`) | yes | yes |
| FMU library in the Run working directory, removed at Run end | yes, with `--buildid-mmap` and an explicit build-ID cache entry | yes, live interpretation only |
| short-lived child | yes, all four children by PID | finding: Heaptrack removes `LD_PRELOAD` from the children's environment |
| Clock shim | perf timestamps are host time; shimmed readings are Virtual time | finding: Heaptrack timestamps become Virtual time |
| stripped library, matching separate debug file | yes, names and source lines | yes |
| stripped library, no debug file | finding: static functions have no name, the samples stay in the module | not run |
| stripped library, mismatched debug file | yes, refused | yes, refused |
| PID namespace | yes: perf and the workload in the same namespace, native and container | not applicable |
| caller paths | yes with DWARF; finding: frame pointers do not give the caller | yes |
| sample loss | none in the retained run (`report-stats.txt`) | not applicable |

Not covered by this proof: PID reuse within one capture; perf outside a
container that samples a process inside it; kernel-mode samples; hardware
events; Heaptrack attach mode (`-p`, needs ptrace); the diagnostics runner
together with a collector (#316, #319); interruption and storage limits
(#319); a Process participant written in C.

## Findings

**Permissions (perf).** The outcome comes from the probes:

| Environment | paranoid 4 (Ubuntu default) | paranoid 2 |
| --- | --- | --- |
| native, unprivileged user | permission denied | **available** |
| container, default capabilities | permission denied | permission denied (seccomp) |
| container, `--security-opt seccomp=unconfined` | permission denied | available |
| container, `--cap-add PERFMON` | **available** | not probed |

Docker's default seccomp profile allows `perf_event_open` only with
`CAP_PERFMON` or `CAP_SYS_ADMIN`. `CAP_PERFMON` also passes the paranoid
check. So the documented container setup is `--cap-add PERFMON` and needs no
host change. Native Linux needs `kernel.perf_event_paranoid` of 2 or less
for an unprivileged user. Heaptrack's launch mode needs no privilege in any
environment.

**perf event and unwinding.** `cpu-clock:u` is a software event: it needs no
hardware counter, so it works on the VM. `:u` limits the samples to user
space, which paranoid 2 allows. With `-O2` and no frame pointers,
`--call-graph fp` gave no source caller for any hot function. DWARF
unwinding with an 8192-byte stack copy gave the source caller for every hot
sample, through the Python interpreter and libffi too. DWARF costs data:
about 23 MB for about 2700 samples (8.5 KB per sample), against 0.25 MB with
frame pointers.

**Module identity.** perf 6.1 (container) with its default options reads
build IDs from the files when the recording ends. The FMU library is then
gone with its Run directory, so that capture has no build ID and no symbols
for the FMU, also after a later build-ID cache entry. With `--buildid-mmap`
the kernel puts the build ID in each mmap event, and perf does not fill its
build-ID cache. Adding the built FMU library (same build ID) to the cache
then resolves the samples, with perf 6.1 and 6.17. perf 6.17 (native) with
its default options recorded the FMU build ID and resolved the FMU with no
explicit step. The behavior depends on the perf version, so a production
capture must not depend on the default.

**Debug files.** perf and Heaptrack both found a matching debug file beside
the stripped library through `.gnu_debuglink`. Both refused the mismatched
file with the same name. The samples and allocations stay in the evidence
with an unknown function. Without a debug file, perf names no `static`
function of the stripped library: its samples stay in the module.

**Clock mapping (perf).** With `-k monotonic`, sample timestamps are host
`CLOCK_MONOTONIC`, the clock of the runner's `steady_clock` and of the
sidecar. In both environments every hot sample (about 2500 per proof) was
inside one of its own function's intervals in the same process. None was
outside, and none was inside the interval of another function. The Clock
shim does not change perf timestamps: they come from the kernel. A shimmed
participant's own clock readings are Virtual time, so host intervals for a
Process Step must come from the runner, not from the participant.

**Heaptrack timestamps.** Heaptrack writes milliseconds since its tracker
started, every 10 ms, with no absolute time. They cannot be joined to host
Activation intervals with a validated mapping. In a shimmed participant they
are Virtual time minus a host start time, so they are negative (an unsigned
value above 2^63) and have one value per Step.

**Heaptrack in a shimmed Process participant.** Four findings:

1. Heaptrack 1.5 (native) stopped the Python participant at start with
   `OSError: [Errno 38] Function not implemented` when the shim's sleep
   policy was `reject`, the builder default. With `immediate` it ran.
   Heaptrack 1.4 (container) ran with both policies. Error 38 is the
   `ENOSYS` that the shim's `reject` policy gives a sleep call, and
   Heaptrack's own threads sleep. The proof does not show the exact call.
2. Heaptrack's timer thread sleeps 10 ms between records. The shim returns
   at once, so the thread runs without a stop. It wrote 1.4 to 1.8 million
   timestamp records (36 to 49 MB) in a 1 s Run, against about 280
   (0.4 MB) for the runner scope.
3. Heaptrack removes `LD_PRELOAD` from its process environment. The
   participant's children then start without the Clock shim, and the
   child's clock reading in the Recording changes.
4. The allocation counts were exact where the Run completed.

**Late interpretation.** Raw Heaptrack data interpreted after the Run lost
the FMU function names: the library was gone. Every allocation stayed in the
data. Live interpretation, before the kernel removes the Run directory,
resolved them.

## Observations

Single Runs on a shared CI VM. They are not gates, not benchmarks and not a
prediction for another host.

| Run | container wall ms | native wall ms | capture bytes |
| --- | --- | --- | --- |
| ordinary | 2736 | 2791 | Recording 28039 |
| perf-dwarf | 3022 | 2838 | perf.data 22.9 MB (container), 23.2 MB (native) |
| perf-fp | 3018 | 2823 | perf.data 0.26 MB, 0.25 MB |
| heaptrack-runner | 2721 | 2819 | 0.36 MB |
| heaptrack-fmu | 2874 | 2853 | 1.0 MB, 0.7 MB |
| heaptrack-shimmed-process | 5122 | 203 (failed) | 48.6 MB, 0.2 MB |
| heaptrack-shimmed-process-sleep-immediate | 4383 | 3234 | 42.9 MB, 35.8 MB |

perf with DWARF writes about 8.5 KB per sample: at 999 Hz that is about
8.5 MB per second for each busy thread. A production capture needs a
storage limit that follows from this rate.

## Recommendations for #315 to #317

| Subject | Recommendation |
| --- | --- |
| launch strategy | perf: one `perf record` that starts `sil-run` (inherit covers every descendant), in the same PID namespace as the Run. Heaptrack: an exec-preserving preload adapter for one process; the interpreter is started by the profiling command before the Run, outside the Run's process group, on a FIFO |
| event and unwinding | `cpu-clock:u` at an explicit frequency (999 Hz here), `--call-graph dwarf` with an explicit stack size (8192 here), `--buildid-mmap`; frame pointers only as a recorded fallback for builds that have them |
| clock mapping | `perf record -k monotonic`: sample time is host `CLOCK_MONOTONIC`, the same clock as the runner's `steady_clock`, so no mapping is necessary. Heaptrack: correlation unavailable |
| export and normalization | `perf script -F comm,pid,tid,time,ip,sym,dso --ns` and `-F ip,sym,srcline`, after `perf buildid-cache -a` for each module that the Run directory held; Heaptrack's interpreted text format (file version 3), read directly. Keep `perf.data` and the Heaptrack file as the raw evidence |
| packaging boundary | SiL does not ship perf or Heaptrack. They are host tools: perf from the package of the running kernel, Heaptrack from the distribution. Record both versions, because behavior differs between versions (build IDs, the shim failure). A container image adds them; the container needs `CAP_PERFMON` |

## Requirements for #313

The spec must decide these before #315 and #317 start:

1. **Heaptrack in a shimmed Process participant.** It is not qualified. It
   can stop the participant (Heaptrack 1.5 with sleep `reject`), it removes
   the shim from the participant's children and so changes the Recording,
   and its timer writes without bound. The spec says that a named Process
   participant is a heap scope. Either reject a shimmed participant as a
   scope, or keep the scope and state these limits. A fix in the shim or a
   Heaptrack option is outside this proof.
2. **Heaptrack and Activation correlation.** The timestamps have no
   validated mapping to host time, and the shim makes them Virtual time.
   Per-Step allocation history is unavailable. This is the fallback that the
   spec already names.
3. **Process-scope launch.** The proof put the adapter into the Manifest
   command, which changes the Manifest bytes. The spec does not allow this
   in production. #317 needs a launch adapter that the runner applies at the
   Run boundary to one participant's exec. That is a generic lifecycle hook
   in the kernel, within the limits of the spec.
4. **Modules in the Run directory.** perf needs `--buildid-mmap` (or perf
   6.17 or later) and an explicit copy of each module from the Run directory,
   for example from the FMU archive. Heaptrack must interpret live, while
   the Run directory exists.

## Evidence

[`evidence/`](evidence/) holds the retained output of workflow run
[39219821315](https://github.com/Stevie1704/sil/actions/runs/39219821315)
at commit `4e0e651`:

| Path | Content |
| --- | --- |
| `native/probe-*/probe.json`, `container/probe-*/probe.json` | host facts and probe outcomes; `docker-options.txt` gives the options of each container |
| `native/run-paranoid-2/`, `container/run-cap-perfmon/` | `results.json` (host, versions, settings, artifact digests and build IDs, every Run, every check and finding), `results.md`, the Manifests |
| `*/runs/<run>/` | stderr of each Run and interpreter; for perf, `buildid-list.txt`, `mmap-events.txt` and `report-stats.txt` |
| `container/image-id.txt` | the image the container proof used |
| `host-paranoid-before.txt` | `perf_event_paranoid` before the host change |
| `digests.txt` | SHA-256 and size of every file in the workflow artifact |

`perf.data`, the Heaptrack data, the `perf script` listings and the
Recordings stay in the workflow artifact. They are large, and raw stack
copies hold process memory. Their digests are in `digests.txt`.
