# Running simulations

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below use the installed wheel and native runtime on `PATH` and run
from the directory containing your Manifest. A source checkout is not needed.

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
- **Determinism check**: `sil check manifest.json`
  runs twice and bit-compares (exit 3 on violation). Enforced in CI.
- **Declared memory footprint**: `sil footprint manifest.json` reports
  the worst-case payload memory a manifest promises, from route capacities and
  arena slot counts alone — no run, no benchmark. It also names any route that
  declares no capacity, whose worst case is unbounded.

Exit codes: `0` ok, `1` run/test failure, `2` Manifest error, `3` determinism
violation (sil check).

## Validate a Manifest

`sil-run` stops at the first Manifest error. `sil manifest validate` checks a
Manifest without a Run and lists every error:

```sh
sil manifest validate manifest.json
sil manifest validate manifest.json --json
```

It applies the rules of `sil-run`: keys, types, ranges, references between
Schemas, Channels and Participants, and one publisher for each Channel. It
also reads the files that the Manifest names, and it compares the Channels
and Schemas of a replayed Recording with the Manifest. Paths resolve against
the Manifest directory, as in a Run. A bare Process participant command is looked up on the `PATH` of
the check, and `sil-run` uses its own `PATH`. The check needs only the wheel,
not `sil-run`.

Only a Run finds the Manifest errors that come from the code of a
Participant or from the system: a Native library that does not load, does not
export `sil_participant_init`, or fails its init; a Process participant that
answers `init` with `fail`; a Recording Message that does not decode; an
Arena that the system cannot map; and a missing Clock shim.

`--json` writes the `sil_manifest_validation` report (version 1) to stdout:

- `manifest`: the Manifest path, as given.
- `valid`: `true` or `false`.
- `errors`: one object for each error, with `path` (a JSON Pointer, RFC 6901,
  to the value at fault; `""` is the whole document), `message`, and `hint`
  (a short fix).

The exit codes are `0` valid and `2` not valid, as for `sil-run`.

## Start a Participant

`sil new participant --process NAME` writes a Python Process participant, and
`sil new participant --native NAME` writes a C Native participant. Each comes
with a `manifest.py` script. This script writes a Manifest that runs the
Participant. `NAME` must be a
C identifier. The files go to `./NAME`, or to `--dir DIR`. The command does
not overwrite a file.

```sh
sil new participant --process brake
python3 brake/manifest.py brake/manifest.json
sil manifest validate brake/manifest.json
sil check brake/manifest.json
```

The generated Participant publishes one Message on the Channel `NAME.out` at
each Step. It runs and passes `sil check` with no edits. For a Native
participant, `manifest.py` first compiles the library with `$CC` (default
`cc`) against the headers installed beside `sil-run`. To give a different
include directory, set `CFLAGS=-I<dir>`.

## Provenance side-car

Each Run that has loaded its Manifest writes a deterministic JSON record next
to its Recording: `<recording>.provenance.json`. `--provenance PATH` selects
another path. With `--no-recording`, the record goes next to the default
`out.mcap` location.

### Provenance mode

`--provenance-mode full|external` selects who owns artifact identity. The
default is `full`. `sil-run`, `sil run` with or without diagnostics,
`sil benchmark` and `sil profile` accept the option. An unknown or a repeated
value is a usage error (exit `2`), and no Run starts. The option is an
invocation policy. It is not a Manifest field, so it does not change the
Manifest hash.

| Operation | `full` | `external` |
| --- | --- | --- |
| Hash the exact Manifest bytes | Yes | Yes |
| Resolve paths, check that files can be opened, check topology | Yes | Yes |
| Hash the runner, Clock shim, Native libraries, Process executables and files that commands name | Yes | No |
| Hash the output Recording | Yes | No |
| Verify a Replay input against its expected hash | Yes | Yes |
| Write the side-car | Yes | Yes |

The mode does not change Recording bytes, virtual scheduling, Message order,
Participant behavior or exit codes. An artifact that `full` rejects because it
cannot be resolved or opened, `external` also rejects. A Replay hash mismatch
is a configuration error in both modes.

### Schema version 2

