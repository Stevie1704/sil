# Selected function tracing inside Native Activations (#320)

This is a Phase 2 feasibility experiment under #313. It adds no supported
command, report schema, Manifest field, Native ABI or Step protocol. Probe
ownership stays with this proof. Shipped example sources and kernel sources
are unchanged; `sources/` in each capture holds the actual instrumented copy.
ADR 0004 continues to apply. A supported collector would need a separate
specification and an explicit compatibility/ownership decision.

## Question and experiment

The workload is `examples/adas-reference/adas_reference.c`, its Native adapter
and `reference_manifest` builder. Twenty controller Activations per maneuver
run at a 10 ms Period. The proof generates `one` and `eight`: respectively
one and eight active radar objects and camera objects, published every Period.
Camera object i confirms radar object i; distinct pairs are 10 m apart. Thus
each Activation should enter these source functions in this order:

```text
check_object × N (radar), check_object × N (camera)
select_lead
  for radar object i = 0 .. N-1
    confirmed
      confirms × (i+1), stopping at its matching camera object
```

The independent counts are `(check_object, confirmed, confirms, select_lead)`
= `(2, 1, 1, 1)` for `one` and `(16, 8, 36, 1)` for `eight`.
`explicit-*/calls.json` retains every call in entry order, nesting depth,
entry/exit host timestamps and elapsed host nanoseconds for every Activation.
It includes Participant, Task, Activation index and Virtual time from the
runner's `observations.jsonl`. These are measurements of instrumented host
code, including observer cost, preemption and waiting. They are neither CPU
execution durations nor target timing nor WCET. Instrumentation is function
entry/exit evidence, not full instruction trace. No custom payload events or
variable history are collected.

## Repeat and evidence

Real qualification must run on GitHub Actions `ubuntu-24.04`, Linux
x86-64/glibc. macOS/arm64 runs only the offline tests. The workflow
`.github/workflows/proof-function-tracing.yml` runs for proof changes on PRs
and via workflow_dispatch, without changing host security policy.

```sh
# On ubuntu-24.04, after installing the workflow's packages and Python deps:
bash proofs/function-tracing/run-proof.sh \
  "$PWD/build/function-tracing-evidence" "$PWD/build/function-tracing-build"
# Offline parser tests on any supported development host:
.venv/bin/python -m pytest tests/test_function_tracing_proof.py
```

The workflow pins GCC 12.4.0 (`12.4.0-2ubuntu1~24.04.1`) and Clang/LLVM
18.1.3 (`1:18.1.3-1ubuntu1`) Ubuntu packages. Tool versions, relevant package
versions, glibc and kernel are retained in `evidence/report.json`; the complete
package inventory stays in the CI artifact. A missing pinned package
fails setup rather than silently switching compiler versions.
The workload always uses `-O2 -g -ffp-contract=off`, never fast math. This
differs from the GCC 12-only ADAS cost proof: the XRay candidate requires
Clang plus compiler-rt and a proof-only executable link against XRay.
Build commands, optimized inline remarks, symbol tables, DWARF, build IDs,
copied sources, binaries and SHA-256 digests accompany the capture. Do not
symbolize a different build or substitute debug files from the baseline.

The full artifact contains `results.json`, per-Run logs, diagnostics,
Recordings, raw event buffers and metadata, `calls.json`, XRay maps and
positive-control evidence. `digests.txt` hashes all captured files. Retained
results and the CI run identity belong in `evidence/` after qualification;
the workflow uploads full artifacts for 30 days. No local timing substitutes
for the Linux result.

## Event semantics, clock and join

Explicit scopes inject a single cleanup-guard macro into the four selected
functions in a copy. Entry records happen before the body; exit records
happen on each ordinary C return, including early returns. They describe
logical source calls even when GCC inlines the body. `*.inline.txt` and
`*.debug.txt` identify inline decisions for each actual build. Instrumentation
can change those decisions; baseline decisions cannot be assumed unchanged.

Each 16-byte little-endian record is `(uint64 ns, uint32 tid, uint16 function,
uint16 edge)`. Explicit IDs are 1 check_object, 2 confirmed, 3 confirms,
4 select_lead; edges are 0 entry, 1 return, 2 XRay tail exit. XRay numeric IDs
belong to its actual executable/map and must not be decoded with explicit
probe IDs. The XRay callback uses the same buffer and clock implementation;
it replaces the default TSC-based logger rather than inventing a conversion.

Both callbacks use `syscall(SYS_clock_gettime, CLOCK_MONOTONIC)` directly.
At load time, the probe brackets libc's CLOCK_MONOTONIC read between two
direct reads and retains all three timestamps. The decoder verifies this
bracket. The runner's Linux `std::chrono::steady_clock` uses CLOCK_MONOTONIC.
Every successfully joined event lies inside an actual runner observation.
Native participants run in the runner process; the Participant Clock shim
does not apply. The proof clears inherited LD_PRELOAD; its XRay observer
preload is not a clock shim. It makes no Process participant clock claim.

