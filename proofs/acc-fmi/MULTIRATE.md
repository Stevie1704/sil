# ACC multi-rate coupling evidence (#197)

This proof exercises the authored `sil-fmu-couple` profile with the pinned,
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
the endpoint at Duration and is checked post-hoc. Plant priority is 0;
controller priority is 1. Every input has an explicit start: plant command 0,
lead acceleration 0, initial lead position 60 m; controller gap 60 m,
relative speed 0 m/s and ego speed 25 m/s. Each FMU initializes separately
before ordinary Channel delivery. There is no joint initialization solver.

| Row | Plant Period | Controller Period | sensing Latency | command Latency | Purpose |
| --- | ---: | ---: | ---: | ---: | --- |
| `equal-10` | 10 | 10 | 10 | 10 | Existing equal-Period comparison anchor |
| `plant-20` | 20 | 10 | 10 | 10 | Vary plant sampling only |
| `controller-20` | 10 | 20 | 10 | 10 | Vary controller sampling only |
| `zero-sensing` | 20 | 10 | 0 | 10 | Vary sensing Latency only; shared-Slot delivery |
| `command-20` | 20 | 10 | 10 | 20 | Vary command Latency only |
| `command-30` | 20 | 10 | 10 | 30 | Larger command Latency change |
| `wrong-initialization` | 20 | 10 | 0 | 10 | Negative control: plant lead start 70 m |
| `one-period-shift` | 20 | 10 | 0 | 30 | Negative control: command shifted one plant Period |

Under zero sensing Latency, the controller publishes equal commands in each
pair of 10 ms Slots between plant samples, so changing command Latency from
10 to 20 ms leaves the plant input unchanged. A change to 30 ms crosses a
plant activation and changes behavior. Under 10 ms sensing Latency, the
`command-20` row does change behavior.

The `wrong-zero-order` control uses the `zero-sensing` Periods and Latencies
but an independently stepped FMPy schedule that wrongly takes the prior plant
publication in a shared Slot. The authored SiL topology requires the plant
to run first and the controller to take the same-Slot publication.

The delivery oracle in `multirate_contract.py` states the input schedule
without reading SiL's plan. For every activation it chooses the newest
publication visible at that time. When none is visible it holds the input
start; when no new Message arrives it keeps the last input. For positive
Latency, publication at `p` is first eligible at a subscriber activation
`>= p + Latency`. At zero sensing Latency the controller can take the plant's
publication in the same Slot. The pinned bundle retains the complete
expected input schedule for each row, and the FMPy reference records which
publication each activation consumed. For example, with plant Period 20,
controller Period 10 and sensing Latency 10, controller activations at
0/10/20/30/40 ms take start/plant publications 0/0/20/20 ms. With sensing
Latency 0 they take plant publications 0/0/20/20/40 ms.

Every Channel has its own endpoint mapping: `sensing` and `state` use the
plant Period; `command` uses the controller Period. A Recording's publication
Slot plus that offset is its observation time. The comparison checks every
native endpoint at 10 or 20 ms with `abs(actual-reference) <= 1e-10 +
1e-12 * abs(reference)` in SI units. It refuses missing or duplicate
publications, wrong width and nonfinite values. No observation time is
inferred from a Recording and no interpolation is performed. Sensitivity
compares `gap_m`, `ego_speed_mps`, and `accel_mps2` against `equal-10` on the
predeclared 20 ms common Observation grid from 20 through 5000 ms inclusive.
The Sensitivity envelope is 5 m, 1 m/s and 1 m/s² respectively. The
70 m wrong-initialization control is deliberately outside it. These bounds
are experimental acceptance thresholds, not a claim of universal monotonic
convergence as Period or Latency changes.

## Reproduce on Linux x86-64

```sh
proofs/acc-fmi/multirate-bundle.sh prepare build/acc-multirate-bundle
proofs/acc-fmi/multirate-bundle.sh run build/acc-multirate-bundle build/acc-multirate-evidence
```

Preparation builds pinned tool and production runtime images, exports the two
FMUs, executes the independent FMPy references, and hashes every bundle file.
The example image extends the installed production runtime with only this proof's
authoring and comparison code. It checks bundle and archive hashes, authors
and runs each Manifest twice, byte-compares each pair of Recordings, compares
every row with its independent reference, and writes `report.json` plus raw
Manifests and Recordings to the evidence directory. FMPy and PythonFMU3 are
absent from the example image. The report names the first differing signal,
publication Slot and observation time for each negative control and records
both inside and outside envelope values.
