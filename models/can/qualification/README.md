# External node provenance and predeclared exchange

The external model is Modelica Association's `can-node-triggered-output` at
`de019a6efbad810795835f2fd9bbf9e62eb451b9` (the upstream 1.0.0 work, not the
beta main revision). Its Binary MIME types already declare 1.0.0, but its layered-standard
manifest still declares beta.1. `manifest.patch` explicitly corrects that stale
version, the description required by the released schema, and the schema URL.
The terminal definitions and model description are retained. We compile against the actual
released 1.0.0 headers at `8abdf039bfb994c794e4c15bce575cfc00a1ab6e`, replacing
the packaging script's old header pin. `build_nodes.py` records every source
hash and the exact adaptation digest in each FMU.

`node.patch` makes two explicit changes to upstream `App.c`:

1. Separate “Clock activation has been reported” from “Clock is active in
   this event”. Upstream cleared TxClock in GetClock, then rejected GetBinary
   because TxClock was no longer active, and skipped clearing its Tx buffer
   in UpdateDiscreteStates. The latch permits exactly one active Clock read,
   keeps Binary available, and clears the buffer/latch in the discrete update.
2. Compile a receive-only peer with `SIL_CAN_RECEIVE_ONLY`, disabling only the
   periodic transmit loop. Its configuration and received-operation processing
   remain upstream code. This avoids manufacturing a competing request in a
   profile that deliberately does not model contention yet.

The manifest correction is not compatibility evidence by itself: the released
headers, schema checks, Clock/Binary lifecycle assertions and actual exchanges
through both import paths establish this restricted profile.

The sender's frame construction is unchanged: ID 1, IDE=RTR=0, payload
`01 02 03 04`. The receive-only artifact is a named, upstream-derived test
variant, not claimed as an unmodified external product. The shared-library
build defines `FMU_IDENTIFIER_H` to export standard unprefixed FMI symbols.
FMI headers come from FMPy 0.3.32; node/LS-BUS sources use BSD-2-Clause.

Before execution the expected exchange is (the 83-bit frame at 100 kbit/s ends 830 us after its request; see ../README.md):

| Event time | Expected behavior |
| --- | --- |
| 0 ns | Both nodes emit CAN bitrate 100000 and BufferAndRetransmit configuration; bus consumes these without forwarding. |
| 300000000 ns | External sender emits `1000000014000000010000000000040001020304`. Receiver emits no frame. |
| 300830000 ns | Bus Node1 emits only Confirm `200000000c00000001000000`; Node2 emits the unchanged sender operation. Receiver's upstream handler logs receipt of ID 1, length 4. |
| Through 310000000 ns | No additional frame or confirmation. |

Independent FMPy execution checks initialization, repeated Clock reads, Binary
access, discrete updates, countdown intervals, frame receipt, and absence of
buffer leakage on later events. SiL loads those same three archives using its
existing FMU group and checks the Recording's exact payloads and FMI event
times; repeat Runs must have identical Recording bytes. No Importer correction
is involved. Tests additionally exercise malformed traffic, unsupported profile
features, sequential transfers in both directions, boundary payload sizes,
per-instance isolation, and FMI lifecycle failures.