Joins require matching fresh Run PID, runner TID, clock, Native Task kind,
completed outcome and unique containment of both entry and exit events in
`[begin_ns, end_ns)`. An event exactly at the next begin belongs to the next
Activation; an exit at the prior end cannot establish a contained call.
Nested calls use a per-thread stack, preserving recursion and entry order.
Worker threads, failed/unclosed Activations, crossing calls and ambiguous
intervals stay unattributed. Any stream loss suppresses exact per-Activation
counts/durations. Saved evidence comes from one fresh supervised process;
joining unrelated files on a reused PID is unsupported.

## Coverage and limits

| Property | Explicit source scopes | LLVM XRay |
| --- | --- | --- |
| Build | GCC 12, C cleanup extension, proof-only copied sources | Clang/LLVM 18, compiler-rt, Linux x86-64 |
| Selection | Exactly four injected scopes; source-anchor mismatch fails | Attribute list selects four functions; retain actual sled map |
| Optimization | Logical inline calls remain observable; remarks record changed inlining | Only surviving machine functions have sleds; inlined source calls can disappear |
| Recursion / threads | Per-thread nesting decoder; contention drops instead of waiting | Executable positive control includes recursion and a worker thread |
| Early returns / tail exits | Cleanup executes before normal returns and inhibits a tail transfer if needed | Return and tail-exit callbacks are distinct |
| Exceptions / nonlocal exits | C workload has none; longjmp, cancellation and exceptions are unqualified | Positive control throws; retain raw imbalance, never synthesize a return on unwind |
| Loading | Observer linked into the copied Native DSO; constructor/destructor bound capture | Native DSO loaded after startup is the specific feasibility question; executable control separates a working runtime from DSO coverage |

XRay also builds a **separate** noinline variant at `-O2` to distinguish
missing sleds due to inlining from missing runtime registration of a loaded
library. This variant changes call boundaries and is not a timing proxy for
the unmodified optimized workload. The executable positive control is also
separate from ADAS measurements. Runtime patch status and maximum function ID
are retained even when no events occur; an empty capture does not mean no
calls ran. This compiler-rt build localizes its own API symbols, so
`xray_bridge.cpp` exports proof-owned wrappers from the executable. CMake's
project-include/deferred target source mechanism adds them only in the proof
build, with no source edit to the kernel. The Native DSO retries attachment
after loading. A missing bridge is an error, distinct from a working bridge
whose runtime has no registered DSO map.

The executable control uses a high default instruction threshold and disables
loop-based selection; its attribute list explicitly selects the three
`control_*` functions. That keeps unrelated C++ thread helpers out of the
control. The ADAS candidate uses the requested threshold of one. Runtime IDs
are resolved through the actual executable's address table and dynamic
symbols; the control checks nine recursive leaf entries/returns on two TIDs,
one tail-exit event and an unmatched throwing-function entry. Unwinding does
not become a synthesized return.

The observer preallocates 65,536 records (1 MiB) per process. A single try-lock
protects append order; contention/full capacity increments a dropped counter.
There is no retry, ring overwrite, allocation or I/O inside callbacks. The
loss Run lowers capacity to seven records, verifies partial status and keeps
an equal Recording. After the Run, two bounded regular-file writes save the
buffer and its metadata (fresh O_EXCL/O_NOFOLLOW paths). A short write or
missing footer is incomplete. Storage per capture is at most 1 MiB plus
1 KiB metadata; Activation observations have a 1 MiB limit plus their footer.
All Run subprocesses, including final flush, have a 60 s timeout; the CI job
has a 30-minute limit. Regular-file syscalls can wait on the filesystem; the
observer makes no hard real-time or uninterruptible-I/O guarantee. No reader
or collector pipe can backpressure an Activation. Offline inputs are limited
to 16 MiB per file. The experiment's finite matrix fixes total output growth.

`results.json` retains three fresh Runs per normal mode, wall duration and
event volume, with no timing/overhead thresholds. Compare ordinary,
diagnostics, explicit, Clang baseline, XRay and noinline-XRay observations;
baseline differences include compiler/runtime/launch costs and cannot be
subtracted as pure probe cost. Per-Activation elapsed durations give the
more local comparison; nested inclusive durations must not be summed as CPU
time. Every Recording must be byte-identical across modes/repeats, with
the same Manifest bytes and a 10 s Participant response timeout. Native
callbacks are synchronous and that timeout does not interrupt them; the
supervisor provides the whole-process limit. On workloads with Process
participants instrumentation could move a response across its deadline, so
equivalence here establishes no deadline-edge guarantee.

