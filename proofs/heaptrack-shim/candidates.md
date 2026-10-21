# Heaptrack and the Clock shim: candidate source analysis

This is source analysis for #330, not a report of successful measurements.
The [proof README](README.md) owns the runtime matrix, platform facts and
decision. A source rejection below rejects the candidate as sufficient to
remove the existing restriction, rather than claiming that no redesigned
collector could work.

The target context for these rejections is Linux x86-64 with glibc, using the
pinned distribution Heaptrack versions in the proof. Attach, caller/thread
exemptions and eBPF were **not runtime-tested** in this investigation. Only
plain preload and target-resident preload preservation have runtime evidence.

## Source pins

Upstream tags were resolved to commits before reading the source:

| Project | Version | Commit |
| --- | --- | --- |
| Heaptrack | 1.5.0 | `b54e92e88b0895f910a5b107f499d1155a086b80` |
| Heaptrack | 1.4.0 | `9e5514ecbbf56c8d5307717e4d1c70cc58dc27fe` |
| bpftrace | 0.21.0 | `add9d3a3327cb9b084823c527be36e1280af4e0f` |
| BCC | 0.29.1 | `eb8ede2d70b17350757f2570ef76ea4c2e1dbff8` |

These are source pins; the runtime proof reports its installed package versions
separately. In particular, a bpftrace source capability is not evidence that
the proof host or its container can load a BPF program.

## Attach after startup

