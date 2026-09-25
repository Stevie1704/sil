# Public artifacts for lightweight SiL adoption

Research date: 2026-09-25. Scope: issue #178 and the shared-library, single-FMU,
and multiple-FMU adoption paths. Company artifacts are not a prerequisite.

This is a source-verified shortlist, not completed runtime qualification. No new
candidate library or FMU was compiled or run for this research, and no complete
driving segment was decoded. Exact versions, segments, digests, input coverage,
and expected outputs still need to be pinned by #178.

The executed qualification that followed is
[`proofs/public-workloads`](../../proofs/public-workloads/README.md). It pins
the exact artifacts and records the results.

## Recommended starting set

| Path | Target | Input | Reference |
| --- | --- | --- | --- |
| Shared library with real recorded traffic | comma.ai opendbc safety logic, built through its upstream `libsafety` test harness | One public commaCarSegments segment, initially receive-side CAN | Standalone execution of the same pinned library, plus independent packet/state expectations |
| Single FMU with recorded scalar inputs | Existing public `AccController` FMI 3.0 Co-Simulation FMU | A selected JRC OpenACC CSV window, explicitly mapped to the controller inputs | Independent FMPy execution of that FMU with identical inputs and starts |
| Multiple FMUs | Existing public `AccController` and `AccPlant` pair | Begin with the existing authored maneuvers; qualify a measured lead-vehicle maneuver separately | Existing independent closed-loop reference, extended for the selected maneuver |

These have different evidential roles. Real recordings supply realistic input;
executing the same artifact through a separate driver checks SiL integration.
Neither implies that a public ACC controller reproduces a proprietary vehicle's
controller. A changed controller in a closed loop must be evaluated against a
live plant; replaying the original ego trajectory would break that feedback.

## Shared library: opendbc and commaCarSegments

The upstream [libsafety Python bridge](https://github.com/commaai/opendbc/blob/master/opendbc/safety/tests/libsafety/libsafety_py.py)
builds a shared object with a C compiler and loads it through CFFI. Its exposed
calls include receive/transmit hooks, safety-mode selection, state queries, and
an explicit timer setter. This offers an existing automotive C library that a
SiL Process participant can adapt without modifying its algorithm. Its harness
is a testing interface, not a promised stable supplier ABI; pin its revision.

Upstream already supplies a [recorded-drive replay driver](https://github.com/commaai/opendbc/blob/master/opendbc/safety/tests/safety_replay/replay_drive.py).
Source inspection shows timestamp ordering, timer updates, mode/parameter
selection, and initialization/warm-up logic. Preserve these decisions explicitly
when comparing a SiL adapter to the standalone driver. Build the library during
bundle preparation: the upstream bridge's lazy compilation is unsuitable for an
offline runtime that intentionally contains no compiler.

[commaCarSegments](https://huggingface.co/datasets/commaai/commaCarSegments/blob/main/README.md)
contains actual recorded vehicle CAN traffic in compressed openpilot logs and
provides an index and LogReader example. Download one segment, not the full
dataset. The [dataset card](https://huggingface.co/datasets/commaai/commaCarSegments)
identifies its license as MIT, and [opendbc's license](https://github.com/commaai/opendbc/blob/master/LICENSE)
uses the MIT terms.

Do not assume full transmit replay coverage: the publisher's [dataset announcement](https://blog.comma.ai/096release/)
specifically describes `can` and `carParams`. Inspect the chosen segment for
`sendcan` before claiming a transmit-hook test. A receive-side slice can compare
packet acceptance and exposed state with standalone execution. Testing recorded
transmit decisions requires a suitable richer public route or separately
declared generated test messages. Mode compatibility between the recording and
the selected source revision must also be checked.

This is a small automotive decision-logic workload. It is not a complete
perception/planning stack or a vehicle-dynamics model.

## Real scalar recordings: JRC OpenACC

The [JRC catalogue](https://data.jrc.ec.europa.eu/dataset/9702c950-c80f-4d2f-982f-44d06ea0009f)
describes measured car-following trajectories from ACC and manual-driving
experiments and explicitly allows anonymous access without registration.
The [AstaZero directory](https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/TransportExpData/JRCDBT0001/LATEST/AstaZero/)
contains CSV recordings, experiment notes, and vehicle specifications. Individual
listed CSVs are roughly 6–17 MB, making a small selected window practical.
The dataset's [copyright notice](https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/TransportExpData/JRCDBT0001/LATEST/copyright.txt)
states CC BY 4.0, with attribution and indication of changes.

Before use, inspect a chosen file's columns, units, time grid, vehicle ordering,
and missing-data conventions. Define how speed, relative speed, and bumper gap
map into ACC inputs; do not mistake coordinate separation for bumper gap. If a
lead-acceleration maneuver is derived from velocity, pin its differentiation and
filtering policy. Evaluate the public FMU against an independent execution of
that same model, rather than treating the recorded OEM response as its expected
output.

## FMUs available now and candidates for later

The repository already contains a public [ACC acceptance workflow](../../proofs/acc-fmi/INSTALL.md)
for source-available controller and plant FMUs, built using PythonFMU3 and
compared against FMPy. It exercises scalar FMI 3.0 Co-Simulation on the supported
Linux platform. Reusing these models with a new recorded input is a practical
adoption exercise, but is explicitly repository-authored model evidence rather
than a new independent automotive supplier model.

The Modelica Association's [Reference FMUs](https://github.com/modelica/Reference-FMUs)
provide BSD-2-Clause source and downloadable FMI artifacts for importer tests.
They are useful independent FMI fixtures; Feedthrough and numerical examples
should not be described as production ADAS models. Select supported variables
and capabilities rather than assuming every reference FMU fits SiL's profile.

[Project Chrono's FMI module](https://api.projectchrono.org/group__chrono__fmi.html)
supports FMI 2 and 3. Its [vehicle tutorials](https://api.projectchrono.org/tutorial_table_of_content_chrono_fmi.html)
describe vehicle, tire, and path-follower co-simulation FMUs. This is a stronger
third-party dynamics candidate after the lightweight path works. Build cost,
artifact interface/version, required variable types/resources, and compatibility
with SiL remain unverified; it is not a confirmed drop-in replacement.

## Alternatives and boundaries

- [esmini](https://github.com/esmini/esmini) supplies an existing shared-library
  scenario engine; this repository already has a [qualified adapter](../../proofs/esmini/README.md).
  It is a low-friction fallback for the library path. Scenario-generated
  recordings must be identified as synthetic, and feeding external recorded
  control/state inputs still needs an explicitly qualified adapter path.
- [Open Car Dynamics](https://github.com/TUMFTM/Open-Car-Dynamics) is an Apache-2.0
  C++ dynamics candidate, but its README says that significant AV21 calibration
  parameters are confidential. Its reported validation does not establish that
  a complete matching recorded-input/model/calibration bundle is publicly
  reproducible.
- [openpilot demo replay](https://github.com/commaai/openpilot/blob/master/docs/how-to/replay-a-drive.md)
  can replay a public route without hardware. A full openpilot integration is a
  larger dependency and process-coordination task than the selected library
  slice; it is not needed to start this roadmap.

## Implication for the roadmap

#178 should be executable public-artifact qualification, not a request for the
maintainer's company data. Keep it open until selected artifacts and independent
reference procedures are actually qualified. #193 and #194 remain blocked on
that work and their implementation prerequisites, rather than on private access.
Distinguish public-model adoption acceptance from production-vehicle validation.
Performance conclusions about this workload must not be generalized to an
unavailable company vECU or treated as satisfying #125's real-vECU gate.