## Candidate decisions

[Linux qualification run 39466725936](https://github.com/Stevie1704/sil/actions/runs/39466725936)
passed for PR head `9718afe5a55f6178f4499b729d41fbe63ca9ae9a`; the actual
checkout merge revision is recorded in `evidence/report.json`.
The host was Ubuntu 24.04, Linux 6.17.0-1022-azure x86-64 and glibc 2.39.
[Retained evidence](evidence/README.md) includes raw events, observations,
selected compiler evidence, maps and consolidated results/build identities.
Derived summaries can be regenerated from the raw trace. Later commits add
documentation/evidence only.

**Explicit scopes: build, as a separately specified source-owner opt-in.**
All three captures had 2,640 events / 42,240 bytes, no loss and 40 completed
Activation joins. Counts and entry order matched the worked example above in
every Activation. The matching GCC binaries' DWARF records show all four
selected functions inlined, with and without scopes; their logical source
calls stayed observable. Selected compiler remarks and DWARF counts are kept
in `evidence/report.json`. Thus these are durations of
the actual instrumented scopes, not reconstructed durations of the baseline's
machine functions. See the [separate spec proposal](proposed-spec.md); it
requires source ownership, a separate API/contract decision and new tickets.
Nothing is shipped by this result.

First explicit Run, twenty Activations per maneuver (inclusive elapsed host
nanoseconds; medians across calls, with raw per-call values retained):

| Function | Calls per Activation, one / eight | Median ns, one / eight |
| --- | --- | --- |
| check_object | 2 / 16 | 740.5 / 702 |
| confirmed | 1 / 8 | 1,059 / 3,818 |
| confirms | 1 / 36 | 353.5 / 348 |
| select_lead | 1 / 1 | 1,773 / 31,033 |

Object count increases the nested search work; it does not imply a uniform
increase in the duration of every individual call. For example the eight-object
`confirms` maximum was 8,080 ns, although its median was 348 ns. No scheduler
or CPU-time cause is inferred from that elapsed-time outlier.

**LLVM XRay 18.1.3: defer for this loaded Native library.** Clang inlined
`check_object`, `confirmed` and `confirms`; only `select_lead` had surviving
sleds in the optimized DSO. The noinline variant had sleds for all four
functions. Both variants loaded and ran with equal Recordings, but all six
captures had zero events, zero registered runtime function IDs and patch
status NOT_INITIALIZED (0), including the retry after loading. This is
missing runtime registration for the DSO map, not proof that no calls ran.
The same bridge in the executable control successfully patched three mapped
functions and captured 21 events with no loss: nine recursive leaf calls on
two threads, one tail exit and a throwing entry with no matching return.
This qualifies the runtime/control path while demonstrating the Native DSO
boundary. The pinned runtime's
[initialization source](https://github.com/llvm/llvm-project/blob/llvmorg-18.1.3/compiler-rt/lib/xray/xray_init.cpp)
also explicitly leaves DSO maps unsupported. Upgrading LLVM, static-linking
the application into a new runner, or implementing map registration would
need a separate investigation; none answers this workload unchanged.

Observed median whole-Run wall time across three fresh Runs:

| Mode | Host ms | Raw function events / bytes per Run |
| --- | ---: | ---: |
| ordinary GCC | 7.457 | no collector |
| diagnostics GCC | 10.106 | no function collector |
| explicit GCC | 10.420 | 2,640 / 42,240 |
| Clang baseline with XRay runtime link | 12.183 | no function collector |
| XRay optimized DSO | 12.093 | 0 / 0 |
| XRay noinline DSO | 12.057 | 0 / 0 |

Whole-Run measurements include startup, Recording, flush and shutdown. The
first diagnostics/explicit Runs' median inclusive Activation durations were
1,951.5 / 5,645.5 ns for `one` and 7,160.5 / 50,795.5 ns for `eight`.
Those paired observations show substantial local perturbation despite the
small whole-Run difference. They establish no timing threshold or overhead
correction for an uninstrumented function. Unpatched XRay DSO measurements
are not measurements of active XRay tracing overhead.

The seven-record exhaustion Run retained 112 bytes and reported 2,633 dropped
events, with partial analysis and no exact Activation attribution. All 19
Runs, including loss and both XRay variants, had Recording SHA-256
`a5bd9ce5fdf45dcaf15286e2f424c3b3206ba20995eb2028d24518e18055a9fb`.
The PR stays draft and #320 stays open, as requested by the ticket.

Primary references: [LLVM XRay](https://llvm.org/docs/XRay.html),
[XRay API and sled implementation](https://github.com/llvm/llvm-project/tree/llvmorg-18.1.8/compiler-rt/lib/xray),
[GCC instrumentation semantics](https://gcc.gnu.org/onlinedocs/gcc/Instrumentation-Options.html).
