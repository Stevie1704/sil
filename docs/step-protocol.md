# Step protocol

This is the normative specification for the line-based contract between the
kernel and a process participant. Each line is one JSON object, and the child
must keep stdout reserved for protocol lines. The participant-facing API does
not expose the selected transport.

## Line shapes

The kernel starts a participant with:

```json
{"op":"init","name":"sink","channels":{"payload":{"schema":"big.Payload","direction":"in"}},"schemas":{"big.Payload":{"fields":[]}}}
```

`channels` contains the participant's declared inputs and outputs. Every entry
has `schema` and `direction` (`"in"` or `"out"`). A shared-memory entry also
has `transport: "shm"`, `shm_path`, and `shm_capacity`. The `schemas` object
contains the canonical schema declarations referenced by those entries.

The child acknowledges initialization:

```json
{"op":"ready"}
```

For each activation, the kernel sends:

```json
{"op":"step","t":10000000,"dt":10000000,"in":[{"ch":"payload","t":0,"data":"..."}]}
```

`t` and `dt` are virtual nanoseconds. `in` is the complete input set for the
activation, in global publish order. Each input has its channel (`ch`), its
publish time (`t`), and exactly one payload representation:

- `data` is the base64-encoded inline representation.
- `shm_seq` is the freshness sequence of the channel's shared-memory arena.

The child responds with either:

```json
{"op":"step_done","out":[{"ch":"mirror","data":"..."}]}
```

or:

```json
{"op":"fail","reason":"..."}
```

`out` preserves the participant's output order. Each output has `ch` and
exactly one of `data` or `shm_seq`; the field present on the line is
authoritative. A child failure is a run failure, not a manifest failure.

After the run, the kernel sends:

```json
{"op":"shutdown"}
```

The child exits without another protocol response.

## Shared-memory payloads

An arena is a single-slot mapping containing a fixed header (`seq`, `len`) and
then the payload bytes. Its capacity is supplied in the `init` line and comes
from the channel schema's `byte_size`.

The kernel owns the arena file. It creates and maps the file before it spawns
the child, so `shm_path` is valid from the `init` line onward, and it unlinks
the file when the run ends. A child maps the path during initialization and
must not expect it to exist after the run. A run that fails while mapping its
arenas unlinks the ones it already mapped, so a failed run leaves nothing in
the temp directory either.

The first message for an arena-backed channel in one step uses `shm_seq`. Every
later message for that channel in the same step uses inline `data`, because one
slot holds one payload. The rule resets at the next step. This applies
independently to each channel, so inline and arena-backed channels may be mixed
in one step. Receivers always inspect the field on each message instead of
inferring transport from the channel declaration.

Inputs arrive already merged in global publish order; transport handling never
reorders them.

Arena creation or mapping failure is a configuration/environment failure and
exits with status 2 before the participant starts. A stale `shm_seq`, an arena
length beyond capacity, or a payload beyond capacity is a runtime failure and
exits with status 1. The diagnostics identify the participant and, where
applicable, the channel.
