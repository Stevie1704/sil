# 7. Source-owned Native scopes with a separate observer and contract

Date: 2026-11-04

Status: accepted (contract); implementation pending

Relates to [issue #360](https://github.com/Stevie1704/sil/issues/360),
[issue #320](https://github.com/Stevie1704/sil/issues/320) and
[issue #354](https://github.com/Stevie1704/sil/issues/354). Builds on
[ADR 0004](0004-opt-in-run-diagnostics-runner.md).

## Context

The [#320 proof](../../proofs/function-tracing/README.md) counted the logical
calls of four selected C functions in each Native Task Activation of the ADAS
reference, also when GCC inlined them. Sampled stacks cannot give exact call
counts or order. The proof used copied sources, a proof-owned observer linked
into the library and a proof-only file format. LLVM XRay could not register
the loaded Native library.

A supported version must decide who owns the instrumentation, how a scope
map stays bound to the build that ran, and what it can change. Ordinary
`sil-run` must stay free of observation hooks (ADR 0004), and no existing
contract may change its meaning.

## Decision

**Source owners mark scopes in their own source with an optional SiL header.
A separate observer library records them only in an explicit
`sil profile --collector scopes` capture. The evidence is a new, separately
versioned set of files.** The [contract](../proposals/native-source-scopes.md)
defines the API, lifecycle, formats, join and budgets.

Ownership:

| Item | Owner |
| --- | --- |
| Placement, IDs and names of scopes; the instrumented and original builds | Source owner |
| `include/sil/source_scope.h`, `libsil_source_scope.so.1`, the collector, decoder and renderer | SiL |
| The scope map | The instrumented library's bytes (ELF section `sil_source_scopes`); no separate file to maintain |

SiL tooling never inserts scopes into user code, by regular expression or
otherwise.

Compatibility:

- **Unchanged:** `sil_api_v1` and the Native participant ABI, the Manifest
  and its hash, the Step protocol, Recording bytes, scheduling, exit codes of
  existing commands, `sil_run_diagnostics` v1, `sil_activation_observations`
  1 and `sil_profile_report` v1. The scopes collector writes `scopes.json`,
  not `profile.json`.
- **New and versioned:** the header's record layout (version 1), the
  observer ABI (soname `.so.1`, symbols `sil_source_scope_v1_*`), raw
  `events.bin` version 1 and the JSON contracts `sil_source_scope_raw`,
  `sil_source_scope_map`, `sil_source_scope_calls` and
  `sil_source_scope_report`, each version 1. An incompatible change makes a
  new version; a reader rejects a version it does not know.
- **Additive CLI:** `--collector scopes` and its options, and `sil profile
  render` of `scopes.json`. Existing invocations keep their behavior.
- **Ordinary runs:** `sil-run` has no symbol, branch or include of the
  observer. Without `SIL_SOURCE_SCOPES=1` the header produces nothing. An
  instrumented library outside a scopes capture is dormant and writes
  nothing.

## Consequences

- An installed SiL carries one more header and one more shared library.
  A source owner who uses them builds and keeps an instrumented variant.
- Durations describe the instrumented build. Its inlining and timing can
  differ from the original build, and observer cost is substantial in short
  Activations (see the contract's #320 evidence). No overhead is
  subtracted; both build identities are kept.
- Instrumentation can move a Run that sits on a response-deadline edge.
  Recording equality is qualified with generous Response deadlines only.
- Process participants, FMUs, prebuilt libraries, worker threads,
  nonlocal exits and compilers other than GCC 12 stay outside the contract
  until separately qualified.
- No test gates on a timing threshold. Tests pin identities, counts, order,
  reasons and bounds.