**Source-rejected as a complete remedy; no runtime attach test.**
Heaptrack's attach script uses GDB to load `libheaptrack_inject.so`, call
`heaptrack_inject(output)` and detach. It does not attach before the process
starts. Allocations before hook installation cannot be recovered from those
hooks. Starting a short Process participant and then discovering its PID adds
a startup race; pausing at an agreed boundary can bound the missing interval
but cannot recover dynamic-loader or earlier initialization allocations.
See the pinned [1.5 launch/attach script](https://github.com/KDE/heaptrack/blob/b54e92e88b0895f910a5b107f499d1155a086b80/src/track/heaptrack.sh.cmake#L326-L359)
and [1.4 script](https://github.com/KDE/heaptrack/blob/9e5514ecbbf56c8d5307717e4d1c70cc58dc27fe/src/track/heaptrack.sh.cmake).

Injection replaces allocator relocation targets and initializes the same
`heaptrack_init` engine used by preload. It avoids the preload initializer's
environment cleanup: the injected initializer itself does not unset
`LD_PRELOAD`. That addresses one cause of descendant shim loss, but not the
shared timer's clock and sleep interference. Injection also changes executable
process state, including making relocation pages writable; it is not a fully
out-of-process collector. Sources: [1.5 injection](https://github.com/KDE/heaptrack/blob/b54e92e88b0895f910a5b107f499d1155a086b80/src/track/heaptrack_inject.cpp),
[1.4 injection](https://github.com/KDE/heaptrack/blob/9e5514ecbbf56c8d5307717e4d1c70cc58dc27fe/src/track/heaptrack_inject.cpp).

## Exempt a profiler-owned thread or caller

**Source-rejected for an unmodified profiler and an unchanged shim contract.**
Both versions use `std::chrono::steady_clock` for elapsed timestamps and a
timer thread with `std::this_thread::sleep_for(10 ms)`. Both also sleep for
1 microsecond when contending for the output lock. Allocation hooks acquire
that lock on the participant's own thread. Initialization takes the start
timestamp on the initializing thread; shutdown writes a timestamp on the
shutting-down thread. Therefore exempting only the timer thread leaves
profiler sleeps and clock reads on participant threads. These statements are
source predictions about call paths, not measured sleep rates or a portable
claim about which libc symbol each C++ library uses. Sources:
[1.5 engine](https://github.com/KDE/heaptrack/blob/b54e92e88b0895f910a5b107f499d1155a086b80/src/track/libheaptrack.cpp),
[1.4 engine](https://github.com/KDE/heaptrack/blob/9e5514ecbbf56c8d5307717e4d1c70cc58dc27fe/src/track/libheaptrack.cpp).

The shim currently answers mapped-region wall-clock calls without classifying
the caller. Its `pthread_create` interposer also applies the configured
thread policy to every creation. A profiler timer is consequently subject to
`threads="reject"`, too. See the repository's
[shim](../../shim/clock_shim.c) and [clock region](../../include/sil/clock_region.h).

A caller-based exemption would need extra work at the clock/sleep interposer:
for example resolving a return address or examining a stack. A direct caller
may be libstdc++ when it implements `steady_clock::now()`; treating every
libstdc++ call as profiler-owned would also exempt participant C++ clocks.
A whole-thread flag cannot distinguish profiler work from subsequent
participant work on an allocation thread. The pinned profiler provides no
shim exemption API; its internal recursion flag is for allocation tracing.
Thus none of these is a drop-in solution without instrumentation on the
participant call path or a deliberately specified new boundary. The exact
cost and correctness of such classification are **unmeasured**.

A separate profiler build using explicit host clock/sleep operations could be
investigated later. That would need its own versioned integration and descendant
environment policy; it is not evidence that the distribution packages compose.

### Timestamp origin and initialization order

The source does **not** prove that every shimmed Heaptrack timestamp is purely
Virtual time. `startTime()` caches `steady_clock::now()` on the first
`heaptrack_init()`; `elapsedTime()` subtracts that cached value from a fresh
read, and `writeTimestamp()` casts milliseconds to `size_t`. Meanwhile the
shim falls back to libc until its constructor maps the region. An early
allocation can therefore initialize Heaptrack with a host-clock origin before
the shim constructor, followed by virtual-clock reads after mapping. That
mixed-origin subtraction can become negative and appear as a large unsigned
timestamp. This is a source-derived possibility, not a measured outcome.
If initialization occurs after mapping, both endpoints instead use Virtual
time. Attach after mapping also starts with a virtual origin. In every case,
a validated host-time mapping must be demonstrated rather than assumed.
Sources: the pinned engines above and the repository shim.

For a concrete C++ implementation, GCC 12.2's
[chrono implementation](https://github.com/gcc-mirror/gcc/blob/2ee5e4300186a92ad73f1a1a64cb918dc76c8d67/libstdc%2B%2B-v3/src/c%2B%2B11/chrono.cc#L70-L86)
uses `CLOCK_MONOTONIC` through `clock_gettime` in its normal configured path,
with a separate direct-syscall build option. This both explains why a return
address may identify libstdc++ and limits the inference: the actual installed
library build and observed timestamp range belong in the proof facts.

## Restore the shim in descendants

**Source-rejected as a shell-adapter-only remedy.** Combining the current launch
mechanisms would give runner → shimmed adapter → target (the CLI currently
rejects that combination). The runner overwrites `LD_PRELOAD` with
the shim; the heap adapter prepends Heaptrack to the inherited value and
executes the target. In that target, Heaptrack lazily initializes through an
allocation hook and unsets the entire `LD_PRELOAD` value, not only its own
entry. An export in the shell before `exec` therefore cannot run after that
cleanup. Sources: [runner environment injection](../../kernel/src/process_participant.cpp),
[heap launch adapter](../../python/src/sil/profile_heap_run.py),
[1.5 preload initializer](https://github.com/KDE/heaptrack/blob/b54e92e88b0895f910a5b107f499d1155a086b80/src/track/heaptrack_preload.cpp#L163-L214),
[1.4 preload initializer](https://github.com/KDE/heaptrack/blob/9e5514ecbbf56c8d5307717e4d1c70cc58dc27fe/src/track/heaptrack_preload.cpp).

Restoring only the shim inside the target after initialization could preserve
future exec descendants, but requires target-resident code or a profiler
patch with a reliable initialization order. Constructor ordering alone is
insufficient to prove this because allocator interception can initialize
Heaptrack before `main`. Keeping Heaptrack itself in descendants also needs
separate output ownership. Neither environment repair nor swapping preload
order solves the clock/sleep problem. These are source-based limits, not a
measurement of a custom restoration interposer.

## Out-of-process allocation tracing

**Source-based defer; no runtime eBPF availability or allocation capture test.**
BCC's pinned `memleak` attaches libc allocation entry/return probes for a
specific PID. It records successful addresses, sizes, kernel-clock timestamps
and user stack IDs in bounded maps, deleting tracked allocations on frees.
Its normal output is outstanding allocations, not Heaptrack's complete
allocation history. `-c` starts the child before compiling/loading and
attaching probes, so that mode has a startup gap too. See
[memleak source](https://github.com/iovisor/bcc/blob/eb8ede2d70b17350757f2570ef76ea4c2e1dbff8/tools/memleak.py).

Its `bpf_ktime_get_ns()` timestamp is host monotonic time, excluding suspend;
the userspace Clock shim cannot intercept it. BCC documents stack-map capacity
and stack-ID capture separately from symbol lookup. See
[BCC reference](https://github.com/iovisor/bcc/blob/eb8ede2d70b17350757f2570ef76ea4c2e1dbff8/docs/reference_guide.md#3-bpf_ktime_get_ns).

bpftrace offers explicit `nsecs(monotonic)` timestamps and userspace stacks.
Its manual warns that the stack examples require frame pointers and that
ASLR/dynamically loaded symbols affect symbol resolution. A working probe
does not establish useful stacks for arbitrary optimized opaque participants.
PID-scoped probes do not establish coverage of future exec descendants.
The manual's stopped-child startup guarantee is described for USDT; it should
not be generalized into evidence of complete allocator startup coverage.
See the [pinned manual](https://github.com/bpftrace/bpftrace/blob/add9d3a3327cb9b084823c527be36e1280af4e0f/man/adoc/bpftrace.adoc).

Availability depends on the host kernel, permitted BPF/perf operations,
trace interfaces and the tool build; being root inside a container alone
does not establish those conditions. BCC lists required kernel configuration,
headers and a privileged-container example with host mounts; these differ
from the ordinary profiling container's `CAP_PERFMON` setup. See
[BCC installation requirements](https://github.com/iovisor/bcc/blob/eb8ede2d70b17350757f2570ef76ea4c2e1dbff8/INSTALL.md#kernel-configuration).

Allocator-symbol probes cover calls reaching the selected symbols, not all
language-runtime objects or custom arenas. A general collector must define
which allocators count, suppress nested double counting, handle unsuccessful
reallocations, track PID/address identity across descendants and report map
or event loss. Every-allocation probes still execute instrumentation per
call; BCC offers sampling to reduce overhead. Sampling changes count semantics.
No numerical overhead or stack-quality claim is made without a successful
target-machine capture. Bounded aggregate maps can avoid a large event log,
but change report semantics; a full event stream needs a measured growth and
loss budget. Source-informed design requirements are not a replacement for
the runtime equality and descendant checks in #330.

## Keep the rejection

**Conclusion: defer support and retain the restriction.** Retain the Phase 1 guard and use
runner heap profiling for Native participants, or an explicitly unshimmed
Process variant when that is the intended experiment. Runner scope does not
make shimmed Process allocations visible. The tested preservation candidate
addresses inheritance only; the source analysis shows that attach does not
remove the timer and clock conflict, an exemption changes the shim boundary,
and eBPF needs a new collector with independently established coverage and
availability. These findings do not justify relaxing #317. This investigation
has no build conclusion and proposes no implementation scope expansion.
