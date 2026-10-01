# Coupling and substituting FMUs

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Couple FMUs through Channels

`sil-fmu-couple` writes the Manifest of a Run of FMUs that are coupled
through Channels. Each FMU is its own Process participant, and each
connection is a field of an ordinary Channel. A coupling document states
every choice. The command checks the document against the FMUs before
anything runs, then writes an ordinary canonical Manifest in which each FMU's
command is the [FMI importer](fmi.md#fmi-30-co-simulation-importer)'s. The kernel
and the importer do not change. A hand-written Manifest with the same bytes
is the same Run. A connection that carries an instant between two Slots, such
as a bus model's transmission time, is not a Channel field. For that, use
a [group](fmi.md#connected-fmus-one-participant-one-bus) (ADR 0001).

[examples/fmu-coupling/](../examples/fmu-coupling/) holds two documents:

| File | Run |
| --- | --- |
| `acc.json` | the ACC controller/plant loop of `proofs/acc-fmi` (#148): one Period of Latency in each direction |
| `feedback.json` | a delayed feedback loop between two `Feedthrough` instances |

From the checkout root, with the staged installation on `PATH`:

```sh
workdir=$(mktemp -d "$HOME/sil-fmu-couple.XXXXXX")
fmu=tests/fixtures/reference-fmus/3.0/Feedthrough.fmu
sil-fmu-couple examples/fmu-coupling/feedback.json \
    --fmu left "$fmu" --fmu right "$fmu" \
    -o "$workdir/feedback.json" --receipt "$workdir/feedback.receipt.json"
sil-run "$workdir/feedback.json" -o "$workdir/run-1.mcap"
sil-run "$workdir/feedback.json" -o "$workdir/run-2.mcap"
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
```

`make example-fmu-coupling` runs the same sequence from the source tree.

The coupling document is JSON. Every key is required, and nothing has a
default:

```json
{
  "sil_fmu_coupling": 1,
  "duration_ns": 5000000000,
  "fmus": {
    "plant": {"step_period_ns": 10000000, "priority": 0, "start": [],
              "hold": ["lead_accel_mps2", "initial_lead_position_m"]},
    "controller": {"step_period_ns": 10000000, "priority": 1, "start": [],
                   "hold": []}
  },
  "channels": {
    "sensing": {
      "publisher": "plant", "latency_ns": 10000000,
      "fields": [{"name": "gap_m", "type": "f64", "variable": "gap_m",
                  "unit": "m"}, "..."],
      "subscribers": {
        "controller": {"capacity": 2, "overflow": "fail",
                       "bind": {"gap_m": "gap_m", "...": "..."}}
      }
    },
    "...": {}
  }
}
```

- `--fmu <name> <path>` gives the archive of each FMU the document names.
  The command writes the path as an argument of its own, resolved to an
  absolute path, so `sil-run` records its SHA-256 in the Run's provenance.
- Each FMU states its `step_period_ns` and its `priority`. Each FMU has its
  own priority, so a lower priority runs first in a Slot, and a name never
  decides the order. To change the order, change a priority.
- Each Channel has one `publisher` and states its `latency_ns`. Its schema has
  the Channel's name and its `fields`. Each field names the publisher's output
  `variable` and states its `unit`, or `null` for no unit.
- Each subscriber states its bounded route and `bind`s fields of the Channel
  to its inputs. One bound field is one connection.
- `start` gives an input or a parameter a start value, with its unit. A
  connected input holds its start value until its first delivery. `hold`
  names the inputs that keep the start value the FMU declares for the whole
  Run.

The Manifest is canonical: the command sorts FMUs, Channels, routes and start
values, so a document that declares them in another order gives the same
bytes.

Before it writes a Manifest, the command rejects (exit 2) with the reason:

- an FMU the importer cannot drive, or a mapping the importer would reject.
  These are the checks of
  [`sil-fmi-inspect`](fmi.md#inspecting-an-fmu-before-a-run). The command does not
  load an FMU's binary;
- an FMU with no `--fmu` archive, and an archive for no declared FMU;
- a publisher or subscriber that is not a declared FMU, and a bound field
  that the Channel does not carry;
- a connection that does not start at an output of its publisher or end at
  an input of its subscriber, or whose two ends declare different types or
  dimensions;
- a stated unit that is not the publisher's unit, and a connection between
  two different units. The importer converts no unit. Author the conversion
  explicitly: add a converting FMU to the document, between the two;
- an input that two connections feed, an input that is not connected, not given a
  start value and not held, a connected input that has no start value, and a
  held input whose FMU declares no start value;
- a missing or `null` `latency_ns`;
- two FMUs with the same priority;
- a cycle of zero-Latency connections. The diagnostic names the cycle, for
  example `left -[left.value]-> right -[right.value]-> left`;
- a zero-Latency connection whose publisher does not run before its
  subscriber. The diagnostic names an order that the connections allow;
- a route that holds more Messages than its `capacity` under the `fail`
  policy (see the plan below);
- a `duration_ns` that is not a multiple of the `step_period_ns` of each FMU
  (see [Timing across several periods](fmu-coupling.md#timing-across-several-periods));
- an unknown or duplicate key, and every value the Manifest builder refuses.

**The zero-Latency check is a conservative authoring profile.** It reads the
declared connections only. It does not find or solve an algebraic loop in the
models' equations, and it refuses a structural cycle even where the models
would not form an algebraic loop. A loop with a Latency above zero on at
least one of its Channels is an ordinary feedback loop, and it stays
supported: `feedback.json` is one.

The command prints the plan of the Run on standard output. The receipt (at
`--receipt`, `"sil_fmu_coupling_receipt": 1`) holds the same plan, each
connection with its type and unit, and the SHA-256 of the document, each FMU
and the Manifest. For `feedback.json`, the plan starts:

```text
Duration 50 ms: Slots at 0 <= t < 50 ms; the last Step of each FMU ends on the Duration
execution order in each Slot (lowest priority first):
  1. left  priority 0, period 10 ms, 5 Steps
  2. right  priority 1, period 10 ms, 5 Steps
same-Slot connections: none
Channels (a Message published at t holds its publisher's outputs at t + the publisher's period):
  left.value, published by left with Latency 10 ms: value = Float64_continuous_output
    to right: route capacity 2 (fail), at most 2 Messages in the route
      right.Float64_continuous_input holds 0 (FMU start) until the first delivery
      at 0 ms: no Message yet, holds the start value
      at 10 ms: takes the Message published at 0 ms (values at 10 ms)
    last Message published at 40 ms (values at 50 ms): no activation takes it; only the Recording holds it
```

- **Publication.** The importer publishes at the start of a Step the values
  at its end. A Message published at `t` holds the publisher's outputs at `t`
  plus the publisher's period.
- **Delivery.** A Message is visible one Latency after its publication. The
  subscriber takes it at its first activation at or after that time, and
  writes it before its Step. A zero-Latency Message reaches a subscriber in
  the same Slot, because its publisher runs first.
- **Activations.** For each route, the plan lists the input that each
  activation of the subscriber steps on, until the pattern repeats. The
  pattern repeats after one common period of the two FMUs, counted from the
  first time a Message can be visible. The plan lists at most 12
  activations of a route and states how many more come before the pattern
  repeats. The first activations hold the start value. The plan states whether the start value comes from the document or
  from the FMU.
- **Route bound.** A route holds a Message from its publication to its
  delivery. When the publisher runs first in a Slot, its new Message is in
  the route before the subscriber takes the previous one. The plan states
  the most Messages each route holds in the Run. In `acc.json`, the sensing
  route holds two, and a sensing Latency of two Periods would make it three.
  Under `drop_newest`, a route refuses a Message that finds it full. The plan
  lists the deliveries that the kernel makes and the dropped publications.

Supported limits:

- **Channel fields only.** A connection carries a `Float64`, `Boolean`,
  `Binary`, `Float32`, `Int32`, `UInt32`, `UInt64`, `UInt8` or `Int64` value of one output, as
  the single-FMU importer maps it, in the field type of that FMI type. A
  fixed-size numeric array is one field whose `count` is its value count
  ([FMI importer](fmi.md#fixed-size-numeric-arrays)), and both ends declare
  the same dimensions. Clocks, network terminals and FMI-LS-BUS stay in a
  group.
- **Fixed periods.** Each FMU steps on its own fixed Period, from 0, and
  the Duration is a multiple of each Period. There is no adaptive step, no
  rollback and no change of a Period during the Run.
- **FMUs only.** The Run holds the coupled FMUs. The proof in
  [proofs/acc-fmi/](../proofs/acc-fmi/README.md#authored-coupling-187) runs the
  rendered ACC Manifest and compares it with the independent FMPy trajectory.

### Timing across several periods

The FMUs of one document can have different periods. The rules of the plan
above apply to each route. This section states the timing contract for such
a Run. The tests (`TestSeveralPeriods` in `tests/test_fmu_coupling.py`) use
this Run: `ball` (`BouncingBall`) publishes its height, and two `Feedthrough`
instances, `fast` and `slow`, publish the height they step on. Each FMU
states its start value in the document.

| FMU | Period | Priority | Steps in 120 ms | Takes | Latency of the route |
| --- | --- | --- | --- | --- | --- |
| `ball` | 20 ms | 0 | 6 | nothing | |
| `fast` | 10 ms | 1 | 12 | `ball` | 10 ms |
| `slow` | 30 ms | 2 | 4 | `fast` | 10 ms |

All three FMUs have an activation in the Slots at 0 and 60 ms. In such a
Slot, they run in the order of their priorities.

**Four times of one Message.** The contract keeps them apart:

| Time | What it is | For a Message of `ball` published at 20 ms |
| --- | --- | --- |
| Sample time | The virtual time that the values describe. For an FMU output, this is the end of the Step, the FMU endpoint: publication Slot + the publisher's period. | 40 ms |
| Publication Slot | The Slot of the Step that publishes the Message. The Recording stores this time. | 20 ms |
| Visible time | Publication Slot + the Channel's Latency. | 30 ms |
| Delivery time | The first activation of the subscriber at or after the visible time. The subscriber writes the value before its Step over `[delivery, delivery + its period]`. | 30 ms (`fast`) |

A replayed Message has a sample time too. The Replay participant publishes
it at its recorded time, and a
[comparison contract](comparison.md#compare-a-trajectory-against-a-reference) states the
offset from its publication to its sample time.

**Which input each activation steps on.** Each activation writes the
Messages it takes in Publish order, so it steps on the newest one. An
activation that takes no new Message holds the last value. Before the first
delivery, the input holds its start value. The plan for this Run states:

```text
  ball, published by ball with Latency 10 ms: h = h [m]
    to fast: route capacity 1 (fail), at most 1 Message in the route
      fast.Float64_continuous_input holds 1.25 (document start) until the first delivery
      at 0 ms: no Message yet, holds the start value
      at 10 ms: takes the Message published at 0 ms (values at 20 ms)
      at 20 ms: no new Message, holds the one published at 0 ms (values at 20 ms)
    last Message published at 100 ms (values at 120 ms): fast takes it at 110 ms
  fast, published by fast with Latency 10 ms: h = Float64_continuous_output [m]
    to slow: route capacity 4 (fail), at most 4 Messages in the route
      slow.Float64_continuous_input holds 2.5 (document start) until the first delivery
      at 0 ms: no Message yet, holds the start value
      at 30 ms: takes 3 Messages and steps on the newest, published at 20 ms (values at 30 ms)
    last Message published at 110 ms (values at 120 ms): no activation takes it; only the Recording holds it
```

`fast` takes a new height at every second activation and holds it in
between. `slow` takes three Messages of `fast` at once, and the two older
ones have no effect.

**Initialization.** Each FMU initializes alone, at virtual time 0, before
the first Slot. The importer sets the start values, then enters and exits
the initialization mode of that one FMU. No Message is delivered during
initialization, and no FMU sees the initial outputs of another FMU. There is
no joint initialization and no solver for the initial values of the coupled
FMUs. Thus each connected input needs a start value, from the document or
declared by the FMU, and the plan states which one. A Recording holds no
initial output: the first Message of an FMU, published at 0, holds its
values at the end of its first Step.

**Duration.** A Run has the Slots `0 <= t < Duration`, a half-open interval.
The kernel steps an FMU at each of its Slots, and each Step covers one full
period `[t, t + period]`. Thus the last Step of an FMU starts at
`Duration - period` and ends on the Duration only if the Duration is a
multiple of the period. `sil-run` does not clip the last Step: for a
hand-written Manifest whose Duration is not a multiple, the FMU steps past
the Duration, and that behavior does not change. This authoring profile
refuses such a Duration before it writes the Manifest. The diagnostic names
each FMU, the last Step and a Duration that fits, for example:

```text
sil-fmu-couple: error: Duration 100 ms is not a multiple of the period of FMU 'slow' (period 30 ms). The last Step of 'slow' would start at 90 ms and end at 120 ms, after the Duration. [...] such as 60 ms or 120 ms
```

**The final samples.** The last Message of each FMU holds its values at the
Duration. A subscriber takes it only when it is visible before the
Duration and a later activation of the subscriber exists. For each Channel,
the plan states who takes the last Message. In this Run, `fast` takes the
last height of `ball` at 110 ms. The last Messages of `fast` (visible at
120 ms) and `slow` (no subscriber) reach no participant.

An in-run check, such as a Test participant, sees only what it takes in a
Slot before the Duration. It can see a final sample: a Test participant
scheduled like `fast` takes the last height of `ball` at 110 ms. The Run
has no Slot at the Duration, and shutdown does not step a participant.
Thus an in-run check cannot see:

- a Message that becomes visible at or after the Duration, such as the last
  Message of `fast`;
- a Message published in the checker's own last Slot or after it, with a
  Latency above zero.

Only the Recording holds these Messages.

Check the final samples post-hoc. `sil-compare` observes each Message at
its sample time: `actual_offset_ns` is the publisher's period. With an
observation grid that ends on the Duration, the final sample is compared
like each other sample. The tests compare the Recording of this Run with a
reference computed from the hand-written consumption tables. The same
comparison with a wrong final sample of `slow` fails, and the first
divergence is at 120 ms.

### Replace one FMU with its Recording

`sil-fmu-substitute` removes one live FMU from a recorded coupled Run and
replays the Channels it fed the other FMUs. The other FMUs are the retained
subsystem. The Channels of the removed FMU that a retained FMU takes are the
replacement boundary. The command writes the replacement Manifest and a
[comparison contract](comparison.md#compare-a-trajectory-against-a-reference). The
contract says whether the retained outputs of the replacement Run are those
of the original Run. From the checkout root, with the staged installation on
`PATH`:

```sh
workdir=$(mktemp -d "$HOME/sil-fmu-substitute.XXXXXX")
fmu=tests/fixtures/reference-fmus/3.0/Feedthrough.fmu
# 1. Record the coupled Run.
sil-fmu-couple examples/fmu-coupling/feedback.json \
    --fmu left "$fmu" --fmu right "$fmu" -o "$workdir/original.json"
sil-run "$workdir/original.json" -o "$workdir/original.mcap"
# 2. Replace `left` by its recorded Channel `left.value`.
sil-fmu-substitute examples/fmu-coupling/feedback.json \
    --fmu left "$fmu" --fmu right "$fmu" --replace left \
    --recording "$workdir/original.mcap" -o "$workdir/replacement.json" \
    --contract "$workdir/contract.json" --receipt "$workdir/receipt.json"
# 3. Run the replacement twice; one Manifest gives one Recording.
sil-run "$workdir/replacement.json" -o "$workdir/replacement-1.mcap"
sil-run "$workdir/replacement.json" -o "$workdir/replacement-2.mcap"
cmp "$workdir/replacement-1.mcap" "$workdir/replacement-2.mcap"
# 4. Compare the Messages with the original Run's.
sil-compare "$workdir/contract.json" "$workdir/replacement-1.mcap" \
    "$workdir/original.mcap"
```

`make example-fmu-substitution` runs the same sequence from the source tree.

Give the command the document and the archives that the original Run was
authored from. It makes every check of `sil-fmu-couple` again, and it then
checks that they author the Manifest whose hash the Recording carries. The
Manifest names each archive by its absolute path, so the archives must be at
the paths the Run named. The command refuses a Recording that no Run
wrote, and a Recording that does not hold one Message of each compared
Channel at each Slot of its publisher, such as that of a failed Run.

**What the replacement preserves.**

| Property | How |
| --- | --- |
| Schema and Latency | Each boundary Channel and each retained Channel is declared as in the original Manifest. |
| Retained models, initialization and parameters | Each retained FMU's participant declaration is the original one: its command, with every start value and binding, its Period, priority and routes. Only a route to the removed FMU is dropped. |
| Publication times | The Replay participant publishes each recorded Message at its publication Slot. The Sample time stays one Period of the removed FMU later. |
| Total same-time order | The Replay participant has the removed FMU's priority. It publishes the Messages of one Slot in the recorded Publish order, at the removed FMU's place among the Slot's activations. Each Slot's Publish order, and the Messages each route holds, are those of the original Run. A zero-Latency boundary route delivers inside the Slot, as it did. |
| One publisher per Channel | The Replay participant takes the removed FMU's name and is the one publisher of each boundary Channel. The Manifest holds the digest of the Recording it replays. |

The Replay participant's place is its Manifest key `priority`
(`Manifest.add_replay(..., priority=...)`). It orders the Replay participant
among a Slot's activations as it orders a Process participant. A Replay
participant without the key publishes before every activation of a Slot, as
before, so an existing Manifest keeps its bytes and its Run.

A Channel of the removed FMU that no retained FMU takes is not in the
replacement Run. The receipt (`"sil_fmu_substitution_receipt": 1`) states
the boundary, the number of Messages replayed on each boundary Channel and
when each retained FMU first takes one. Until then, a retained input holds
its start value. The receipt also holds the digests of the document, the
archives, the Recording, the original Manifest, the replacement Manifest and
the contract.

**What is compared.** The contract compares each retained output and each
boundary Channel at each Sample time of its publisher, from the end of the
first Step through the Duration. It compares integer fields exactly and
float fields with zero tolerance. It leaves out no warm-up: the replacement
starts from the same initialization, so it compares the first Sample times,
where a retained input still holds its start value, and the final one at the
Duration. The two Runs have different Manifests, so their Recordings differ
in bytes, and `sil-compare` compares their Messages. A byte comparison
(`cmp`, `sil-check`) is only for two Runs of the same Manifest.

The tests (`tests/test_fmu_substitution.py`) do this for two compositions:

| Composition | Replaced | Retained | Compared |
| --- | --- | --- | --- |
| `feedback.json`, equal Periods of 10 ms | `left` | `right` | 5 Sample times each of `left.value` and `right.value`, 10 to 50 ms |
| the Run of [Timing across several periods](fmu-coupling.md#timing-across-several-periods) | `ball` (20 ms) | `fast` (10 ms), `slow` (30 ms) | `ball` 6, `fast` 12, `slow` 4 Sample times, through 120 ms |

Two negative controls edit the replacement Manifest. The comparison has to
fail each one and name the first divergence:

| Control | Feedback loop | Several Periods |
| --- | --- | --- |
| Wrong Latency: the boundary Channel at 20 ms instead of 10 ms | `right.value` at 20 ms: 0 (the start value) instead of 1.5 | `fast` at 20 ms: 1.25 (the start value) instead of the height of 0 ms |
| Omitted initial input: the replayed Message of 0 ms dropped | `left.value` missing at 10 ms; `right.value` then holds its start value | `ball` missing at 20 ms; `fast` holds its start value at 20 and 30 ms, and `slow` steps on it |

**When the replay is equivalent.** A recorded boundary is the removed FMU's
response to the original Run. It does not respond to anything else. In a
closed loop, such as `feedback.json`, the replacement is equivalent to the
original Run only for the unchanged retained experiment. A changed retained
model, start value, parameter, Period or Latency changes what the removed FMU
would have computed. Such a change needs a live Run of the removed FMU, not a
replay. The command refuses such a change, because the Recording is not a Run
of the changed experiment. The tests show the reason. Start `right` at 0.75
instead of 0: live, `left` sends 0.75 back at 10 ms. The recorded `left`
still sends the original 0, so `left.value` diverges at 20 ms.

This workflow is for signal-coupled FMUs. It does not change the replay of a
bus terminal. In [Replaying one source at the boundary](fmi.md#replaying-one-source-at-the-boundary),
a replayed operation is raised at the event time its Message states, through
a zero-Latency boundary Channel
([ADR 0002](adr/0002-a-replayed-terminal-lands-on-its-own-instant.md)).
The CAN proof in [proofs/fmi-ls-bus](../proofs/fmi-ls-bus/README.md#one-node-replaced-by-its-own-recording)
shows that for the CAN demo FMUs. A signal-coupled Message states no event
time. Its Sample time is one Period after its publication Slot, and replay
keeps both of these times.
