# SiL Framework

Deterministic software-in-the-loop kernel for ADAS/AD regression testing:
**deterministic scheduling + typed data routing**, nothing else in the core.
Same artifacts + same machine class ⇒ bit-identical recordings. See
[DESIGN.md](DESIGN.md) for the decision record.

Apache-2.0 licensed. Read [SUPPORT.md](SUPPORT.md) before you build a process
on SiL: it names the supported machine class, the exact bound on the
determinism guarantee, and the fact that SiL is not safety-qualified.

## Milestone 1 — walking skeleton + determinism proof

One run = `sil-run manifest.json -o out.mcap`:

- **Kernel** (C++20): central virtual-time master, two-tier scheduling
  (periodic tasks for native code, `step(t, Δt)` for opaque vECUs), typed
  pub/sub channels with explicit per-channel latency (default: unit delay —
  in-slot execution order cannot change outputs).
- **Native participants**: shared libraries against a small stable C ABI
  ([include/sil/participant.h](include/sil/participant.h)), loaded via the
  manifest.
- **Out-of-process participants**: any executable speaking a JSON-lines
  step protocol on stdin/stdout; `sil.participant` provides the Python side.
  The normative line and payload contract is in
  [docs/step-protocol.md](docs/step-protocol.md).
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
is raised. [docs/step-protocol.md](docs/step-protocol.md) has the detail.

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
participants coexist in one manifest and one run. `shim`, `sleep` and
`epoch_ns` are all hashed into the manifest (they change output), so shimmed
and unshimmed variants of a run never collide under hash-based caching, and two
shimmed runs
started at different wall-clock times still produce bit-identical MCAPs — run
`sil-check` on a shimmed manifest to prove it.

**Boundaries (out of scope, documented):** statically linked binaries and code
that reads the clock via direct syscalls or the vDSO bypass the preload and are
not virtualized.

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

## FMI 3.0 co-simulation importer

An FMU is a vendor model or vECU packaged to the FMI standard: one archive
containing a machine-readable model description and a shared library for each
target platform. SiL imports an FMU as an ordinary process participant. The
importer extracts it into the participant's kernel-owned working directory
during initialization and drives its FMI 3.0 co-simulation interface. The
extraction is dropped at shutdown and, either way, goes with the tree the
kernel removes after the Run. Channel schema field names map directly to the FMU's Float64 input and
output variables; Channel direction decides whether a field is written before
the FMU Step or published after it.

The FMU path is just a command argument, so it is part of the hashed Manifest:

```python
import sys

from sil import Manifest, SubscriberRoute

m = Manifest(duration_ns=100_000_000)
m.add_schemas({
    "fmu.In": {"fields": [{"name": "u", "type": "f64"}]},
    "fmu.Out": {"fields": [{"name": "y", "type": "f64"}]},
})
m.add_channel("fmu.In", schema="fmu.In")
m.add_channel("fmu.Out", schema="fmu.Out")
m.add_process(
    "fmu",
    command=[sys.executable, "-m", "sil.fmi", "models/fmu.fmu"],
    step_period_ns=10_000_000,
    subscribes=[SubscriberRoute("fmu.In", capacity=4)],
    publishes=["fmu.Out"],
)
```

[examples/fmu/](examples/fmu/) is that snippet as a Run you can execute:
`Feedthrough`, the Modelica Association's own Reference FMU, driven by one
stimulus participant. `examples/fmu/manifest.py` is the whole Run, and
`tests/test_example_fmu.py` imports that same file rather than restating it.

```sh
make example-fmu                                # run it, record it into build/
```

Because `Feedthrough` copies each input to the output of the same name, the
Recording shows the mapping round-trip directly: an output at `t` is the input
published one Step earlier, which is the Channel's Latency rather than the
FMU's. To point it at your own FMU, change the path, the schemas, and the
stimulus — nothing else.

Any co-simulation call that answers a status other than `fmi3OK` aborts the
Run with exit 1, and the diagnostic names the call, the participant and the
status. `Warning` aborts too: a Run that steps past a status the FMU raised is
not evidence of anything. An FMU that answers `Fatal` is abandoned rather than
terminated, which is what FMI 3.0 requires of its importer.

This milestone deliberately supports FMI 3.0 co-simulation only and Float64
variables only. It does not implement the bus layered standard because the
repository has no demo FMU against which to write a conformance test.

## Large-Message routing baseline

