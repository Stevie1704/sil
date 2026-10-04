# Running simulations

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Run contract

One run = `sil-run manifest.json -o out.mcap`:

- **Kernel** (C++20): central virtual-time master, two-tier scheduling
  (periodic tasks for native code, `step(t, Δt)` for opaque vECUs), typed
  pub/sub channels with explicit per-channel latency (default: unit delay —
  in-slot execution order cannot change outputs).
- **Native participants**: shared libraries against a small stable C ABI
  ([include/sil/participant.h](../include/sil/participant.h)), loaded via the
  manifest.
- **Out-of-process participants**: any executable speaking a JSON-lines
  step protocol on stdin/stdout; `sil.participant` provides the Python side.
  The normative line and payload contract is in
  [docs/step-protocol.md](step-protocol.md).
- **Recording**: uncompressed MCAP, virtual timestamps only, manifest hash
  embedded; bit-diff of two runs = determinism check.
- **Manifest**: canonical JSON, SHA-256 hashed, the single execution input.
  Built and validated with the `sil.manifest` Python builder.
- **Test API**: tests are scheduled participants (`sil.testing` +
  `sil.participant`) run under pytest; an in-simulation assertion failure
  aborts the run non-zero and fails the pytest test with that message.
- **Determinism check**: `sil-check manifest.json --runner build/sil-run`
  runs twice and bit-compares (exit 3 on violation). Enforced in CI.
- **Declared memory footprint**: `python -m sil.footprint manifest.json` reports
  the worst-case payload memory a manifest promises, from route capacities and
  arena slot counts alone — no run, no benchmark. It also names any route that
  declares no capacity, whose worst case is unbounded.

Exit codes: `0` ok, `1` run/test failure, `2` Manifest error, `3` determinism
violation (sil-check).

For a bounded CI wait, add the optional run-boundary guard:

```sh
sil-run manifest.json --participant-timeout-ms 5000 -o out.mcap
```

The value is a positive wall-clock duration in milliseconds. It applies
independently to each Process participant's complete `ready` and `step_done`
response wait. A missed deadline is a Run failure (exit 1), not a Manifest
change: the option is absent from the Manifest and its hash. Omitting it keeps
the existing unlimited-wait behavior.

The determinism check takes the same option and hands it to both of the runs
it compares, so a stalled participant fails the check instead of hanging it:

```sh
sil-check manifest.json --runner build/sil-run --participant-timeout-ms 5000
```

The check then reports the run's own exit code and diagnostic — a missed
deadline is a run failure (exit 1), not a determinism violation. The
recorded bytes of a run that answers in time are the same either way, so a
bounded check and an unbounded one report the same digest.

The pytest helper takes the same option as a keyword. Install the `sil` wheel
into the test environment, then give the helper the runner path:

```python
from sil.testing import run_simulation

def test_regression(tmp_path):
    result = run_simulation(
        build_manifest(),               # your sil.manifest.Manifest
        runner="/opt/sil/bin/sil-run",
        workdir=tmp_path,
        participant_timeout_ms=5000,
    )
    assert result.messages("ticks")
```

The helper forwards the value unchanged as `--participant-timeout-ms`. It
accepts only an `int` from 1 to 2^63−1 and raises `TypeError` or `ValueError`
before it starts the run. A missed deadline raises `RunFailure` with
`exit_code` 1 and the runner diagnostic as its message. The runner stops the
participant and its descendants at Run shutdown, as for a direct `sil-run`.
When you omit the keyword, the wait stays unlimited and the Manifest hash, exit
codes, and Recording bytes do not change.

The deadline is not a bound on the whole job. It covers only the response wait
of each Process participant. Native participants run in the runner process, so
a Native callback that does not return is not bounded by this option. Give the
CI job its own overall timeout (for example `timeout-minutes` in GitHub
Actions). That timeout bounds this case and all other waits.

### Process participant descendants

Each Process participant becomes the leader of its own process group before it
starts. At Run shutdown, the kernel gives the group the normal SIGTERM grace
period and escalates the group to SIGKILL if needed, including when the direct
child has already exited. This bounds the lifetime of cooperative descendants
and releases the participant's Run working directory, Arenas, and protocol
resources with the Run.