The record has `schema_version` 2 and a top-level `provenance_mode`. All other
top-level members keep their names: `manifest_hash`, `sil`, `machine_class`,
`run_exit_code`, `artifacts` and `errors`.

Each digest is an object with a `digest_state` member. An artifact object also
has `path`.

| `digest_state` | Meaning | `path` | `sha256` | `reason` |
| --- | --- | --- | --- | --- |
| `calculated` | SiL calculated the digest of this Run. | Canonical | 64 hex digits | Not present |
| `omitted` | External ownership: SiL did not calculate a digest. It makes no statement about the file contents. | Canonical | null | Not present |
| `unresolved` | The path does not resolve to a regular file. | As requested | null | Text |
| `failed` | SiL could not read or hash the file. | Canonical | null | Text |
| `not_requested` | `recording` only: the Run was asked to write no Recording. | Not present | null | Not present |

```json
{"path": "/opt/sil/libcontroller.silp", "digest_state": "omitted", "sha256": null}
```

- `artifacts.runner`, and the `library`, `executable` and `file` member of each
  entry, always hold an artifact object. `artifacts.clock_shim` is null only
  when no Participant asks for the Clock shim.
- `recording` is a digest object without `path`: `calculated`, `omitted`,
  `failed` or `not_requested`. `omitted` does not say that a Recording file
  exists. See `run_exit_code` for the outcome.
- An empty or zero digest never means omission. A null `sha256` never means
  that a check passed.
- `errors` still lists every `unresolved` or `failed` artifact and a failed
  Recording digest.
- Two equivalent Runs in one mode give the same record bytes. A record of
  `full` and a record of `external` differ.

### Recording digest

In `full` mode, the runner calculates the digest while it writes the Recording.
The digest includes each byte in file order: the header, Messages, chunks,
indexes, metadata, footer and final magic. Memory use does not grow with the
file. A normal Run does not read the file again after close. In `external` mode,
the runner does no output digest work.

| Case | Run outcome | `recording` in `full` mode |
| --- | --- | --- |
| The Recording closes correctly | Exit `0` | `calculated`, from the bytes written |
| A Participant fails, and the partial Recording closes correctly | Exit `1` | `calculated`, from the bytes written |
| A write or the close fails, for example on a full disk | Exit `1` | `calculated`, from one read of the file after close, so the digest is of the bytes on disk. `run_exit_code` and the failure report show that this file is not complete. |
| A write or the close fails, and the file cannot be read | Exit `1` | `failed`, with an entry in `errors` |
| The Run opened no Recording, for example after a Manifest error | Exit `2` | `failed`, with an entry in `errors`. An older file at that path is not the output of this Run, so SiL does not digest it. |

After the first failed write, the runner writes no more bytes to the file, and
the Run stops with exit `1`. In `external` mode the outcome is the same and
`recording` is `omitted`.

A `calculated` digest identifies the bytes when the runner closed the file. It
does not show that the file is unchanged after that time.

A record with `path` values only is not evidence of file contents. Do not read
an `omitted` digest as a verification, or as a statement that a file did not
change.

### Migrate from schema version 1

| Member | Version 1 | Version 2 |
| --- | --- | --- |
| `schema_version` | `1` | `2` |
| `provenance_mode` | Not present | `full` or `external` |
| Artifact object | `path`, `sha256` | `path`, `digest_state`, `sha256`, and `reason` on failure |
| Artifact that failed | null, and an `errors` entry | An object with `unresolved` or `failed`, and an `errors` entry |
| `recording` with a digest | `{"sha256": ...}` | `{"digest_state": "calculated", "sha256": ...}` |
| `recording` without a Recording | null | `{"digest_state": "not_requested", "sha256": null}` |
| `recording` with a failed digest | null | `{"digest_state": "failed", "sha256": null, "reason": ...}` |

