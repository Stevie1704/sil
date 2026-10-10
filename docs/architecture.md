# Architecture and repository map

[Documentation index](README.md) · [Repository overview](../README.md)

SiL separates the deterministic execution machinery from the behavior being
simulated. The kernel schedules Participants in Virtual time, routes typed
Messages, and records their publication order. FMI coordination, vehicle
behavior, and CAN semantics live at the edge.

## Runtime interfaces

| Module | Responsibility and interface |
| --- | --- |
| `kernel/src/` | Loads and prepares a Manifest, schedules Tasks and Steps, routes Messages, and writes Recordings. `sil-run` is the execution entry point. |
| `include/sil/` | C ABI for Native participants and binary layouts shared with Process participants and the Clock shim. |
| `shim/` | Preloaded Clock shim that reads the kernel-provided Virtual time. Its test probe lives in `tests/fixtures/`. |
| `python/src/sil/` | Manifest authoring, schema codecs, Process participant support, test helpers, recording conversion, comparison, and regression tooling. |
| `python/src/sil/fmi/` | Importer: archive inspection, variable binding, FMI lifecycle, and coordination of FMU groups within one Process participant. |
| `models/` | Maintained model products. `models/can/` owns CAN behavior, its FMI adapter, supported profile, tests, qualification, and release policy. |

The Native participant C ABI and the Process participant
[Step protocol](step-protocol.md) are the seams between the kernel and external
behavior. The kernel does not interpret FMI or CAN. See the
[FMU group decision](adr/0001-connected-fmus-in-one-process-participant.md) and
[CAN model decision](adr/0003-maintained-can-model-at-the-edge.md) for why these
responsibilities are placed there.

The recorder receives Messages in global Publish order. Message layout is
packed little-endian; cross-platform bit-exactness is not claimed. The precise
support and determinism scope lives in [SUPPORT.md](../SUPPORT.md).

## Examples, tests, and evidence

- `examples/` teaches adopter workflows with runnable inputs and adapters.
  The ACC example lives in `python/src/sil/examples/acc/` because it also ships
  in the Python distribution as `python -m sil.examples.acc.manifest`.
- `tests/` verifies behavior through Run and installation interfaces, with
  focused C++ tests for internal invariants. `tests/fixtures/` owns native toy
  and benchmark Participants, fixture schemas, the Clock probe, FMU fixtures,
  and other test inputs. `tests/participants/` holds Python test Participants.
- `proofs/` holds acceptance experiments against pinned external artifacts,
  with independent references and retained evidence. These are distinct from
  maintained model products under `models/`.
- `tools/bench_*.py` drives the routing experiments; their procedures and
  results live in `docs/bench/`. Native benchmark Participants and schemas are
  shared test fixtures under `tests/fixtures/`.

Keep acceptance evidence with the experiment or model it qualifies. A new
model's domain semantics belong with the model; an example's adapter belongs
with that example.

## Build and development ownership

The root `CMakeLists.txt` owns project configuration, dependencies, and
composition. Target definitions live in `kernel/CMakeLists.txt`,
`shim/CMakeLists.txt`, `tests/CMakeLists.txt`, and `examples/CMakeLists.txt`.
Runtime installation rules live with their targets; shared headers, tools,
metadata, and notices are installed by `cmake/install.cmake`.

All native artifacts retain their names and locations directly under the
chosen build directory. Defining a target in a subdirectory does not change
paths consumed by Manifests or tests. The runtime Docker build omits examples;
the root build includes their targets only when their build definition exists.

`python/pyproject.toml` owns the Python distribution and the shared product
version. The root `Makefile` provides developer entry points across the native
and Python builds. `container/`, the root `Dockerfile`, and `.github/` own
runtime packaging and CI. `tools/` contains schema generation, release,
benchmark, and determinism tools.

`worker/` contains the development-agent image and its bundled skills;
`factory.yaml` configures that automation's setup and gates. Neither is a
simulation Participant. Local `build/`, `.venv/`, and cache directories are
generated working state.

## Schema ownership

Primitive schema metadata lives in `python/src/sil/_schema_types.py`. The
Manifest builder, Python codec, and `tools/silschema.py` header generator share
its formats, widths, and integer bounds. CMake installs the same stdlib-only
file beside `silschema` as `_sil_schema_types.py`; keep both installed files
together. No generated metadata or regeneration step is needed.

The kernel independently validates hand-written Manifests.
`tests/fixtures/schema_conformance.json` states expected bytes and offsets;
`tests/test_schema_conformance.py` exercises generated structs, kernel loading,
and Recording through both checkout and installed interfaces. Numeric tests
also pin builder and loader conversion policies, including finite f32 limits.

See the [documentation index](README.md#architecture-and-contracts) for the
relationship between current contracts, historical design notes, and ADRs.