The current data path copies a payload once from the publisher into the kernel.
It copies it again into every subscriber's queue.
[docs/bench/large-message-routing-baseline.md](docs/bench/large-message-routing-baseline.md)
measures what that costs today, before the project commits to a leased buffer
pool. The matrix varies payload size, subscriber fan-out, recording on or off,
native against process participants, inline against shared-memory transport,
and burst delivery.

```sh
make bench                     # regenerate docs/bench/routing-baseline.{json,md}
```

Copy counts come from `sil-run-instrumented`, the same kernel sources compiled
with `SIL_COPY_COUNTERS`. Wall-clock comes from the production `sil-run`, which
carries no instrumentation. Keeping the two apart lets a run report copies as
counts instead of inferring them from timing. These counters are the whole
counter surface (#63): there is no metrics artifact and no metrics flag on
`sil-run`, because a run reproduces exactly, so re-running it instrumented
answers the question for the price of one run — an inertness the determinism
suite asserts per fixture, as the same exit code and the same recording bytes
under both runners. The report states the run's exit code and keeps its
repeatable counters (`deterministic`) apart from its wall-clock and RSS values
(`observational`).

`sil-run --no-recording` runs a manifest and writes no recording. That
separates the cost of routing to subscribers from the cost of recording I/O.

## ACC reference example

[python/src/sil/examples/acc/](python/src/sil/examples/acc/) is one closed loop
end to end: a plant carrying
the longitudinal motion of two vehicles, a shimmed vECU controller, and a test
participant holding the Run to a minimum-gap KPI. The packaged
`sil.examples.acc.manifest` module is the whole Run — three participants, two
Channels, and the Duration — and the pytest suite in `tests/test_example_acc.py`
imports that same module rather than restating it.

```sh
make example                                    # run it, record it into build/
```

It ships in two variants. The second declares one Interceptor — five Steps of
delay on the sensing Channel over a one-second window — and comes from the same
builder:

```sh
PYTHONPATH=$PWD/python/src .venv/bin/python -m sil.examples.acc.manifest \
    build/acc.json
PYTHONPATH=$PWD/python/src .venv/bin/python -m sil.examples.acc.manifest \
    --delayed-sensing build/acc-delayed.json
```

The two hashes differ, which says the Runs are not the same Run;
`tests/test_example_acc.py` is what says the Interceptor is the whole of the
difference, by taking it back out of the delayed Manifest and getting the
nominal one. Late sensing is late braking: the delayed Run drives a measurably
different trajectory, which is what shows the Interceptor doing something
rather than merely being declared. Both variants are in the CI determinism
gate.

## Install and run from a staged prefix

The production runner, virtual Clock shim, and public Native participant
headers can be staged independently of the build tree. The Python wheel
contains the `sil` package, the ACC reference Run, and its command-line entry
points. Build the two parts from the repository:

```sh
uv build --wheel --out-dir dist python
wheel=$(find dist -maxdepth 1 -name 'sil-*.whl' -print -quit)
uv venv .venv-staged
uv pip install -p .venv-staged/bin/python "$wheel"

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
prefix=$(mktemp -d)
cmake --install build --prefix "$prefix"
```

Add both installation `bin` directories to `PATH`, then run from any directory
outside the checkout. No `PYTHONPATH`, build-tree path, or preload-library path
is needed:

```sh
export PATH="$PWD/.venv-staged/bin:$prefix/bin:$PATH"
workdir=$(mktemp -d)
cd "$workdir"
sil-acc acc.json
sil-run acc.json -o acc.mcap
sil-check acc.json --runner sil-run

sil-acc --delayed-sensing acc-delayed.json
sil-run acc-delayed.json -o acc-delayed.mcap
sil-check acc-delayed.json --runner sil-run
```

The installed runner resolves the Clock shim from the staged prefix's
conventional library directory. The installed headers are under
`$prefix/include/sil`; development fixtures and `sil-run-instrumented` are
not part of the production installation.

## Run one Manifest in a Linux container

Build the production image from its pinned base-image digest and exact Python
dependency lock. The separate `acceptance` target adds the public FMU reference
artifact for this repository's CI checks; it is not part of the production
image.

```sh
version="$(python3 tools/release.py project-version)"
docker build --target runtime -t sil:local \
  --build-arg SIL_VERSION="$version" \
  --build-arg SIL_SOURCE_REVISION=local .
```

Mount a workspace at `/workspace` and use the host user's numeric identity so
ordinary host-owned directories remain writable. Runtime networking is not
needed. The image entry point is `sil-run`, so its arguments are the installed
runner's normal command-line contract:

```sh
workspace=$PWD/run
mkdir -p "$workspace"
cp manifest.json "$workspace/manifest.json"

docker run --rm --network none \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  sil:local /workspace/manifest.json \
  --participant-timeout-ms 5000 -o /workspace/out.mcap
```

The command prints the Manifest hash, returns `sil-run`'s exit code unchanged,
and writes `out.mcap` directly into the host directory. To run without a
Recording while keeping the same Manifest and Manifest hash:

```sh
docker run --rm --network none \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  sil:local /workspace/manifest.json \
  --participant-timeout-ms 5000 --no-recording
```

A private image can add Participant artifacts without changing the production
runtime surface. Name their image paths in the mounted Manifest:

```dockerfile
FROM sil:local
USER root
COPY --chown=10001:10001 participants/ /opt/participants/
USER 10001:10001
```

The default image user is non-root. Callers may still select their host UID and
GID as above; the installed runner and Python Participant entry points do not
depend on a passwd entry or a writable home directory.

## Consumer adoption proof

[proofs/esmini/](proofs/esmini/) is that derived-image pattern carried through
end to end with a Participant this repository did not write: the third-party
scenario engine [esmini](https://github.com/esmini/esmini) `v3.8.1`, driving a
UN-R157 cut-in scenario as a Process participant. It starts from the published
runner image by digest, declares its own Schema, Channels, route capacities
and Duration, is judged by a domain KPI in-run and post-hoc, reproduces
esmini's own expected trajectory within tolerance, and records bit-identically
across two Runs.

```sh
proofs/esmini/run-proof.sh            # needs docker and network
```

Nothing in that directory is part of the framework. Its README is the evidence:
what worked unchanged, what needed consumer-side adaptation, and what the
current contracts cannot represent.

## Build & test

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
ctest --test-dir build --output-on-failure
uv venv .venv && uv pip install -p .venv/bin/python -e "python/[dev]"
.venv/bin/python -m pytest tests/
```

The pytest suite builds the kernel if needed and includes the milestone exit
criterion (`tests/test_determinism.py`: run twice → bit-identical MCAP).

## Layout

```
kernel/src/        C++20 kernel: manifest, interceptor plan, engine
                   (scheduler+router), recorder, native/process adapters
include/sil/       stable C ABI for native participants; clock-region layout
participants/      toy native participants (walking-skeleton fixtures) and
                   the bench publisher/subscriber fixtures for the
                   routing baseline
shim/              virtual clock shim (preload lib) + probe for POSIX vECUs
schemas/           message schemas for benchmark, FMU, and toy fixtures
tools/silschema.py schema → packed C structs; sil.schema packs the same
                   layout in Python
tools/bench_*.py   routing-baseline driver and its process participants
docs/bench/        routing baseline: procedure, raw results, decision inputs
python/src/sil/examples/acc/
                   the packaged ACC reference example: one closed-loop Run
                   in a nominal and a delayed-sensing variant
examples/fmu/      the FMU import example: one Reference FMU driven as a
                   process participant
proofs/esmini/     consumer-side adoption proof: a third-party scenario
                   engine run on the published release, with its evidence
python/src/sil/    manifest builder, step-participant lib, test API,
                   determinism check, declared memory footprint
tests/             behavior tests at the run boundary
```

## Notes / deferred (per DESIGN.md)

- Shared-memory zero-copy payloads and bus adapters: later milestones.
- The recorder is fed in global publish order — behaviorally identical to a
  latency-0 subscriber scheduled last in every slot.
- Message layout is packed little-endian; cross-platform bit-exactness is an
  explicit non-goal.

## License, security, and support

- [LICENSE](LICENSE) — Apache License 2.0, SPDX `Apache-2.0`, covering the
  kernel, the public C ABI headers, the Clock shim, the Python distribution,
  and the schema-generation surface a Participant compiles against.
- [NOTICE](NOTICE) — the attribution notice the license propagates.
- [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) — every third-party
  component in a published artifact, with its license.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability privately, what a
  report should contain, what response to expect, and which versions get fixes.
- [SUPPORT.md](SUPPORT.md) — supported machine class, the interfaces under the
  compatibility policy, the interfaces that are implementation details, the
  boundary of the determinism guarantee, and the absence of any safety
  qualification.
- [CONTRIBUTING.md](CONTRIBUTING.md) — the terms a contribution is accepted
  under, and how to get a change reviewed.
- [docs/releasing.md](docs/releasing.md) — how maintainers prepare, publish,
  verify, and recover a versioned release bundle.
