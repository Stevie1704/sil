# Step protocol

This is the normative specification for the line-based contract between the
kernel and a process participant. Each line is one JSON object, and the child
must keep stdout reserved for protocol lines. The participant-facing API does
not expose the selected transport.

## Line shapes

The kernel starts a participant with:

```json
{"op":"init","name":"sink","protocol":2,"channels":{"payload":{"schema":"big.Payload","direction":"in","transport":"shm","shm_path":"/tmp/sil_arena_x","shm_capacity":1048576,"shm_slots":2}},"schemas":{"big.Payload":{"fields":[]}}}
```

Before starting the process participants, the kernel creates the Run working
directory beneath the runner's invocation directory. Each child starts in its
own directory beneath that root. The kernel removes the complete tree after
reaping the children, whether the Run passes or fails. Generated directory
names are runtime state: they are absent from the Manifest, its hash, and the
init line.

Changing directory must not strand commands written for the earlier launch
contract. Before the fork, the kernel resolves a relative `command[0]` that
contains a directory component, and every argument that names an existing
relative filesystem entry, against the runner's invocation directory. A bare
executable name still uses `PATH`. Other relative arguments stay unchanged and
are therefore interpreted by the participant from its private directory; this
is where a relative output path naturally creates Run-scoped state.

That resolution covers the command line, which the kernel builds. It does not
cover the environment, which the child inherits unchanged apart from the shim
and clock-region variables the kernel sets itself — and those the kernel
resolves in the parent, so the values it passes are already absolute. A
relative entry the caller put in `PYTHONPATH`, `LD_LIBRARY_PATH`, or any other
variable is therefore resolved from the participant's own directory rather than
from the runner's invocation directory. Make such entries absolute. The kernel
does not rewrite them: it would have to know which variables of which language
name a search path, and it knows neither.

`channels` contains the participant's declared inputs and outputs. Every entry
has `schema` and `direction` (`"in"` or `"out"`). A shared-memory entry also
has `transport: "shm"`, `shm_path`, `shm_capacity` (bytes per slot), and
`shm_slots`. The `schemas` object contains the canonical schema declarations
referenced by those entries.

`protocol` is 2 when any shared-memory Channel in this participant's contract
declares more than one slot; otherwise it is 1. Protocol 2 adds indexed Arena
slots.

The child acknowledges initialization:

```json
{"op":"ready","protocol":2}
```

Or it rejects initialization:

```json
{"op":"fail","reason":"..."}
```

If the participant's own initialization work fails after it has accepted the
contract, it may mark that failure as a Run failure:

```json
{"op":"fail","failure":"run","reason":"..."}
```

A child that cannot honour the contract the `init` line describes answers
`fail` instead of `ready` and exits. Without `failure: "run"`, the kernel
treats that as a Manifest error and exits with status 2 before any
participant is stepped, because a Manifest that names a participant it cannot
initialize is wrong, rather than a Run that went wrong. A participant that
accepts the contract but then reports its own initialization failure with
`failure: "run"` exits with status 1. This lets an imported FMU preserve an
FMI diagnostic instead of turning it into a generic child-exited message.

Only that deliberate line is a Manifest error. Any other answer to
`init`, and a child that dies while initializing without answering at all,
stays a run failure — a participant that breaks on the way up is not a bad
Manifest.

The run boundary may be given `--participant-timeout-ms N`. When present, the
kernel gives each Process participant an independent `N`-millisecond,
wall-clock deadline for the complete wait for this `ready` response. The
deadline starts with the `init` request and is not extended by receiving only a
partial response line. A missed initialization deadline is a Run failure (exit
1) naming the participant and initialization phase. Without the option, the
wait remains unlimited for compatibility.

The child echoes the highest offered protocol it supports. An absent
`ready.protocol` means 1. If a child answers 1 (or omits the field) to an offer
of 2, the kernel uses only slot zero and applies the established inline fallback
after the first Message. Because the Transport is an optimization, this changes
neither participant-visible payloads nor Run semantics.