The group is a lifetime boundary, not a sandbox: it adds no CPU or memory
quota, syscall filter, namespace, or protection against a descendant that
deliberately escapes the group. Use the [container Run](container.md#run-one-manifest-in-a-linux-container)
(issue #115) for stronger isolation.

SIGINT, SIGHUP, and SIGTERM interrupt the Run through the same failure and
group-cleanup path, so terminal or service shutdown does not orphan
participants.

The runner also has independent Step-protocol resource guards:

```sh
sil-run manifest.json \
  --max-protocol-line-bytes 16777216 \
  --max-step-output-messages 1024 \
  --max-step-inline-payload-bytes 67108864 \
  -o out.mcap
```

These positive `size_t` values default to 16 MiB per response line, 1,024
output Messages per Step, and 64 MiB of decoded inline payload per Step. They
are run-boundary arguments, not Manifest data: a Run that stays within them
has the same Manifest hash and Recording bytes. An over-limit Process
participant causes a Run failure (exit 1).

`--max-protocol-line-bytes` is the one to turn down for a tighter memory
ceiling. It bounds the read buffer exactly, but the kernel parses a line that
stays inside it before it can count that line's outputs, and the kernel's
peak resident memory runs to 13-16 times the line limit in the worst case. The inline
payload of a Step is capped at three quarters of the line limit by base64
expansion, so `--max-step-inline-payload-bytes` only bites once the line limit
is raised. [docs/step-protocol.md](step-protocol.md) has the detail.

## Virtual clock shim for opaque POSIX vECUs

An out-of-process participant is expected to derive time from the `step(t, Δt)`
protocol and never read the wall clock. Opaque binaries you cannot change often
break that rule — they call `clock_gettime`, `gettimeofday`, `time`, or sleep.
The **clock shim** makes such a participant deterministic without touching it:
preload a small library that answers every POSIX wall-clock read from virtual
time.

Opt a process participant in per participant via the manifest `shim` flag, and
set the run's realtime `epoch_ns` (calendar time, ns since 1970) that
realtime-class reads are offset from:

```python
m = Manifest(duration_ns=30_000_000, epoch_ns=1_700_000_000_000_000_000)
m.add_channel("readings", schema="toy.Counter")
m.add_process(
    "vecu",
    command=[sys.executable, "vecu.py"],
    step_period_ns=10_000_000,
    publishes=["readings"],
    shim=True,           # this participant sees virtual time; others do not
    sleep="reject",      # default: fail sleeps instead of faking them
)
```

Inside a shimmed child, per step at virtual time `t`:

- monotonic-class reads (`CLOCK_MONOTONIC`, `CLOCK_BOOTTIME` and their raw,
  coarse and approximate variants) return `t` — nanoseconds from run start;
- realtime-class reads (`CLOCK_REALTIME` and its variants, `gettimeofday`,
  `time`) return `epoch_ns + t`;
- `clock_getres` reports 1 ns for those IDs;
- **every other clock ID passes through to the real libc** — the CPU-time IDs
  (`CLOCK_PROCESS_CPUTIME_ID`, `CLOCK_THREAD_CPUTIME_ID`), which measure
  consumed CPU rather than elapsed wall time, and any ID the shim does not
  name, which has no known class to answer from. A participant that profiles
  itself with a CPU clock reads real CPU time and is not deterministic;
- **virtualized reads are frozen within a step** — every such read during one
  step returns the same value; time advances only between steps.

Because a step's time is frozen and the kernel waits for the step response, no
sleep can wait for the clock to move — it would deadlock against the only thing
that could move it. The `sleep` policy decides what a sleep call says instead:

| `sleep` | The sleep family does | Use it for |
| --- | --- | --- |
| `"reject"` (default) | fails with `ENOSYS` | new manifests: a retry loop ends instead of spinning the CPU for the rest of the step |
| `"immediate"` | returns success, as if the whole duration had elapsed | code that treats a failed sleep as fatal, and manifests written before the policy existed |

`sleep()` is the exception: POSIX gives it no error return, so `reject` reports
the full duration as unslept and sets `ENOSYS` for callers that check it. An
older manifest with no `sleep` field keeps `"immediate"`, so its hash and its
behavior are both unchanged.

The shim applies **per participant**: a shimmed vECU and ordinary unshimmed
participants coexist in one manifest and one run. `shim`, `sleep`, `threads`
and `epoch_ns` are all hashed into the manifest (they change output), so shimmed
and unshimmed variants of a run never collide under hash-based caching, and two
shimmed runs
started at different wall-clock times still produce bit-identical MCAPs — run
`sil-check` on a shimmed manifest to prove it.

**Boundaries (out of scope, documented):** statically linked binaries and code
that reads the clock via direct syscalls or the vDSO bypass the preload and are
not virtualized. The shim answers only the calls listed above; the rest of the
participant must still behave deterministically.

The shim also does not cover **native participants**. The runner preloads the
shim only into process participant children. A native library runs in the
kernel's own process, so its clock reads and sleeps use real time. Do not
preload the shim into the runner: it freezes the runner's own deadline clock.
[When the Clock shim applies](library.md#when-the-clock-shim-applies) gives the
reason and the alternative.

### Thread creation diagnostics

A vECU can start worker, timer or condition-variable threads. Under frozen
Virtual time, these threads often fail:

- A timer thread fails on its sleeps, or spins on them.
- A worker that finishes after the Step response publishes nothing, or
  publishes in a later Step.

A failed determinism check tells you that the Run is different. It does not
tell you the cause. The `threads` policy makes thread creation by a shimmed
participant visible:

```python
m.add_process(
    "vecu",
    command=["./vecu"],
    step_period_ns=10_000_000,
    shim=True,
    threads="report",    # default "allow"
)
```

| `threads` | `pthread_create` does | Use it for |
| --- | --- | --- |
| `"allow"` (default) | starts the thread; nothing is reported | normal runs |
| `"report"` | starts the thread; the shim writes one stderr line for each successful creation | finding out whether, and at which Step, a vECU starts threads |
| `"reject"` | returns `EAGAIN` and does not start the thread | checking whether a vECU runs without its threads |

A `report` line has this form, with the Virtual time at which the call began
(`0` before the first Step, for example during initialization):

```text
sil clock shim: thread created at virtual time 10000000 ns
```

The line goes to the participant's stderr, never to the protocol stdout. It has
no pointers, OS thread IDs or wall time. The lines of threads created
concurrently can come in any order.

`reject` returns `EAGAIN` as the `pthread_create` return code and leaves
`errno` alone. It does not stop the Run: the participant decides what to do
with the error. A participant that retries forever is still stopped by the
Response deadline.

`threads` is valid only on a Process participant with `shim` enabled; on any
other participant, without the shim, or with another value it is a Manifest
error (exit 2). The builder omits the default `"allow"`, so a Manifest written
before the field existed keeps its bytes and hash. A hand-written explicit
`"allow"` is accepted and hashed as written.

**Boundaries:** the policy observes only the `pthread_create` calls that the
preload intercepts, not every thread. These are outside its coverage:

- statically linked binaries;
- threads started with a direct `clone` system call or another call that does
  not go through `pthread_create`;
- threads started before the shim loads, for example from another library's
  load-time constructor;
- what a thread does after it starts: a `report` line names only its creation;
- how an opaque runtime uses its threads.

The policy is detection only. It does not count threads, serialize or schedule
them, diagnose races, or make a participant deterministic. A `report` line does
not prove that the thread caused a determinism violation. Internal determinism
stays the participant author's responsibility ([DESIGN.md](../DESIGN.md)).

## Shared-memory channel transport

Every channel payload is base64-encoded inside the JSON step line by default
(inline transport). For megabyte-class payloads at sensor rate — a camera frame,
a point cloud — the ~33% base64 penalty plus per-message encode/decode dominates
the run. Declare `transport: "shm"` on such a channel and its payload crosses the
kernel↔process boundary through a per-channel shared-memory arena instead:

```python
m.add_channel("frames", schema="sensor.Frame", transport="shm", slots=2)
```

The kernel sizes and maps one arena per such channel from `byte_size * slots`
at startup and hands the process participant its path. Publishing writes each
payload into its indexed slot and the step line carries the slot plus a
freshness marker; the participant reads the bytes directly. The builder always
emits `slots` for `shm` Channels and defaults it to 2: that covers the measured
two-Message burst at the cost of one additional schema-sized payload per Arena.
A deeper burst falls back to inline only after filling the declared slots — a
detail of delivery that never changes what the participant sees. **The
transport choice never leaks into
participant code** — `on_step` still sees the same field-dict (scalars) and
`bytes`/`list` (array fields) whether the channel is inline or shm. Flip the flag
and rebuild nothing.

`transport` and `slots` are hashed (inline, the default, is omitted; absent
`slots` means the legacy single-slot behavior). A run that cannot create or map
its arena fails at
**startup with exit 2** (a Manifest/environment problem), distinct from a test
failure's exit 1. Native participants are unaffected — they stay on the existing
pointer-based C ABI data plane and need no rebuild.

**Boundaries (out of scope, per PRD):** true zero-copy into user code (a
participant-facing copy stays), native-participant shm beyond the pointer ABI,
cross-machine transport, compression, and arena-size/backpressure tuning.

## Bounded subscriber routes

Every subscriber route authored by the Python builder has an explicit finite
Message capacity. Declare it with `SubscriberRoute`; a full route aborts the Run
by default, while a non-critical subscriber can explicitly discard only its
newest attempted delivery:

```python
from sil import Manifest, SubscriberRoute

m.add_process(
    "detector",
    command=["./detector"],
    step_period_ns=10_000_000,
    subscribes=[SubscriberRoute("frames", capacity=4)],
)
m.add_process(
    "preview",
    command=["./preview"],
    step_period_ns=20_000_000,
    subscribes=[
        SubscriberRoute("frames", capacity=2, overflow="drop_newest")
    ],
)
```

Capacity and overflow policy are part of the canonical Manifest bytes and hash.
Choose capacity from the route's declared burst and drain behavior; the builder
does not guess a workload-specific number. A Manifest route with both fields
absent is unbounded: the loader accepts either a pre-#75 string entry such as
`"subscribes":["frames"]` or `{"channel":"frames"}`. Supplying only one of
`capacity` and `overflow` is invalid. This preserves existing behavior and
byte-identical hashes. Blocking overflow is invalid because the sequential
scheduler cannot activate a consumer while stopped inside its publisher's call.
