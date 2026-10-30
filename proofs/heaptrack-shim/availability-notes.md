# Package notices and eBPF availability follow-up

## License evidence

The versioned distro source archives were downloaded and their
`debian/copyright` files inspected, together with the binary packages' installed
copyright text. The relevant licenses differ by component and release:

| Artifact used by the proof | Heaptrack 1.4.0-2 | Heaptrack 1.5.0+dfsg1-2ubuntu3 |
| --- | --- | --- |
| `libheaptrack_preload.so` / tracking source | LGPL-2.1-or-later | LGPL-2.1-or-later |
| `heaptrack_interpret` main source | LGPL-2.1-or-later | GPL-2.0-or-later |

For Ubuntu 1.5, the package copyright explicitly overrides its LGPL default for
`src/interpret/heaptrack_interpret.cpp`, `src/interpret/symbolcache*` and
`src/interpret/dwarf*`, marking them GPL-2+. It lists the bundled robin-map
headers as MIT. Debian 1.4's copyright has no interpreter override; the
interpreter's own header confirms LGPL-2.1-or-later. These statements concern
the named components, not a single license for all Heaptrack files or all
of their dynamically linked dependencies.

Primary package sources, named by exact version:

- Ubuntu [1.5 source descriptor](https://archive.ubuntu.com/ubuntu/pool/universe/h/heaptrack/heaptrack_1.5.0+dfsg1-2ubuntu3.dsc)
  and [Debian packaging archive](https://archive.ubuntu.com/ubuntu/pool/universe/h/heaptrack/heaptrack_1.5.0+dfsg1-2ubuntu3.debian.tar.xz).
  The archive's SHA-256, also present in the descriptor, is
  `1b5b5ad05a099ff07d3f3084743768a84becb9f1c40670d9cb5eef668ad12f0c`.
- Debian [1.4 source descriptor](https://deb.debian.org/debian/pool/main/h/heaptrack/heaptrack_1.4.0-2.dsc)
  and [packaging archive](https://deb.debian.org/debian/pool/main/h/heaptrack/heaptrack_1.4.0-2.debian.tar.xz).
  Archive SHA-256:
  `cdef19a6cb1108ca06da014cc9e296c00f7441ede887d6c98de956970e7357bc`.
- Installed copyright was independently read from
  [`heaptrack_1.5.0+dfsg1-2ubuntu3_amd64.deb`](https://archive.ubuntu.com/ubuntu/pool/universe/h/heaptrack/heaptrack_1.5.0+dfsg1-2ubuntu3_amd64.deb),
  SHA-256 `25e09c15e204c2578b81a1e82d138a79f046ad26c14bb54b5b385b28758c7456`,
  and [`heaptrack_1.4.0-2_amd64.deb`](https://deb.debian.org/debian/pool/main/h/heaptrack/heaptrack_1.4.0-2_amd64.deb),
  SHA-256 `2ff6dbf3b992ef31bf8213a0efd3a9583ea9540212648eecf599c49b975da212`.

Immutable upstream headers agree with that version distinction:
[1.5 preload](https://github.com/KDE/heaptrack/blob/b54e92e88b0895f910a5b107f499d1155a086b80/src/track/heaptrack_preload.cpp#L1-L5),
[1.5 interpreter](https://github.com/KDE/heaptrack/blob/b54e92e88b0895f910a5b107f499d1155a086b80/src/interpret/heaptrack_interpret.cpp#L1-L5),
[1.4 interpreter](https://github.com/KDE/heaptrack/blob/9e5514ecbbf56c8d5307717e4d1c70cc58dc27fe/src/interpret/heaptrack_interpret.cpp#L1-L5).

The new bpftrace probe uses Ubuntu Noble's
**`bpftrace=0.20.2-1ubuntu4.3`**, available in
[noble-updates](https://packages.ubuntu.com/noble-updates/bpftrace).
Its exact [packaging archive](https://archive.ubuntu.com/ubuntu/pool/main/b/bpftrace/bpftrace_0.20.2-1ubuntu4.3.debian.tar.xz)
has SHA-256 `c67ae68be0e5d93159699395979e8bfe53f7eb7cc307627dc8c0b54d445e8a26`.
The archive's `debian/copyright` assigns Apache-2.0 to `Files: *` and tools,
Expat/MIT to `resources/*.h`, and BSD-3-clause to `cmake/FindLibElf.cmake`.
The probe uses the bpftrace executable and its libraries; it does not copy a
bundled example `.bt` script. Dependency notices remain distinct from these
source-file assignments. Retain installed `/usr/share/doc/{heaptrack,libheaptrack,bpftrace}/copyright`
alongside the environment package inventory in CI.

## What the runtime probe establishes

[`availability.py`](availability.py) exposes `probe(out: Path) -> dict` and a
standalone CLI:

```sh
python3 proofs/heaptrack-shim/availability.py build/heaptrack-shim-bpf-native
```

Install `bpftrace=0.20.2-1ubuntu4.3`, a C compiler, `libc6-dev`, and Python 3.
`coreutils` supplies `timeout` for an optional sudo-root capture. The probe
does not install packages. Use a new output directory. Each command and its
full stdout/stderr is retained, together with package pin comparison, kernel,
UID, capability masks, seccomp mode, relevant readable policy settings,
interface presence, generated fixture/program and file digests.

The probe first attempts tracing as its current user. If that fails and
existing `sudo -n true` succeeds, it tries the same fixture via sudo-root.
This is a separately recorded privilege context; its success cannot qualify
unprivileged profiling. Running the CLI as root inside an ordinary container
records root **with that container's existing capability/seccomp limits**.
No sysctl, mount, container capability or seccomp policy is changed.

The fixture makes 50,000 malloc/free calls of 2048 bytes inside a named,
non-inlined function. A function-entry marker limits the libc `malloc` uprobe
to that loop, and `ustack(12)` records user stacks in an aggregate map.
The fixture is compiled with frame pointers, debug symbols and exported
symbols. A successful smoke capture requires the exact entry count, a
resolved `proof_malloc_site` frame and successful fixture completion. It is
not a test of arbitrary stripped/optimized participant stacks, unsuccessful
allocation accounting, reallocations or outstanding allocations.

The BPF program explicitly uses `nsecs(monotonic)`. In pinned bpftrace 0.20.2,
[IR generation](https://github.com/bpftrace/bpftrace/blob/b0f7f14abfa83ec08d68e0558391f7529d9d84a5/src/ast/irbuilderbpf.cpp#L1070-L1085)
selects `BPF_FUNC_ktime_get_ns`, so the intended source is host monotonic time,
excluding suspend. A failed capture has no observed timestamps. This is
separate from shimmed userspace `clock_gettime` behavior.

If capture succeeds, the report includes the fixture's monotonic loop duration
with and without tracing, with the fixture UID recorded for each. A ratio is
reported only when those UIDs match; a tracer that changes the command
privilege context does not supply a comparable overhead ratio.
These are one run per mode, with no stability, percentile or cross-workload
claim. Tracer startup/attachment time is recorded separately as command wall
time and is excluded from the loop ratio. If loading or attachment fails,
profiled duration, counts and timestamps remain absent; no overhead or
stack-quality result is manufactured from the failed attempt. Standard output
bytes are recorded for the aggregate program, not predicted full-event-stream
growth.

## Local setup check

A local default-capability Ubuntu 24.04 container successfully installed the
exact package and compiled/ran the fixture. It reported Linux x86-64 kernel
`6.8.0-117-generic`, UID 0, capability mask `00000000a80425fb`, seccomp mode 2,
`perf_event_paranoid=4` and `unprivileged_bpf_disabled=2`. The capture exited 1:
map creation returned `Function not implemented`, with an additional memlock
diagnostic. Thus this setup could not collect the fixture. The error alone
does not identify whether kernel exposure, seccomp, capabilities or another
constraint caused it. No host policy was changed. Native CI and its separate
default-capability container reports are archived at the links below;
this local smoke check is not substituted for them.

## Candidate disposition

Even if the privileged smoke capture succeeds, eBPF remains **rejected as a
drop-in replacement for the existing heap collector** and **deferred as a new
collector**. This aggregate malloc-entry probe has no complete allocation
history, ownership/lifetime accounting, descendant management, startup capture
guarantee for general participants, general allocator coverage, loss budget or
Recording-equality result. Those are requirements for a new integration,
rather than capabilities established by this availability experiment.

The pinned [bpftrace manual](https://github.com/bpftrace/bpftrace/blob/b0f7f14abfa83ec08d68e0558391f7529d9d84a5/man/adoc/bpftrace.adoc)
documents privilege inheritance of `-c`, PID-scoped uprobes, bounded maps,
frame-pointer requirements in its user-stack example and symbol-cache/ASLR
limitations. Its explicit stopped-child startup explanation concerns USDT;
this fixture's observed marker/count must not be generalized into a startup
coverage guarantee. The existing
[BCC source analysis](candidates.md#out-of-process-allocation-tracing) additionally
explains why `memleak` outstanding-allocation output is not the existing
Heaptrack event history. The support conclusion stays **defer**, with the
shimmed-Process rejection retained.

## Native and container observations

The [extended x86-64 workflow](https://github.com/Stevie1704/sil/actions/runs/40291189216)
retains [native results](https://github.com/Stevie1704/sil/blob/d34226cb347d6568c8b7dde33ccb23227e960231/proofs/heaptrack-shim/evidence-review/heaptrack-shim-bpf-native/availability.json)
and [default-container results](https://github.com/Stevie1704/sil/blob/d34226cb347d6568c8b7dde33ccb23227e960231/proofs/heaptrack-shim/evidence-review/heaptrack-shim-bpf-container/availability.json).
The ordinary UID 1001 invocation was rejected by this bpftrace version's root
requirement. With existing sudo-root access it counted all 50,000 fixture calls
and recorded monotonic values, but user-stack output contained raw addresses
without a resolved `proof_malloc_site`; the capture remains incomplete.
Both fixture UIDs were zero. Recorded loop durations of 1,821,610 ns and
182,243,410 ns give a separately derived 100.05-times duration for that pair.
The source report withheld its qualified-capture ratio because symbolization
failed; current code treats comparable loop timing separately from symbol
resolution. Neither form is a benchmark or a production overhead estimate.

The default-capability root container had effective capability mask
`00000000a80425fb`, seccomp mode 2, and failed BPF map creation with
`Operation not permitted`. It supplies no observed allocation count, usable
stack or profiled loop duration. Nothing in the experiment changed sysctls,
mounts, container capabilities or seccomp. These setup-specific observations
support the explicit rejection as a drop-in collector; they do not claim that
all eBPF approaches or privileged deployments are impossible.
