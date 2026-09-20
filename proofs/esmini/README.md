# Consumer proof: esmini on released SiL artifacts

One representative Participant that was never written as a SiL fixture, run on
nothing but a published SiL release. This directory is the whole proof for
[issue #117](https://github.com/Stevie1704/sil/issues/117): the consumer-side
material, the commands, and the evidence the Run produced.

Nothing here is part of the SiL product. The kernel never sees these files;
they are what an adopter would write.

## What was proven

A UN-R157-shaped ALKS cut-in scenario, played by esmini's scenario engine,
runs as an ordinary SiL Process participant: deterministic, bounded, judged by
a domain KPI, and reproducing the upstream project's own expected trajectory.

| Question | Answer |
| --- | --- |
| Does a third-party simulator fit the Step protocol? | Yes, through one ctypes adapter and no change to SiL |
| Does an opaque C++ simulator need the Clock shim? | Not this one: withdraw it and 0 of 16 000 recorded field readings change |
| Is the Run deterministic? | Yes, bit-identical Recordings — and byte-identical again on a second machine |
| Does the Run reproduce the vendor's expected values? | Yes, worst deviation 4.5e-4 against a 1e-2 tolerance |
| Does a KPI failure reach the caller? | Yes, exit 1 with the value, the floor, and the Virtual time |

## The artifacts

### SiL

Recorded in [`evidence/identity.txt`](evidence/identity.txt).

| Field | Value |
| --- | --- |
| Release | `v0.1.0` |
| Runner image | `ghcr.io/stevie1704/sil@sha256:d67a254125fbe44467bf1354a04d19955815098d86a4c94c74f73ead6a943c0b` |
| Reported version | `sil-run --version` → `0.1.0` |

The derived image is built `FROM` that **digest**, never the `0.1.0` tag. Every
SiL command the proof runs — `sil-run`, `sil-footprint`, `sil-check`, the
`sil.manifest` builder, the `sil.participant` Step endpoint, the
`sil.recording` reader — is an entry point that image already ships. No
binary, wheel, or Python module comes from a SiL checkout or build directory.

### esmini

| Field | Value |
| --- | --- |
| Artifact | esmini — "Environment Simulator Minimalistic", an OpenSCENARIO player. The Run drives its `esminiLib` scenario-engine library, not the viewer application |
| Upstream | <https://github.com/esmini/esmini> |
| Version | `v3.8.1`, build 6383 |
| Binaries | `esmini-bin_Linux.zip`, SHA-256 `05e6c9bb…9654` |
| Scenarios and roads | `esmini-fullsrc.zip`, SHA-256 `dacb0d3c…105e` |
| License | Mozilla Public License 2.0; the unmodified binaries are redistributed inside a locally built image with the upstream `LICENSE` kept at `/opt/esmini/LICENSE`. Recorded in [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md) as downloaded by a proof and shipped in no SiL artifact |
| Scenario | `resources/xosc/alks_r157_cut_in_quick_brake.xosc`, shipped unmodified |

Both checksums are verified during the image build and fail it on mismatch.
esmini predates this repository and has no knowledge of SiL; every contract it
meets, it meets by being an ordinary Linux simulation library.

## The Run

Two Participants, two Channels, one Schema.

```
scenario (esmini, shimmed)  --esmini.Ego----->  kpi (Test participant)
                            --esmini.Target-->
```

| Declaration | Value | Why |
| --- | --- | --- |
| Duration | 8 s | Covers the encounter and the standstill that ends it, and stays inside esmini's own stop trigger at 10.00 s |
| Step period | 10 ms, both Participants | esmini's own smoke test drives this scenario at `--fixed_timestep 0.01`, and its API documents a fixed step size as the precondition for repeatable results |
| Channels | `esmini.Ego`, `esmini.Target`, Schema `esmini.ObjectState`, 72 B | One Channel per named scenario object; inline Transport, because 72 B does not need an Arena |
| Subscriber routes | capacity 2, overflow `fail` | Both Participants step at the same period, so a route's steady depth is one and its worst case is two: the publisher activates first in the Slot |
| Arenas | none | Declaring one for a 72 B payload would reserve memory the Run cannot use. The [footprint report](evidence/footprint.txt) shows no unbounded route and no Arena |
| Process deadline | `--participant-timeout-ms 30000` | Wall-clock guard on every request; esmini parses the OpenDRIVE network inside its initialization, which is the longest one |
| Clock shim | on the `scenario` Participant | esmini keeps its own time and its applications synchronise to the wall clock by default. Declared as insurance; the control below shows this scenario does not depend on it |
| Sleep policy | `reject`, the builder default | A sleeping Participant would be a determinism hole. The Run never trips it |

Manifest hashes are in
[`evidence/manifest-hashes.txt`](evidence/manifest-hashes.txt); the exact bytes
of both variants are in [`evidence/alks-cut-in.json`](evidence/alks-cut-in.json)
and
[`evidence/alks-cut-in-failing.json`](evidence/alks-cut-in-failing.json).

### The domain KPI

The scenario is the regulation's: a target vehicle cuts into the ego's lane and
brakes at 5.2 m/s², and the ego is driven by esmini's own ALKS safety model.
The property that decides the Run is that model's job — **the ego must come to
a stop without contacting the target**.

Three things are asserted, deliberately different in kind:

- **Every Step, in-run**: the longitudinal freespace gap stays above 0.25 m.
  The measured minimum is 0.358 m at 7.68 s, where both vehicles are at rest.
- **At one declared Virtual time, in-run**: at 6.35 s the target is in the
  ego's lane, it was seen in another lane earlier, and the ego has slowed to
  under 10 m/s. It is doing 6.080 m/s, down from 20.
- **Post-hoc, over the whole Recording**: the same properties, plus the final
  Message the in-run half cannot see. Under the default Latency a Message
  published one Step before the Duration becomes visible at the Duration,
  where no activation is due. `verify.py` reads the Channels and thresholds
  back out of the Manifest the Run was executed from, so the two halves
  cannot drift apart.

A Participant that published syntactically valid but unchanging object state
would satisfy the gap floor and fail all three encounter assertions: no lane
change, no deceleration.

## Results

| Acceptance | Evidence |
| --- | --- |
| Run completes with the expected exit code and emits its Manifest hash | exit 0, `manifest_hash fd33135e…2757` |
| Recording is readable, with the expected Channels and Virtual timestamps | 800 Messages per Channel at exactly 0 ns … 7 990 000 000 ns, 10 ms apart — [`observations.json`](evidence/observations.json) |
| Domain KPI evaluated in-run and post-hoc | minimum gap 0.358 m at 7.68 s; ego at 6.080 m/s at the 6.35 s evaluation instant |
| A deliberately failing variant reaches the caller | exit 1, `participant 'kpi' failed: AssertionError: freespace gap 1.971 m is below the 2.000 m floor at t=6690000000 ns (…)` — [`failing-variant.txt`](evidence/failing-variant.txt) |
| A Manifest naming an environment the Participant cannot honour is a Manifest error | exit 2, `participant 'scenario': sil.participant.ManifestError: esmini SE_Init returned -1 for scenario '…/does-not-exist.xosc'` — [`manifest-error.txt`](evidence/manifest-error.txt) |
| Two Runs are bit-identical | both Recordings SHA-256 `4ff77017…16c4` — [`determinism.txt`](evidence/determinism.txt) |
| The recorded trajectory matches the vendor's expectation | 10 samples at 5 Virtual times, worst deviation 4.5e-4 m against a 1e-2 tolerance |
| The Recording is retained, not only its hash | [`evidence/run-1.mcap`](evidence/run-1.mcap), 191 911 B, SHA-256 `4ff77017…16c4` |
| Withdrawing the Clock shim changes no object state | 0 differing readings out of 16 000 — [`clock-shim-control.json`](evidence/clock-shim-control.json) |
| The vendor expectation is what esmini actually says | all 10 samples matched against esmini's own smoke test in the image, and the id block confirmed against all four ALKS models — [`reference-provenance.txt`](evidence/reference-provenance.txt) |

### Against the vendor's expectation

esmini ships no FMI-LS-REF reference result, but it does ship the equivalent:
its smoke test asserts exact positions and speeds for this scenario, produced
on the upstream project's machines. Those values are transcribed into
[`reference/alks_r157_expected.json`](reference/alks_r157_expected.json) and
checked with a tolerance — never bit-compared, because they come from another
machine class. This is independent of the determinism check: determinism says
two Runs agree with each other, this says the Run agrees with esmini.

Identifying *which* expected values apply was consumer work in itself. The
smoke test runs the scenario once per ALKS safety model and merges the four
recordings, which offsets object ids by 100 per merged file — and the block
order is the merge order, not the order of the model list in the test.

That is checked rather than claimed.
[`reference/check_transcription.py`](reference/check_transcription.py) reads
esmini's smoke test out of the derived image and holds the reference file to
it twice over: every transcribed sample must be a row the smoke test actually
asserts, and the block it was taken from must be reproduced by the model the
scenario file declares — and by no other. It is, and the four models are
plainly different:

```
transcription: 10 samples match the smoke test
  ReferenceDriver  ego x=146.870 m speed=14.191 m/s
  Regulation       ego x=152.710 m speed=6.080 m/s
  FSM              ego x=155.824 m speed=6.321 m/s
  RSS              ego x=148.191 m speed=4.367 m/s
identification: only 'Regulation' reproduces it, as declared
```

A mistranscribed digit cannot pass as a vendor expectation.

### The Clock shim, controlled rather than asserted

Declaring `shim: true` proves nothing about whether esmini ever reads the wall
clock. So the same Run is executed with the shim withdrawn and the two
Recordings compared —
[`clock_shim_control.py`](clock_shim_control.py), result in
[`evidence/clock-shim-control.json`](evidence/clock-shim-control.json).

The comparison is by payload, not by file bytes, and that distinction turned
out to matter. A Recording embeds its own Manifest hash, so withdrawing the
shim changes the Manifest, changes the hash, and changes the file — the two
Recordings differ by construction whatever esmini does. Comparing the bytes
would have "proved" the shim load-bearing when it is not.

By object state the two Runs are identical: **0 differing field readings out
of 16 000**, across 800 Slots and both vehicles. esmini driven through
`SE_StepDT` with a fixed step size makes no wall-clock read that reaches its
output. The shim is correct insurance for a scenario that does — esmini's
applications synchronise to the wall clock by default — but this Run does not
depend on it, and the honest claim is that the strict `reject` sleep policy
was never tripped, not that an interception was observed.

This does not weaken what the proof exercises. The production integration
boundary here is the Process-participant Step protocol, and it is exercised
on every one of the 800 Slots. The Clock shim is a second boundary, declared
and carried through the Run — the control is what says it is not the one
doing the work.

### Observations, not thresholds

From [`evidence/resources.json`](evidence/resources.json) and
[`evidence/observations.json`](evidence/observations.json). Timing and memory
are machine-dependent and are recorded rather than asserted.

| Observation | Value |
| --- | --- |
| Wall-clock time, 800 Slots | 0.66 s |
| Peak resident set, largest child | 62.0 MB |
| Declared payload footprint | 2 routes × 2 × 72 B; no Arena, no unbounded route |
| Recording size | 191 911 B for 1 600 Messages, retained at [`evidence/run-1.mcap`](evidence/run-1.mcap) |
| Deterministic routing counters | not applicable: the instrumented runner is a development fixture and is deliberately absent from the production image |

**Machine class.** The committed evidence comes from a native `linux/amd64`
run of `run-proof.sh` on an `ubuntu-latest` runner — the machine class this
release supports. `.github/workflows/proof-esmini.yml` executes it on any pull
request that touches this directory, so the evidence a reviewer reads is
produced by the supported class rather than by whatever machine wrote the
proof.

The proof was developed on an arm64 host running the same `linux/amd64` image
under emulation, and that turned out to be a free cross-check: the emulated
Run produced a Recording **byte-identical** to the native one, SHA-256
`4ff77017…16c4`. Only the cost differed, and by a lot — 7.10 s and 152.7 MB
emulated against 0.66 s and 62.0 MB native. Which is the argument for keeping
timing out of the pass criteria: the same Run, the same bytes, an order of
magnitude apart in wall clock.

## Reproduce it

Requires docker and, for the image build, network access. Everything the
script pulls is public.

```sh
proofs/esmini/run-proof.sh [evidence-dir] [workspace-dir]
```

It builds the derived image, records the artifact identities, builds both
Manifests with the released builder, reports the declared footprint, runs the
proof under a resource observer, verifies the Recording against the KPI and the
vendor's expectation, runs the determinism check twice over, and runs the
failing variant. Any deviation fails the script.

## What required consumer-side work

Every adapter, generated file, environment setting, copied library, path
convention, and manual transformation this adoption needed.

1. **Five X and OpenGL shared libraries.** `libesminiLib.so` links `libGL`,
   `libX11`, `libXrandr`, `libXinerama` and `libfontconfig` even when the Run
   instantiates no viewer, so the dynamic loader needs them present before the
   library can be opened at all. The derived image installs them; the base
   image is untouched. Resolved dependencies:
   [`evidence/esmini-runtime-libs.txt`](evidence/esmini-runtime-libs.txt).
2. **Re-plumbing stdout.** The Step protocol reserves the Participant's stdout
   for protocol lines. esmini writes a version banner and its log to stdout
   from C. `_reserve_protocol_stdout` moves the real stdout onto a private
   descriptor and points file descriptor 1 at stderr before the library is
   loaded. `SE_LogToConsole(false)` is called as well but is not a guarantee:
   esmini documents options as unset on the next scenario run, and a banner
   printed during load arrives before any option could take effect.
3. **Suppressing the log file.** `SE_SetLogFilePath("")` before `SE_Init`,
   or esmini writes a log into the kernel-owned Run working directory.
4. **A hand-written ctypes binding.** No binding ships for Python. The adapter
   declares `argtypes` and `restype` for every entry point it calls — without
   them ctypes mis-passes the `double` `dt` and truncates the `double`
   returns — and mirrors the 28-field `SE_ScenarioObjectState` struct field
   for field from `esminiLib.hpp`.
5. **An object-to-Channel mapping in the Manifest.** esmini discovers its
   object list at runtime; a Channel is a static Manifest fact. The consumer
   names each object and its Channel on the Participant's command line
   (`--object Ego=esmini.Ego`) and resolves ids once with `SE_GetIdByName`.
6. **Reconciling two notions of "finished".** esmini's stop trigger raises a
   quit flag — at 10.00 s for this scenario — while the Manifest's Duration is
   what ends the Run. The consumer picks a Duration inside the scenario's own
   life, and the adapter fails the Run loudly if the quit flag rises first
   rather than publishing state the scenario no longer stands behind.
7. **Aligning Virtual time zero.** The Step at `t = 0` publishes the state
   `SE_Init` left behind, without stepping; every later Step advances first.
   Without that convention the whole trajectory sits one Step away from the
   vendor's expected values.
8. **Absolute paths everywhere.** The kernel starts each child in its own Run
   working directory, so the scenario path, the library path, and the
   Participant file path in the Manifest are absolute.
9. **Identifying the vendor's expected values** for the scenario as
   distributed, described above.

Consumer-authored files: one adapter
([`participants/esmini_participant.py`](participants/esmini_participant.py)),
one Test participant ([`participants/kpi.py`](participants/kpi.py)), one
Schema ([`schemas/esmini.json`](schemas/esmini.json)), one Manifest builder
([`manifest.py`](manifest.py)), and three scripts that judge, measure, and
audit the result ([`verify.py`](verify.py), [`observe.py`](observe.py),
[`reference/check_transcription.py`](reference/check_transcription.py)). No
generated code, no code generator, and no patch to esmini.

## Integration friction

### Worked unchanged

- The published runner image ran the Run as shipped. The derived image only
  adds files and installs packages; nothing in the base image is modified.
- The Step protocol took a foreign stepping API directly. Mapping one Step
  onto `SE_StepDT(dt)` needed no shim in between, and the Manifest's integer
  nanosecond period converts to exactly the 0.01 s esmini's own tests use.
- The Clock shim cost one Manifest field and no change to esmini, and the
  Run never tripped the strict `reject` sleep policy. What it did *not* do is
  matter — see the control below.
- Both validators accepted the Manifest, and the declared Channel contract
  needed no compatibility escape hatch.
- Determinism needed no special handling at all. Two Runs, bit-identical, with
  no retry, no sorting pass, and no post-processing.
- The non-root image user with a host UID and GID, `--network none`, and the
  bind-mounted workspace all behaved as documented.
- Exit-code classification landed where it should: a KPI violation is a Run
  failure (1), and a Manifest that names a scenario esmini cannot load is a
  Manifest error (2) carrying the adapter's own diagnostic. Neither needed a
  new failure kind.

### Needed consumer-side adaptation

The nine items above. None of them required changing SiL, and none of them
required changing esmini.

### Unexercised scenarios and a tooling gap

Recorded, not built. Each is narrow and is deferred to the capability-gate
issue.

- **Dynamic entities are not covered by this adapter.** A Schema is a fixed,
  packed layout, and this Run maps two objects known when the Manifest is
  written onto two Channels. esmini can add and delete entities during a
  scenario; this proof did not exercise that behavior. Fixed-capacity arrays
  with an active count, or bounded entity-update Messages, are candidate
  consumer-side representations under current contracts. They must be tested
  against a named scenario before claiming a framework limitation.
- **OSI over UDP has no adapter.** esmini's standard interface for
  sensor-grade ground truth is Open Simulation Interface over a UDP socket.
  The proof did not need it — it read the C API directly — so no bus adapter
  was written.
- **`sil-check` cannot carry a Process-participant deadline.** The shipped
  determinism check invokes the runner without `--participant-timeout-ms`, so
  the Run it checks is not the Run a caller with a deadline executes. Here
  both were run and both produced the same Recording hash, which is why this
  is a gap in the tool rather than a result that differs. Closed in the
  development checkout and validated separately below; the released checker
  this proof ran still has no such option.

## Validating the updated checker

[Issue #134](https://github.com/Stevie1704/sil/issues/134) gives `sil-check` a
`--participant-timeout-ms` option and forwards it to both of its Runs. That
checker is not in any release, so it is not in the image above and does not
belong in the release evidence. It is validated on the same consumer artifact
by a separate script:

```sh
./validate-checker-deadline.sh
```

The script builds the same derived image from the same pinned runner digest
and mounts only `python/src/sil/check.py` into it, read-only, as the checker
under test. The runner, the Manifest builder, the Step endpoint, and the
consumer participants all remain the released ones the proof ran. It rebuilds
the nominal 8 s / 10 ms Manifest, refuses to continue if its hash is no longer
the one above, runs the check with a 30000 ms response deadline, then retains
one deadline-bounded Run so the existing KPI and upstream-reference checks
have bytes to read and the digest the checker reported is tied to an artifact.

Its evidence is written to `evidence-checker-deadline/` — identities of the
runner and the checker under test, the Manifest hash, the Recording hashes
next to the v0.1.0 one, and the post-hoc observations. Nothing in `evidence/`
is touched. The release-only reproduction in `run-proof.sh` is updated when a
release containing the option exists, and records that release's identity.

### What the validation produced

| Field | Value |
| --- | --- |
| Checker under test | `python/src/sil/check.py`, SHA-256 `12efb549…d5c0`, revision `38da0f0` |
| Runner | the pinned `v0.1.0` image above, unchanged |
| Manifest SHA-256 | `fd33135e…2757` — the release proof's nominal Manifest |
| Response deadline | `30000` ms, on both checked Runs and on the retained one |
| Checker verdict | `deterministic: 4ff77017…16c4` |
| Retained Recording | `4ff77017…16c4`, 191,911 bytes |

Both deadline-bounded Runs completed and agreed byte for byte, and their
digest is the one the v0.1.0 proof recorded without a deadline. The KPI and
the ten upstream-reference samples pass on the retained Recording with values
identical to `evidence/observations.json`.

One difference from the release evidence: this validation ran `linux/amd64`
emulated on a `Darwin arm64` host, recorded in its `identity.txt`, rather than
on the native x86-64 machine class the v0.1.0 evidence names. Reproducing the
released digest there is a stronger result than the deadline needs, not a
claim that determinism is now asserted across machine classes.

The stalled-Participant behavior the option exists for is covered at the run
boundary by `tests/test_check_participant_timeout.py`, not here: this
consumer answers every request well inside the deadline.

## Deferred

The capability gate
([#118](https://github.com/Stevie1704/sil/issues/118)) selected **no new framework
capability** from this evidence. Its
[decision record](../../docs/decisions/118-consumer-capability-gate.md) states
the evidence needed to reopen each deferred alternative. The checker deadline
omission is a separate tooling follow-up,
[#134](https://github.com/Stevie1704/sil/issues/134). This proof adds no bus
adapter, no Native ABI change, no FMI type expansion, and no data-plane change;
its retained v0.1.0 evidence remains unchanged.