A version 1 record keeps its meaning. A reader that needs a digest must check
`digest_state` in version 2, and must reject a record without a calculated
digest. In this repository, `sil recording export` reads both versions. It
binds a Run outcome to the source only through a digest that SiL calculated
([completeness](visualization.md#completeness)).

### Platform responsibility in external mode

Use `external` when a platform can bind its recorded identities to the actual
runner and inputs that SiL uses, for example with immutable artifacts or an
isolated immutable Run directory. `external` means that the caller owns
artifact identity. It does not mean that SiL verified a digest of the caller,
and SiL accepts no such digest.

The platform must:

- bind the identity of the actual runner and of every input to its execution
  record;
- hash the finalized Recording after the Run;
- associate the Manifest hash with these identities.

The Manifest hash is configuration identity. It is the SHA-256 of the exact
bytes of the Manifest file. It does not identify the contents of files that the
Manifest names: if you replace a library at the same path, the Manifest hash
does not change. If you parse and write the JSON again, the bytes and so the
hash can change.

If a wrapper does the same hashing synchronously, the total work does not
decrease. Other operations keep their own hashing and their defaults:
`sil check`, `sil bundle seal`, `sil bundle verify`, comparisons and the
integrity checks of a profile directory.

## Failure report

When `sil-run` has accepted its command line, a Run that ends with exit `1` or
`2` writes a failure report next to its Recording: `<recording>.failure.json`.
A usage error writes no report. With `--no-recording`, the report goes
next to the default `out.mcap` location, like the provenance side-car. A
successful Run writes no report, and it removes a report that an earlier
failed Run left at the same path. The report adds facts to the diagnostic
line. It does not change the exit code.

```json
{"category":"exit","exit_code":1,"exit_status":3,
 "message":"participant 'controller' exited unexpectedly with status 3 during Step 3 at virtual time 30000000 ns",
 "participant":"controller","schema_version":1,"signal":null,
 "stderr_tail":["Traceback (most recent call last):","..."],
 "step_index":3,"virtual_time_ns":30000000}
```

| Field | Value |
| --- | --- |
| `schema_version` | `1` |
| `exit_code` | the exit code of the Run |
| `category` | what went wrong, from the table below |
| `message` | the first diagnostic line, without the `sil-run: ` prefix |
| `participant` | the name of the Participant that failed, or `null` |
| `exit_status` | the exit status of a Process participant, or `null` |
| `signal` | the signal that ended a Process participant, or `null` |
| `virtual_time_ns` | the virtual time of the last Step sent to the Participant, or `null` before its first Step |
| `step_index` | the 0-based index of that Step among the Participant's Steps, or `null`; always `null` for a Native participant |
| `stderr_tail` | the last 20 lines that a Process participant wrote to stderr |

| `category` | Meaning |
| --- | --- |
| `manifest` | the Run was rejected before any Participant was stepped (exit `2`) |
| `spawn` | the runner could not create the pipes or the process for a Process participant. A command that cannot be executed exits with status `127`, and the report gives `exit` with no Step |
| `protocol` | a Participant broke the Step protocol, for example a wrong `op` or a line that is not JSON |
| `timeout` | a Participant missed its response deadline (`--participant-timeout-ms`) |
| `exit` | a Process participant exited or was killed by a signal before the runner shut it down, or exited non-zero at shutdown |
| `guard` | a Participant exceeded a Step-protocol size guard |
| `participant` | a Participant reported its own failure (`fail`, or a failed test assertion) |
| `interrupted` | `sil-run` received a termination signal |
| `run` | any other Run failure, for example a full subscriber route |

The runner reads the stderr of each Process participant through a pipe. It
writes what it reads to its own stderr unchanged, and keeps only the tail for
the report. The child's stderr is therefore a pipe, not a terminal. Reading it
does not change the Step protocol, the Clock shim or the Recording.

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
sil check manifest.json --participant-timeout-ms 5000
```

The check then reports the run's own exit code and diagnostic — a missed
deadline is a run failure (exit 1), not a determinism violation. The
recorded bytes of a run that answers in time are the same either way, so a
bounded check and an unbounded one report the same digest.

On a violation, the check reports the first difference between the two
Recordings. It compares them in this order:

1. The metadata records.
2. The Channel list, then the Schema of each Channel.
3. The Messages in stored order: Channel, virtual time and payload bytes.

The first difference names its kind (`metadata`, `channels`, `schema`,
`message`, `bytes` or `unreadable`). A `message` difference names the
Channel, the virtual time (`t_ns`) and the Message index in stored order. A
`bytes` difference means that the Recordings differ, but not in the metadata,
Channels, Schemas or Messages. The check reports only the first difference.
For a `message` difference, `run1` and `run2` give the SHA-256 of each
payload, not the payload.

`--keep DIR` keeps both Recordings in `DIR` as `run1.mcap` and `run2.mcap`.
Then read the different Message with `sil recording show DIR/run1.mcap
--channel NAME --from T --to T+1`. Without `--keep`, the check deletes the
Recordings.

`--json` writes the `sil_check_report` (version 1) to stdout. It holds:

- `verdict`: `deterministic` or `violation`.
- `runs`: the SHA-256 of each Recording, and its kept path (null without
  `--keep`).
- `first_difference`: null when the Recordings are bit-identical.

```sh
sil check manifest.json --keep check-out --json
```

A Run that fails writes no report. The check then gives the exit code and
the diagnostic of that Run, as before. The exit codes are: `0` deterministic,
`3` violation, `2` when `DIR` cannot be made, and the exit code of a Run that
fails.

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

### Process participant command

Before the Run starts, `sil-run` finds the first word of each Process
participant command:

- For a bare name, such as `python3`, `sil-run` searches its own `PATH`.
- For a relative path with a `/`, `sil-run` starts from the directory of the
  Manifest.
- `sil-run` uses an absolute path as it is.

The runner starts the file that it found. The provenance side-car records the
command with the path of that file. The side-car also records the SHA-256 of
the file after symbolic links. For a venv `python3`, that file is the base
interpreter. The runner does not search `PATH` for the other arguments. If a
relative argument names a file in the Manifest directory, the runner and the
side-car use the absolute path of that file.

In `full` mode, `sil-run` digests each distinct input file once for each Run.
Participants that name the same file, directly or through a symbolic link, share
one digest. Each Run starts a new digest pass, so a file that changed between
Runs gets a new digest. In `external` mode, `sil-run` digests no input file.

Because of this, the environment of `sil-run` decides which file a bare name
starts. The ACC example uses `python3 -m sil.participant ...`. If the
environment that has `sil` installed is not active, a different `python3`
starts. Then a Participant fails, for example:

```text
No module named 'sil'
sil-run: participant 'controller' exited unexpectedly
```

To correct this, activate that environment. Then start the Run again. You can
also put the `bin` directory of the environment first on `PATH`.

For a Manifest that you write, select one of the two forms:

| Command | Manifest hash | Provenance side-car |
| --- | --- | --- |
| Bare name (`python3`) | Does not change between machines or environments. | Records the file that `PATH` gave. |
| Absolute path (`/opt/venv/bin/python3`) | Changes with the path. The Manifest is tied to one installation. | Records that path. |

Use a bare name if the same Manifest must run from source and from an
installed wheel, or on more than one machine. The ACC example does this. Use
an absolute path if the Manifest must not depend on `PATH`. The Manifest hash
does not identify the interpreter in either form. The side-car identifies the
interpreter, but not the modules that it imports.

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

## Measure a Run

`--diagnostics` runs the Manifest with the installed diagnostics runner,
`sil-run-diagnostics`, and writes a `sil_run_diagnostics` report (version 1,
[docs/diagnostics-report.md](diagnostics-report.md)). After the Run, `sil`
prints a summary of the report on stdout.

```sh
sil run manifest.json --diagnostics -o run.mcap   # run.mcap.diagnostics.json
sil run manifest.json -o run.mcap --diagnostics-out "results/run 1.json"
sil run manifest.json --no-recording --diagnostics  # out.mcap.diagnostics.json
```

- The report is `<recording>.diagnostics.json`, also under `--no-recording`
  (`out.mcap.diagnostics.json`). `--diagnostics-out PATH` gives another path
  and implies `--diagnostics`.
- `-o`, `--no-recording`, `--provenance`, `--provenance-mode`,
  `--participant-timeout-ms` and the three Step-protocol size guards have the
  same meaning as for `sil-run`. The
  Manifest is not moved or changed, so its relative paths resolve as in an
  ordinary Run.
- The report must be a new file in an existing directory. A path that exists,
  or that is also the Manifest, the Recording, the provenance record or the
  failure report, is rejected before any Participant starts (exit `2`).
- The Run's stdout (`manifest_hash ...`) and stderr are unchanged; the summary
  follows the stdout. It gives each value with its unit and scope, and an
  unavailable value as `unavailable (reason)`. A failed Run is marked
  `partial`. Peak resident memory is per process; no total is given.
- `--html PATH` also writes a [standalone HTML report](#html-reports) of the
  report after the Run, and implies `--diagnostics`. The JSON report is always
  kept. The HTML path is checked with the report path before the Run.
- Without a diagnostics option, `sil run` is `sil-run`, with no measurement.

The diagnostics runner is the same kernel with a measurement collector. Its
Recordings and exit codes are those of `sil-run`. The collector adds work to
each activation, so a Run that only just meets a response deadline can miss
it with diagnostics.

| Exit | Meaning |
| --- | --- |
| the Run's code | The Run failed (`1`) or its Manifest is not valid (`2`). A partial report and summary are written where possible. |
| `0` | The Run succeeded and the report was written. |
| `1` | The Run succeeded, but the report is missing or not valid, or the requested HTML report could not be written; or `sil-diagnostics-launcher` failed and no Run took place. stderr says which. |
| `2` | Invalid options, or a rejected report or HTML path. |
| `127` | `sil-run-diagnostics` or `sil-diagnostics-launcher` is not on `PATH`. |
| `128+N` | The runner ended by signal `N`, for example SIGKILL. It wrote no report, and `sil` gives no metrics. |

SIGINT, SIGTERM and SIGHUP sent to `sil` go to the runner, which stops the
Run and its Process participants as for `sil-run` (exit `1`, status
`interrupted`).

## Benchmark repeated Runs

`sil benchmark` executes a Manifest several times, each time as its own
diagnostics Run, and writes one `sil_benchmark_report` (version 1,
[docs/diagnostics-report.md](diagnostics-report.md)) to `DIR/summary.json`.

```sh
sil benchmark manifest.json --out results                 # 1 warm-up, 5 Runs
sil benchmark manifest.json --repeats 10 --warmup 0 --out results \
  --participant-timeout-ms 5000 --no-recording
```

- `--provenance-mode` is forwarded to every Run
  ([provenance mode](#provenance-mode)). The summary records it in
  `policy.provenance_mode`, because the hashing is part of the measured work.
- `--repeats N` (an integer, at least 1, default 5) measured Runs follow
  `--warmup N` (an integer, at least 0, default 1) warm-up Runs. Each Run has
  fresh processes and a fresh working directory, and the Runs are one after
  the other. A Run does not start with cold file-system caches, and external
  services are not reset.
- `--out DIR` is required and must not exist; its parent must exist. A path
  that exists is rejected before any Run starts (exit `2`) and is not changed.
- `--no-recording`, `--participant-timeout-ms` and the three Step-protocol size
  guards go to every Run. The Manifest is not moved or changed, so its
  relative paths resolve as in an ordinary Run.
- Each Run has a directory, `warmup-001/`, `run-001/`, ...: the Run report
  `run.mcap.diagnostics.json` (`out.mcap.diagnostics.json` under
  `--no-recording`), the provenance record, the Recording, the failure report
  of a failed Run, `stdout.log` and `stderr.log`. The Run's stderr is also
  copied to the stderr of `sil`.
- `summary.json` holds every attempted Run with its complete Run report, so it
  is all that a reader needs. Statistics (min, median, max, mean, population
  standard deviation) use only the successful measured Runs: warm-ups and
  failed Runs never enter them. Activation percentiles stay per Run and are
  never pooled across Runs.
- The first failed warm-up or measured Run stops the benchmark: no later Run
  starts, the earlier Runs are kept, and the summary is marked incomplete.
  SIGINT, SIGTERM or SIGHUP stops the active Run as for `sil run
  --diagnostics`, and no later Run starts.
- A summary of `summary.json` goes to stdout after the last Run. The values
  are diagnostics-runner measurements, not timings of the ordinary `sil-run`.
- `--html PATH` also writes a [standalone HTML report](#html-reports) of
  `summary.json` after it. `PATH` may be inside `DIR`, for example
  `--out results --html results/report.html`, but not `summary.json` or a Run
  directory. Elsewhere its directory must exist. It is checked before the
  first Run.

| Exit | Meaning |
| --- | --- |
| `0` | Every Run succeeded and `summary.json` (and the HTML report, if asked for) was written. |
| the first failure | The exit code `sil run --diagnostics` gives for the first failed Run: the Run's code, `1` for a missing report after a successful Run, `128+N` for a runner that ended by signal `N`. |
| `1` | The Runs succeeded, but `summary.json` or the HTML report could not be written. |
| `2` | Invalid options, `--out` exists, or a rejected HTML path. |
| `127` | `sil-run-diagnostics` or `sil-diagnostics-launcher` is not on `PATH`. |
| `128+N` | Signal `N` stopped the benchmark between Runs. |

## HTML reports

An HTML report presents one saved report, a Run report or a benchmark
summary, as a single page. It opens offline from the file, with no server,
network, external font or other file, and it holds no measurement or
statistic that is not in the JSON.

```sh
sil run manifest.json -o run.mcap --html run.html
sil benchmark manifest.json --out results --html results/report.html
sil diagnostics render results/summary.json --html copy.html
```

- `sil diagnostics render INPUT.json --html OUTPUT.html` renders a saved
  `sil_run_diagnostics` or `sil_benchmark_report` file again. It executes
  nothing and needs neither the Manifest, nor `sil-run`, nor the Run
  directories: a copied `summary.json` is enough.
- The page shows the status, identity, machine and policy, overview cards,
  lifecycle phases, Task and resource tables, sampled timing distributions,
  slow activations and, for a benchmark, every Run with per-Run charts, the
  statistics, a Run selector and a closed section that says what each
  metric means. Values have the units of the terminal
  summary; the tooltip of a value holds its exact JSON number.
- The status, cards and tables are complete without JavaScript and in print.
  The script only sorts and filters tables, selects a Run, switches the theme
  and prepares the download. "Download JSON" saves the source report byte for
  byte.
- Names, paths and messages from a report are always text, never markup or
  script. The page allows no network request.
- The HTML file must be new and its directory must exist; an existing file is
  never replaced. It is written through a temporary file, so a failure leaves
  no partial page.

| Exit of `sil diagnostics render` | Meaning |
| --- | --- |
| `0` | The HTML report was written. |
| `1` | The HTML report could not be written, also because the output exists or has no directory. |
| `2` | Invalid options; or the input is missing, not valid JSON, not a valid report, or of an unknown version. |

## What the measurements mean

[docs/diagnostics-report.md](diagnostics-report.md) is the normative
contract of each value. Read these limits before you compare numbers.

- **Inclusive timing.** An Activation (what you may call a cycle) is one
  execution of a Task. A Native activation time includes the synchronous
  routing and recording of the Messages that the Task publishes. A Process
  Step time starts when the kernel prepares the Step inputs and ends after it
  publishes the outputs. It includes encoding, IPC, waiting for the operating
  system, the Participant's own work, decoding and routing. It is not the time
  of the algorithm alone. Do not add activation times to the lifecycle phases,
  and do not subtract one measurement from another to get "pure" compute or
  kernel time: the scopes overlap.
- **Percentiles.** Up to 8,192 completed activations per Task, the median,
  p95 and p99 are exact nearest-rank values. Above that, they are estimates
  from a uniform sample of 8,192 durations: the report marks them
  `approximate` and gives the total and the retained count. p95 needs at least
  20 and p99 at least 100 completed activations; otherwise the value is
  unavailable with a reason. These minimums do not make a tail reliable. The
  maximum is the largest observed duration, not a worst-case execution time.
- **Memory.** Each peak resident memory value has a scope. The runner value
  is the runner process only, and Native participants share it. A Process
  participant value is the peak of the largest single process among the child
  and the descendants that were waited for (`largest_process_peak_rss`), not
  the memory of its whole process tree at one time. On Linux, a peak also
  includes the parent's resident memory at spawn. Peaks of different processes
  are never added: there is no total memory value.
- **CPU time.** User and system CPU time are given per scope. For
  multithreaded software, CPU time can be more than wall time. `cpu_total`
  adds only scopes that do not overlap.
- **Workload length.** The Manifest Duration sets the length of one Run.
  `--repeats` sets the number of independent Runs. There is no `--steps`
  option: with different Periods, several Native Tasks and Replay, one global
  Step count has no single meaning.
- **Throughput.** Only completed Native activations per second and completed
  Process Steps per second, over the scheduled-execution wall time. A generic
  "operations per second" has no countable definition for an arbitrary
  Participant.
- **Not measured.** Hardware counters, instructions, cycles, TOPS and FLOPS,
  algorithm-only time, memory history and leak detection, the memory of a
  process tree at one time and full activation traces. For hot functions
  and allocation sites, use [`sil profile`](#profile-a-run).
- **Cost of measuring.** Diagnostics-runner values include the collector's
  own cost. They are not a timing of `sil-run`, and no correction is
  subtracted. [docs/bench/diagnostics-overhead.md](bench/diagnostics-overhead.md)
  keeps one comparison of the two runners on a named machine.
- **No determinism verdict.** Timing does not show whether a Run is
  deterministic. Use `sil check` for that.

## Profile a Run

`sil profile` captures one profile of one fresh Run of the diagnostics
runner: CPU samples with Linux perf, or the allocations of one process with
Heaptrack. [docs/profile-report.md](profile-report.md) is the contract of the
command, its exit codes and the `sil_profile_report` (version 1) that it
writes. Use it after diagnostics show which Task or Process Step is
expensive.

```sh
sil profile manifest.json --out prof --collector cpu --activations \
  --modules-from model.fmu
sil profile manifest.json --out heap --collector heap --runner
sil profile manifest.json --out heap-fmu --collector heap --participant fmu
sil profile render prof/profile.json --html prof.html
```

- **Host.** Linux x86-64/glibc only. SiL does not ship perf or Heaptrack:
  install them on the host or in the image (see [Profiling tools](#profiling-tools)).
  Without a tool or a permission, the command stops before any Participant
  starts (exit `1`) and says what is missing. It never changes a host
  setting.
- **One Run, one collector.** No repeat, retry or fallback to another event
  or collector. `--out DIR` must not exist; its parent must exist.
- **The Run does not change.** The Manifest bytes, its hash, the working
  directory, the Participant commands, arguments and environment and the
  Clock shim stay as for `sil run`. The Recording is that of `sil run` for a
  deterministic Run with generous response deadlines. A collector adds work,
  so a Run that only just meets `--participant-timeout-ms` can miss it.
- **CPU scope** (`--collector cpu`): the runner and all its descendants.
  `--activations` relates each sample to a Native activation, a Process Step
  window, a lifecycle phase or Replay work, or says why it is unattributed.
  An FMU archive that the FMI Importer extracts into the Run directory needs
  `--modules-from ARCHIVE` for function names.
- **Heap scope** (`--collector heap`): `--runner` (all Native participants
  together) or `--participant NAME` (one unshimmed Process participant or
  FMU group). A Native participant by name and a Process participant with
  `shim: true` are rejected (exit `2`). There is no per-Step allocation
  history.
- **Limits.** `--max-capture-bytes` (1 GiB for CPU, 256 MiB for heap) and
  `--max-activation-bytes` (64 MiB) stop the capture, not the Run: the
  profile is `partial` and the exit is `1` after a successful Run.
- **Run first, capture second.** A failed Run keeps its own exit code; a
  capture problem after a successful Run gives exit `1`. `profile.json`
  states both outcomes separately.
- **Offline report.** `sil profile render` needs only `profile.json` and the
  normalized files it names (`cpu/samples.json`, with `--activations` also
  `activation/correlation.json`; heap: `heap/allocations.json`). Copy them to
  another host: it needs no perf, no Heaptrack, no profiled binary and no
  network. CPU reports include a searchable, zoomable flame graph; with
  `--activations`, a timeline and per-Task drill-downs. Selecting an interval
  filters its samples. Host and Virtual time stay exact; incomplete
  observation regions, zero-sample Activations and unlisted remainders remain
  visible. Execution context and window association stay separate.
- **Compare saved profiles.** `sil profile diff BASE/profile.json
  NEW/profile.json --html diff.html` compares the same collector: CPU self
  and inclusive shares with both denominators (per Task only with complete
  correlations on both sides), or heap site allocation metrics. It shows
  both identities and warns about differences. One Run each gives no
  significance verdict; small changes may be sampling noise. It uses the
  same portable files as `render`. Exit `0`: rendered; `1`: output write
  failed; `2`: invalid/missing evidence, mismatched collectors or output
  collision. See [Profile diff](profile-report.md#profile-diff).
- **Sensitive data.** `cpu/perf.data` holds copies of user stacks. Share it
  only as you would share a core dump. The normalized files and the HTML
  page hold no stack bytes, environment variable or Message payload.

A sample is an estimate of on-CPU time, not a function duration or a call
count. Zero samples is not evidence of zero CPU use. An allocation that is
outstanding at capture end is not a leak verdict. A profile describes this
host build, not a target ECU or a worst-case execution time.

### Profiling tools

| Tool | Qualified versions | Install | Permission |
| --- | --- | --- | --- |
| perf | 6.1.176 (Debian bookworm `linux-perf`), 6.17.13 (Ubuntu 24.04 `linux-tools-$(uname -r)`) | the package of the running kernel | `cpu-clock:u` needs `kernel.perf_event_paranoid` 2 or less for an unprivileged user, or `CAP_PERFMON`. No hardware counter is used. |
| Heaptrack | 1.4.0 (Debian bookworm), 1.5.0 (Ubuntu 24.04) | the distribution package `heaptrack` | none |

`sil profile` does not change `perf_event_paranoid`. Ubuntu sets it to 4; to
allow an unprivileged user, the host owner can set the upstream default 2
(`sudo sysctl -w kernel.perf_event_paranoid=2`). In a container, give
`CAP_PERFMON` to perf and the container instead: see
[docs/container.md](container.md#profile-in-a-container). That needs no host
change.

## Reading a Recording

A Recording stores the Schema of each Channel, so it can be decoded without
its Manifest. These read-only commands do that:

| Command | Effect |
| --- | --- |
| `sil recording channels out.mcap [--json]` | list each Channel with its Schema name, Message count and earliest and latest virtual time, and the Manifest hash |
| `sil recording show out.mcap [--channel NAME]... [--from T] [--to T] [--limit N] [--json]` | print the decoded Messages in stored order |
| `sil recording query out.mcap --channel NAME --field FIELD... [--summary] [--where FIELD OP JSON_NUMBER]` | bounded projection, numeric summaries and exact predicate search with a completion report |

The [bounded query contract](recording-queries.md) documents field/array
selection, predicate and crossing semantics, byte and scan budgets, evidence
locators, source hashing, `sil_recording_query` version 1 and exit codes
`0` complete, `2` refused, `3` incomplete, `4` complete scan with limited output.

```sh
sil recording show out.mcap --channel acc.Command --from 2000000000 --to 3000000000
```

`--from` and `--to` are virtual times in nanoseconds and select the half-open
interval [`--from`, `--to`). The MCAP index applies `--channel`, `--from` and
`--to`, so the reader holds one chunk of a long Recording in memory at a time. A Channel that the
Recording does not declare is a usage error (exit `2`), and the error lists the
Channels it declares. An unreadable Recording also exits `2`.

`sil recording channels --json` prints one `sil_recording_channels` report
(version 1): `manifest_hash` (`null` for a Recording no Run wrote, such as one
from `sil recording csv`) and a `channels` list of `channel`, `schema`,
`messages`, `first_t_ns` and `last_t_ns`. `first_t_ns` is the earliest and
`last_t_ns` the latest virtual time of the Channel's Messages, also when the
Recording does not store them in time order. Both are `null` for a Channel
without Messages.

`sil recording show --json` prints one JSON object per line and per Message
(`sil_recording_show` version 1): `channel`, `t_ns` and `fields`, the decoded
fields by name. A `u8` array, such as an OSMP or CAN-frame payload, is a
lowercase hex string. Any other array is a list. A non-finite float is the
string `"NaN"`, `"Infinity"` or `"-Infinity"`, because JSON has no such
numbers.

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
`sil check` on a shimmed manifest to prove it.

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