For each activation, the kernel sends:

```json
{"op":"step","t":10000000,"dt":10000000,"in":[{"ch":"payload","t":0,"data":"..."}]}
```

`t` and `dt` are virtual nanoseconds. `in` is the complete input set for the
activation, in global publish order. Each input has its channel (`ch`), its
publish time (`t`), and exactly one payload representation:

- `data` is the base64-encoded inline representation.
- `shm_slot` and `shm_seq` name an Arena slot and its freshness sequence.
  Protocol-1 lines may omit `shm_slot`, which means slot zero.

The child responds with either:

```json
{"op":"step_done","out":[{"ch":"mirror","data":"..."}]}
```

or:

```json
{"op":"fail","reason":"..."}
```

`out` preserves the participant's output order. Each output has `ch` and
exactly one of `data` or `shm_seq`; `shm_slot` accompanies `shm_seq` under
protocol 2. The representation present on the line is authoritative. A child
failure at Step time is a run failure, not a Manifest error; only a `fail`
answering `init` is a Manifest error.

The same `--participant-timeout-ms N` deadline starts anew with every `step`
request and covers the complete wait for its `step_done` response. It is a
wall-clock guard: partial line reads do not reset it, and it is independent of
the Step's virtual time. A missed deadline is a Run failure (exit 1) whose
diagnostic names the Process participant, the Step phase, and that Step's
virtual time. The option is a run-boundary argument, not Manifest data, so it
does not change the Manifest hash, protocol messages, or Recording bytes of a
Run that completes before all deadlines.

The run boundary also supplies three resource guards for every Process
participant. Their defaults are deliberately above the payloads used by the
examples and routing benchmark:

| argument | default | measured value |
| --- | ---: | --- |
| `--max-protocol-line-bytes N` | `16 MiB` (`16777216`) | response-line bytes, excluding the terminating newline |
| `--max-step-output-messages N` | `1024` | entries in one `step_done.out` array |
| `--max-step-inline-payload-bytes N` | `64 MiB` (`67108864`) | decoded bytes in that Step's inline `data` fields |

Each `N` must be a positive integer representable as the runner's `size_t`.
The line limit is enforced while reading, before the kernel appends bytes to
the response buffer, and applies to both `ready` and every `step_done`
response. The output-Message limit is checked before any output is decoded.
The inline-payload limit is checked against the decoded bytes before any
output reaches the Engine. A Message represented by `shm_seq` (and its Arena
descriptor) is not charged to the inline-payload limit; an inline fallback on
an Arena-backed Channel is charged.

The guards are configured separately but they are not independent of each
other. Every inline byte of a Step arrives base64-encoded inside that Step's
one response line, so the inline payload of a Step can never exceed three
quarters of the line limit: at the default line limit the inline budget is
capped at 12 MiB whatever `--max-step-inline-payload-bytes` says. The
inline-payload guard therefore does nothing at the shipped defaults. It exists
for the deployment that raises `--max-protocol-line-bytes`, where it bounds
decoded payload independently of how much encoded text the line may carry.

Exceeding one of these limits is a Run failure (exit 1), never a Manifest
error or a successful determinism check. The diagnostic names the Process
participant, the limit, its configured value, and the observed value; a Step
diagnostic also names the Step's virtual time. For a line-length failure the
observed value is how many bytes the kernel had buffered for that line when
the guard tripped, not the length of the line the participant meant to send:
the kernel stops reading at the limit and never learns the rest. It is a
lower bound, and it varies with how the child's output is chunked into pipe
reads. The child is terminated and reaped through the normal
SIGTERM-to-SIGKILL path, and the existing Mapped region, Run working
directory, and partial-Recording lifetime behavior remains in force.

### What the guards bound, and what they do not

