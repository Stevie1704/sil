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

After the run, the kernel sends:

```json
{"op":"shutdown"}
```

The child exits without another protocol response. Its exit status is still
read: a child that exits nonzero, or dies on a signal, fails the Run with
status 1 even though every Step succeeded, because work a participant only
finishes at shutdown can fail there. A child that does not exit on its own is
sent SIGTERM and then, if it still does not exit, SIGKILL — so a participant
holding run-scoped state of its own gets the chance to release it.

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
