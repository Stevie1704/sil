# Comparing trajectories

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Compare a trajectory against a reference

`sil compare` compares one Recording with a reference Recording under a
comparison contract, and gives a pass/fail report. The contract states each
decision; the command infers nothing from the data. A reference in another
format becomes a Recording through `sil recording csv` first.

The worked example compares the retained SiL Recording of the ACC
`plant-accelerate` qualification case with the independent fmpy trace of the
same FMU. [examples/compare/plant-accelerate.reference.csv](../examples/compare/plant-accelerate.reference.csv)
is that trace, one row per time and one column group per FMU instance;
[examples/compare/reference-mapping.json](../examples/compare/reference-mapping.json)
converts it; [examples/compare/contract.json](../examples/compare/contract.json)
is the contract. With the installed wheel on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d)
sil recording csv examples/compare/reference-mapping.json \
    examples/compare/plant-accelerate.reference.csv -o "$workdir/reference.mcap"
sil compare examples/compare/contract.json \
    proofs/acc-fmi/evidence/plant-accelerate-1.mcap "$workdir/reference.mcap"
sil compare ... --json                          # the same report as JSON
```

A contract names each compared Channel and states, for that Channel:

| Key | What it states |
| --- | --- |
| `reference_channel` | the reference Channel with the expected values; the default is the same name |
| `actual_offset_ns`, `reference_offset_ns` | observation time = recorded time + offset, per side. The FMU importer publishes at the start of a Step the state at the end of that Step, so its offset is the Step period. An input Channel of the same Run can have offset `0`. No offset is shared between Channels |
| `observations` | the exact observation times: `{"times_ns": [...]}`, or `{"start_ns", "stop_ns", "step_ns"}` with both ends included |
| `fields` | a rule for each field of the actual schema: `"exact"` for an integer field, `{"atol": a, "rtol": r}` for a float field, or `"ignore"` |

The top-level `evaluation` window, `{"from_ns", "to_ns"}` with both ends
included, applies to every Channel. An observation time outside it is not
compared, so a warm-up is outside the window.

The rules:

- A float value passes when `abs(actual - reference) <= atol + rtol *
  abs(reference)`. A NaN or an infinity on either side fails. No rule accepts
  one.
- At each observation time, each side must have exactly one Message. A missing
  Message fails. Two Messages fail as ambiguous. A Message whose observation
  time is not in the contract is not compared. Nothing is interpolated and no
  tolerance is fitted.
- Wrong coverage fails: a schema field that the contract does not name, a
  contract field that the schema does not declare, a rule that does not fit
  the field type, a compared field that the reference Channel does not have,
  a Channel that a Recording does not declare, and a Channel whose every
  field is `"ignore"`. A Channel that the contract does not name is not
  compared. A contract that repeats a key is refused.
- An array field takes its rule element by element. The reference field must
  be an array of the same count and kind. The field fails once per
  observation, and its divergence states the first element that diverges as
  `element`, with both whole arrays as the values.
- The final publication is compared like all other publications. With the
  Step-period offset, the last Step's state lands on the Duration, where no
  participant of the Run can observe it.

The report states the verdict, the digest of the contract and of both
Recordings, per Channel the count of observations, of checked, failed,
non-finite, missing and ambiguous Messages, and the total count of
divergences. It also states the first divergence: the earliest observation
time, then the contract's Channel and field order. That entry gives the kind
(`value`, `nonfinite`, `missing-actual`, `missing-reference`,
`ambiguous-actual`, `ambiguous-reference`), the Channel and field, the
observation time, the recorded time on each side (the MCAP log time; a
Recording states no other source time), the actual and expected
values, the tolerance with its allowed error, and the absolute error. For a
missing or ambiguous Message the field is `null`, and the values are an object
of the compared fields; an ambiguous side gives a list of times and values.
The JSON report is strict JSON: a non-finite value is the string `"nan"`,
`"inf"` or `"-inf"`.

A reference comparison is not a determinism check. It tells whether two
trajectories agree within a contract, often across two Manifests or two
engines. It does not tell whether a Run reproduces. `sil check` bit-compares
two Recordings of one Manifest for that. Each report states this in its
`determinism` key. Domain KPIs stay with the consumer, and the retained proof
results stay attributable to their own contracts.

| Exit | Verdict |
| --- | --- |
| `0` | `pass` |
| `1` | `fail`: a divergence or wrong coverage |
| `2` | usage error: the command line, the contract or a Recording cannot be read |

The contract carries `"sil_comparison": 1` and the JSON report carries
`"sil_comparison_report": 1`; a change to their keys raises that number.