The line limit bounds the read buffer exactly: the kernel inspects each chunk
before appending it, so the buffer never grows past the limit while a child
withholds the newline. It does not bound the parsed form of a line that stays
inside it. A line is parsed into a JSON document before the output-Message
count can be checked, and that document costs a multiple of the line's bytes.
The worst case is a line packed with the smallest legal `out` entries. The
kernel process alone, measured through `sil-run-instrumented`, peaks at 12.8
to 16.4 times the line limit across limits from 1 MiB to 16 MiB — 205 MiB at
the 16 MiB default — before the count guard trips. Peak memory is therefore
linear in `--max-protocol-line-bytes`, and that argument is the one to lower
when a Run must hold a tighter memory ceiling. A line carrying large payloads
rather than many small entries costs far less, because the cost is per JSON
node, not per byte. Those figures are observational, in the sense
`kernel/src/copy_counters.hpp` gives the word: they are measurements of one
machine, not thresholds the test suite asserts.

These are operational Run-boundary arguments. They are not Manifest fields,
do not affect virtual time or the Manifest hash, and cannot change the
Recording of a Run that stays within them. Omitting an argument selects its
documented default.

After the run, the kernel sends:

```json
{"op":"shutdown"}
```

The child exits without another protocol response. Its exit status is still
read: a child that exits nonzero, or dies on a signal, fails the Run with
status 1 even though every Step succeeded, because work a participant only
finishes at shutdown can fail there. A child that does not exit on its own is
sent SIGTERM and then, if it still does not exit, SIGKILL. The child becomes
the leader of a private process group before `exec`, so both signals address
the Process participant and its descendants. The kernel waits for that group
to drain during the bounded TERM/KILL grace periods even if the direct child
exits first, then releases the Run working directory, Arenas, and protocol
resources after reaping the direct child. The direct child's exit status
remains the one interpreted by the Run; descendants do not change it.

This process group is a lifetime boundary for cooperative descendants, not a
sandbox. It supplies no CPU or memory quota, syscall filter, namespace, or
protection against a descendant that deliberately escapes its group. Stronger
isolation belongs to the container Run described in issue #115.

The runner handles SIGINT, SIGHUP, and SIGTERM as Run interruptions. It stops
waiting at the next safe protocol or scheduler point, reports a Run failure,
and performs this same group teardown before releasing Run resources.

## Shared-memory payloads

An Arena contains `shm_slots` fixed-layout slots. Each slot repeats the original
single-slot layout: a header (`seq`, `len`) followed by `shm_capacity` payload
bytes. Payload storage is therefore the schema `byte_size * slots`, plus one
fixed header per slot. Keeping slot zero byte-compatible lets protocol-1
participants map only the original prefix of a multi-slot Arena.

The kernel owns the arena file. It creates and maps the file before it spawns
the child, so `shm_path` is valid from the `init` line onward, and it unlinks
the file when the run ends. A child maps the path during initialization and
must not expect it to exist after the run. A run that fails while mapping its
arenas unlinks the ones it already mapped, so a failed run leaves nothing in
the temp directory either.

Messages for one Arena-backed Channel fill slots from zero in Publish order.
Every Message after the declared slot count uses inline `data`. Slot allocation
resets only across a Step boundary: the kernel writes inputs, sends the Step
line, and waits for the response, so all input slots are free after the child
responds; likewise the child writes output slots before that response and the
kernel consumes them before the next Step. This applies independently to each
Channel, so inline and Arena-backed Channels may be mixed in one Step. Receivers
always inspect each Message's representation instead of inferring it from the
Channel declaration.

`seq` increases on every Arena write, across all slots. The receiver compares
the Message's `shm_seq` to the header in its named slot, distinguishing a slot
written for this Message from stale contents left by an earlier Step.

Inputs arrive already merged in global publish order; transport handling never
reorders them.

Arena creation or mapping failure is a Manifest/environment error and
exits with status 2 before the participant starts. A stale `shm_seq`, an arena
length beyond capacity, or a payload beyond capacity is a runtime failure and
exits with status 1. The diagnostics identify the participant and, where
applicable, the channel.
