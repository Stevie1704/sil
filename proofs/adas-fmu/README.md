# ADAS reference FMU: FMI 3.0 export with archive reproducibility, checked independently (#225)

```sh
proofs/adas-fmu/run-proof.sh [evidence-directory]   # needs docker and network
```

This proof exports the [ADAS reference application](../../docs/adas-reference.md)
as an FMI 3.0 Co-Simulation FMU, `AdasReference.fmu`, and qualifies the
archive without SiL's importer. The archive is the **reference acceptance
artifact** for the importer's type and array support
([#189](https://github.com/Stevie1704/sil/issues/189),
[#190](https://github.com/Stevie1704/sil/issues/190)). Those slices reuse
this archive, its pin and its expectations. They do not change the model to
make an unsupported feature disappear.

It is repository-owned: **not a supplier FMU**, and no claim of supplier
compatibility, general FMI conformance or ADAS safety. The interface is
documented in [FMI 3.0 export](../../docs/adas-reference.md#fmi-30-export).

## What runs where

| Step | Where | Network | Output |
| --- | --- | --- | --- |
| Preparation image | `Dockerfile`: pinned Python base, Debian snapshot `build-essential` (GCC 12.2), FMPy 0.3.26 by hash from [`../acc-fmi/requirements.lock`](../acc-fmi/requirements.lock) | image build only | image |
| Proof | `prove.py` in that image, Linux x86-64 | none | the archive, its identity and every report |

The preparation image is the only place the compiler and FMPy exist. The
Run-time example image that later executes this FMU through SiL needs
neither: it takes the qualified archive.

`prove.py` writes each step's result to `summary.json`, and passes only
when every step passes:

1. **Archive reproducibility.** `package.py` compiles the application and the
   FMI interface twice, each in its own scratch directory, and writes two
   archives. The bytes must be identical. The archive digest must equal the
   committed pin, [`evidence/AdasReference.identity.json`](evidence/AdasReference.identity.json).
2. **Interface audit** (`audit.py`). FMPy validates the model description.
   The archive must hold exactly the binary, the description, the build
   identity and the two licenses. Each Schema field of
   [`schemas.json`](../../examples/adas-reference/schemas.json) must be one
   variable of the matching FMI type and literal dimension. The capabilities
   must declare one fixed 10 ms step and nothing optional.
3. **Independent execution** (`check.py`). FMPy instantiates, initializes,
   sets typed inputs, steps, reads outputs, terminates and frees the FMU.
   The results must equal expectations authored without the C application.
4. **Negative control.** The same sources built with
   `ADAS_REFERENCE_WRONG_SIGN` must fail exactly the five checks with a
   closing object. The expectations detect a sign error that a native/FMU
   comparison could not: both share the application.

## Identity and archive reproducibility

`package.py` controls every input to the archive bytes:

| Input | Control |
| --- | --- |
| Compiler and linker | Debian snapshot `20260901T000000Z`; `cc --version` is in the identity |
| Flags | fixed in `package.py`: `-std=c11 -O2 -fPIC -shared -ffp-contract=off -fvisibility=hidden -Wall -Wextra`, and `-lm` |
| Paths | the sources are copied into a scratch directory and compiled by relative name; no debug information |
| Instantiation token | SHA-256 of the identity (profile, compiler, flags, defines, every source and license digest) |
| ZIP metadata | stored, not compressed; members in name order; timestamp 1980-01-01 00:00; mode `0644`; Unix creator |

The archive contains `documentation/identity.json`, so the archive itself
names the source digests it was built from. A change to a source, a flag or
the compiler is a new token and a new archive digest.
[`tests/test_adas_fmu.py`](../../tests/test_adas_fmu.py) fails when the
committed pin no longer names this checkout's sources. To re-pin, run the
proof and commit its evidence (below).

## Independent expectations

| Case | Expectation | What it covers |
| --- | --- | --- |
| `maneuver_<name>` (10) | `maneuvers/<name>.expected.csv`, enumerated by hand | the five one-object maneuvers of profile 1 and the list maneuvers, all fields and ages, final output at 200 ms |
| `start_values` | the description's start attributes; two steps with no input | every variable reads its start; the start header delivers nothing |
| `float32_inputs` | 7.9999999 is 8 in binary32 (no hazard), 7.9999995 stays below 8 (hazard) | inputs are binary32 |
| `float32_outputs` | `max_change_mps2` 0.1: binary64 steps rounded once to binary32 | outputs are binary32, parameters Float64 |
| `uint64_sample_times` | start at 10^16 ns, Sample times 1 ns before the activation: age 1 ns | UInt64 above 2^53 is exact |
| `late_start` | start at 9·10^18 ns, step at the points the FMU reports | valid steps near the 2^63 ns start limit, where a double second is coarser than 1 µs |
| `held_inputs` | an unchanged header delivers nothing; a new header with an old sequence is ignored and counted | the delivery convention |
| `parameters` | out-of-range parameters fail initialization; a parameter after initialization, an output, a wrong type or count is refused; reset recovers | parameter rejection |
| `fixed_step` | a 20 ms step and a skipped point fail | the fixed 10 ms step is enforced |
| `malformed_inputs` | count, inactive element, NaN, negative ID, future Sample time, sensor ID, validity | malformed input fails with its field named |
| `two_instances` | `hazard` and `clear`, stepped alternately in one process | independent instances |
| `instantiation_refused` | another token; Event Mode | failed instantiation, with its cause |
| `termination` | terminate before initialization fails; after it no step; outputs readable | orderly termination |

Every case checks the exact diagnostics the FMU logs. `check.json` keeps
them.

## SiL inspection

[`recorded-input.mapping.json`](recorded-input.mapping.json) is the mapping
a Run would declare to replay the maneuvers' input Recordings into this FMU
and record its Commands. It binds every Schema field to its variable.
`audit.py` runs `sil-fmi-inspect` with it, and with each binding alone, in a
Channel of that one field. The importer must accept the whole mapping and
each of its 36 bindings: the scalars of `Float32`, `Int32`, `UInt32` and
`UInt64` (since #189), the `[8]` object arrays (since #190), and the `UInt8`
validity flags and `Int64` ages (since #226). `inspection.json` keeps each
verdict. [proofs/adas-equivalence/](../adas-equivalence/README.md) runs this
archive through SiL.

## Evidence

[`evidence/`](evidence/) holds what the proof wrote on Linux x86-64:

| File | Content |
| --- | --- |
| `AdasReference.identity.json` | the pin: what the archive is built from, its token and its SHA-256 |
| `summary.json` | every step's result, and both build digests |
| `interface-audit.json` | the archive members, the description as declared, FMPy's validation |
| `inspection.json` | SiL's inspection of the recorded-input mapping and of each binding |
| `check.json` | every FMPy case, its result and the diagnostics |
| `control.json` | the wrong-sign control: which cases failed |
| `image-id.txt` | the preparation image the evidence came from |

The committed evidence was written under linux/amd64 emulation on an arm64
host. The `proof-adas-fmu` workflow runs the same proof on native Linux
x86-64 and must reproduce the pinned digest.

The archive itself is not committed: the pin and its archive reproducibility
define it. Run the proof to get it, in `AdasReference.fmu` of the evidence
directory.
