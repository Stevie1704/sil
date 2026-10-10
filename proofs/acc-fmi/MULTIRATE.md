# ACC multi-rate coupling evidence (#197)

This proof exercises the authored `sil fmi couple` profile with the pinned,
source-available `AccController` and `AccPlant` PythonFMU3 FMI 3.0
Co-Simulation archives. It executes each coupled Run with an installed SiL
runtime on Linux x86-64 and independently steps the same archives with FMPy.
The FMUs declare `canHandleVariableCommunicationStepSize=false` and no Clock
period. Each tested Run gives each FMU one constant, positive communication
Period; the preparation step executes every listed combination with FMPy.
This evidence covers these two archives and this signal-coupled topology only.

## Predeclared experiment

All times below are milliseconds. Duration is 5000 ms, with half-open
publication Slots (`0 <= t < Duration`). Every FMU's last output describes
the Sample time at Duration and is checked post-hoc. Plant priority is 0;
controller priority is 1. Every input has an explicit start: plant command 0,
lead acceleration 0, initial lead position 60 m; controller gap 60 m,
relative speed 0 m/s and ego speed 25 m/s. Each FMU initializes separately
before ordinary Channel delivery. There is no joint initialization solver.

| Row | Plant Period | Controller Period | sensing Latency | command Latency | Baseline | Varies |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `equal-10` | 10 | 10 | 10 | 10 | — | Existing equal-Period anchor |
| `plant-20` | 20 | 10 | 10 | 10 | `equal-10` | plant sampling |
| `controller-20` | 10 | 20 | 10 | 10 | `equal-10` | controller sampling |
| `sensing-20` | 20 | 10 | 20 | 10 | `plant-20` | sensing delay |
| `command-20` | 20 | 10 | 10 | 20 | `plant-20` | command delay |
| `command-30` | 20 | 10 | 10 | 30 | `plant-20` | command delay |
| `zero-sensing` | 20 | 10 | 0 | 20 | `command-20` | sensing delay; shared-Slot delivery |

Each Row differs from its baseline in exactly one Period or Latency, and the
report names that value. A sampling change is therefore never measured
together with a delay change. `zero-sensing` uses a 20 ms command Latency so
that the plant consumes the even-Slot commands, which are the ones the
shared-Slot sensing delivery changes. With a 10 ms command Latency the plant
would consume only odd-Slot commands, and the change in sensing Latency could
not reach the plant.

## Negative controls

A negative control changes the independent FMPy run of `zero-sensing`, never
the SiL Run. The SiL Recording of `zero-sensing` has to differ from each
faulty run, and the report names the first differing signal, publication Slot
and Sample time:

| Fault | Change to the independent run |
| --- | --- |
| `wrong-initialization` | plant initial lead position 70 m instead of 60 m |
| `wrong-zero-order` | the controller runs before the plant in a shared Slot, so it takes the prior plant publication |
| `one-period-shift` | every command arrives one plant Period (20 ms) late |

Each fault is a defect that a wrong coupling in SiL could have. The proof
passes only if the normal comparison rejects the fault.

## Delivery schedule

The delivery oracle in `multirate_contract.py` states the input schedule as a
formula, without reading SiL's plan. For every activation it chooses the
newest publication visible at that time. When none is visible it holds the
input start; when no new Message arrives it keeps the last input. A
publication at `p` is first eligible at a subscriber activation `>= p +
Latency`. At zero sensing Latency the controller can take the plant's
publication in the same Slot, because the plant runs first. For example, with
plant Period 20, controller Period 10 and sensing Latency 10, controller
activations at 0/10/20/30/40 ms take start/plant publications 0/0/20/20 ms.
With sensing Latency 0 they take plant publications 0/0/20/20/40 ms.

The FMPy run in `multirate_independent.py` does not use this formula. Each
activation takes the newest publication that already exists and whose
Latency has passed, and the run records which publication each activation
consumed. Preparation stops if the two derivations differ for any Row. The
pinned bundle retains the complete expected input schedule for each Row, and
each SiL plan is checked against it before its Run.

## Comparison

Every Channel has its own Sample time mapping: `sensing` and `state` use the
plant Period; `command` uses the controller Period. A Recording's publication
Slot plus that offset is its Sample time. The comparison checks every native
Sample time at 10 or 20 ms with `abs(actual-reference) <= 1e-10 + 1e-12 *
abs(reference)` in SI units. It refuses missing or duplicate publications,
wrong width and nonfinite values. No Sample time is inferred from a Recording
and no interpolation is performed.

Sensitivity compares every field of every Channel against the Row's baseline
on the predeclared 20 ms common Observation grid from 20 through 5000 ms
inclusive. The Sensitivity envelope is:

| Field | Bound |
| --- | ---: |
| `gap_m`, `ego_position_m`, `lead_position_m` | 0.25 m |
| `relative_speed_mps`, `ego_speed_mps`, `lead_speed_mps` | 0.15 m/s |
| `accel_mps2` | 0.25 m/s² |

The `wrong-initialization` fault is deliberately outside it. These bounds are
experimental acceptance thresholds, not a claim of universal monotonic
convergence as Period or Latency changes.

The first retained run declared a 5 m, 1 m/s and 1 m/s² envelope for `gap_m`,
`ego_speed_mps` and `accel_mps2` only. Its largest measured difference was 13
to 60 times smaller than those bounds, so that envelope could not tell a
correct result from a much worse one. The bounds above were declared after
that run and before the run that produced the current report. They give about
three times the largest difference the first run measured.

## Reproduce on Linux x86-64

```sh
proofs/acc-fmi/multirate-bundle.sh prepare build/acc-multirate-bundle
proofs/acc-fmi/multirate-bundle.sh run build/acc-multirate-bundle build/acc-multirate-evidence
```

Preparation builds pinned tool and production runtime images, exports the two
FMUs, executes the independent FMPy runs, and hashes every bundle file.
The example image extends the installed production runtime with only this proof's
authoring and comparison code. It checks bundle and archive hashes, authors
and runs each Manifest twice, byte-compares each pair of Recordings, compares
every Row with its independent FMPy run, and writes `report.json` plus raw
Manifests and Recordings to the evidence directory. FMPy and PythonFMU3 are
absent from the example image. The report names the first differing signal,
publication Slot and Sample time for each negative control and records both
inside and outside envelope values.
