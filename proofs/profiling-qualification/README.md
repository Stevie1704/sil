# Profiling qualification of the installed workflows (#319)

This proof closes Phase 1 of the profiling spec
[#313](https://github.com/Stevie1704/sil/issues/313). It qualifies the shipped
`sil profile` command (#315 to #318) as a user runs it: installed from
release artifacts on native Linux, and in the documented profiling image
([docs/container.md](../../docs/container.md#profile-in-a-container)).
The feasibility proof of the backends is [#314](../profiling/README.md).

## Outcome

| Environment | perf (CPU) | Heaptrack (heap) | Activation join | Offline render |
| --- | --- | --- | --- | --- |
| native, release install, `perf_event_paranoid` 4 (Ubuntu default) | denied in preflight, exit 1, actionable | **qualified**, no privilege | not applicable | **qualified** |
| native, release install, `perf_event_paranoid` 2 (disclosed host change) | **qualified** | (qualified at 4) | **qualified** | **qualified** |
| documented image, default capabilities | denied in preflight, exit 1, actionable | not run here: needs no capability | not applicable | not applicable |
| documented image, `--cap-add PERFMON`, no host change | **qualified** | **qualified** | **qualified** | **qualified** (plain runtime image) |

Qualified means: every check of the environment's scenarios passed in the
retained run. A check never uses a time, a sample share, an RSS value or an
overhead. The retained run is listed in [Evidence](#evidence).

## What runs where

[`proof-profiling-qualification`](../../.github/workflows/proof-profiling-qualification.yml)
runs on a GitHub-hosted `ubuntu-24.04` runner (x86-64, glibc 2.39, no
hardware PMU in the VM: `cpu-clock:u` is a software event). The retained
run had kernel `7.0.0-1017-azure`; the first run (39341633342) had
`6.17.0-1022-azure`, and its CPU, heap and render scenarios passed with
perf 6.17.13 too. One job, in this order:

| Step | What |
| --- | --- |
| tools | apt: `linux-tools-$(uname -r)` (perf 7.0.14 in the retained run), `heaptrack=1.5.0+dfsg1-2ubuntu3`, and a compiler for the workload only |
| native install | [`install-native.sh`](install-native.sh): the wheel and the native archive built as `release.yml` builds them, a local mirror of the release layout, and the release's `install.sh`. The Python package is imported from the installed venv, never from the checkout (`facts.sil_package`) |
| images | [`container.sh images`](container.sh): the runtime image with the command of docs/container.md, the profiling image from [`profile.Dockerfile`](profile.Dockerfile) (the documented Dockerfile: `linux-perf=6.1.176-1`, `heaptrack=1.4.0-2`, `setcap cap_perfmon+ep` on perf, non-root user 10001), and a bookworm workload builder |
| workloads | [`qualify.py build`](qualify.py): host compiler for native, the builder image for the container |
| native, paranoid 4 | groups preflight and heap |
| container, default capabilities | group preflight |
| container, `--cap-add PERFMON` | groups preflight, cpu, heap, storage (a 16 MiB `tmpfs` that allows exec) |
| disclosed host changes | `kernel.perf_event_paranoid=2`; a 16 MiB `tmpfs` at `/mnt/sil-small` |
| native, paranoid 2 | groups cpu and storage |
| offline render | every copied portable set, in the plain runtime image (`--network none`, no perf, no Heaptrack) and natively with `PATH` = the installed SiL only |

The container runs as the host user with `--network none`, as
docs/container.md documents it. Each scenario is one command in a new
directory that holds an unrelated sentinel file; `TMPDIR` is a directory of
its own.

## Workload

[`fixtures.py`](fixtures.py) builds with one compiler and `-O2 -g`, no frame
pointers:

| Manifest | Participants | Covers |
| --- | --- | --- |
| `main` | the #314 workload: Native (stripped library, separate debug file), shimmed Python Process participant that loads a library with ctypes and starts 4 short-lived children, FMU extracted into the Run directory; 10 ms | Native, Process, Clock shim, dynamically loaded code, short-lived children, debug-file variants |
| `mixed` | Replay of `fmu.In` (from a Recording of `main`) into the FMU (10 ms), an FMU group of two CAN nodes and a bus (`--instance`, `--connect`) with an observer (20 ms), [`probe_participant.py`](probe_participant.py) (30 ms) | Replay, mixed Periods, FMU group, the probe of arguments, working directory and environment |
| `failure` | `mixed`, the probe fails at Step 7 | deterministic Run failure |
| `deadline` | `mixed`, the probe sleeps 3 s at Step 3, deadline 1 s | response deadline |
| `long` | `mixed` over 3 s, the probe sleeps 40 ms per Step | interruption, collector failure |

The probe publishes digests of its `argv`, of its environment (with the
Run-directory path replaced) and whether it runs in a Run working directory
inside the command's directory. So a Recording that equals the ordinary one
also proves that the launch kept path, argument and environment semantics.

## Scenarios and checks

`qualify.py run` writes `results.json`: the environment identity (`facts`),
and per scenario the command, exit code, wall time, stdout/stderr tails,
checks, observations (artifact sizes, totals, SHA-256 of each raw capture).

Every profiled scenario also checks normal cleanup: no workload, perf,
runner or interpreter process is left, the sentinel is unchanged, the
command made only its requested outputs, and `TMPDIR` is unchanged.

| Group | Scenario | Checks (besides cleanup) |
| --- | --- | --- |
| preflight | no perf / no Heaptrack on `PATH` | exit 1, stderr names the tool, no Participant started, no `DIR` |
| preflight | perf denied (paranoid 4; image without `CAP_PERFMON`) | exit 1, stderr names `perf_event_paranoid` and `CAP_PERFMON`, no Participant started |
| preflight | heap scope a shimmed or a Native participant; existing `--out` | exit 2, no Participant started; existing evidence unchanged |
| equivalence | `sil run` and `sil run --diagnostics` of `main`, `mixed`, `failure`, `deadline` | expected exit; diagnostics outcome (exit, failure category, Recording SHA-256) equals ordinary |
| cpu | `main` with matching debug file, `--activations`, `--modules-from` FMU | complete; Recording and outcome equal ordinary; Manifest bytes unchanged, profile hash is the Run's; all 5 hot functions named (Native, Process library, child, FMU); `separate_matching`; FMU from the Run directory named; 4 children with own identities and samples; correlation complete; Native samples in their Task's execution context; Process samples window-associated; every Process mapping verified |
| cpu | `main` with missing / mismatched debug file | `not_found` / `separate_mismatched`; static Native functions unnamed, samples stay in the module; other functions named |
| cpu | `mixed` with `--activations` | complete; Recording equals ordinary; each Task has `ceil(1 s / Period)` completed Steps (10, 20, 30 ms); Replay Slots are separate intervals; FMU group samples window-associated |
| cpu | `failure`, `deadline` | Run status `run_failure`, its exit code 1 and failure category and Recording equal the ordinary Run's; capture complete |
| cpu | `long`, SIGINT to `sil profile` after 2 s | exit 1, Run `interrupted`, capture `interrupted`, `perf.data` kept |
| cpu | `long`, SIGKILL to `perf record` after 2 s | exit 1, Run `succeeded`, capture `failed` (`backend_failed`) |
| cpu | `--max-capture-bytes 2000000` | exit 1, Run succeeded, Recording equals ordinary, capture `partial` (`capture_limit_reached`) |
| cpu | `--max-activation-bytes 4096` | exit 1, capture complete, correlation `partial` (`observations_truncated`), Recording equals ordinary |
| heap | runner scope, matching / mismatched / missing debug file | complete; Recording equals ordinary; correlation unavailable; matching: exact calls and bytes of both Native sites, no Process participant site; others: no name for the stripped sites, counts kept |
| heap | `--participant fmu` | exact calls and bytes of the FMU site; no Native site; Recording equals ordinary |
| heap | `mixed`: `--participant group`, `--participant probe` | complete; Recording equals ordinary (also the probe's environment digest); the group scope is the importer of all three instances |
| heap | `failure` runner; `long` with SIGINT; `long` with SIGKILL to `heaptrack_interpret`; `--max-capture-bytes 65536` | outcome equals ordinary; `interrupted`; not complete, exit 1, no hang; `partial` with the Recording equal to ordinary |
| storage | `--out` on the full small file system, Recording elsewhere | command ends, exit not 0, no process left, sentinel unchanged; heap: the Run goes on (Recording equals ordinary) |
| render | each copied portable set | exit 0, HTML written, no network reference, summary printed; in an environment without perf, Heaptrack or the profiled binaries |

## Findings and the fixes they caused

The first qualification run (workflow run 39341633342) found three defects of
the shipped command. This branch fixes them, with tests, and the retained run
qualifies the fixed command:

1. **perf changed the Run's environment.** perf 6.x sets `PERF_BUILDID_DIR`
   and `PREFIX`, empties `DEBUGINFOD_URLS`, puts its own directory first on
   `PATH`, and the workload lost `TMPDIR`. The probe's environment digest
   showed it. Fix: `sil-diagnostics-launcher --environment-fd M`; the
   command passes its own environment and the runner gets exactly that.
2. **The heap launch adapter set `PWD`.** `sh` sets `PWD` at start. Fix: the
   adapter restores the command's `PWD`, or removes it.
3. **A full disk hung the heap collector.** The copy thread of the
   interpreter died on `ENOSPC`, the interpreter blocked on a full pipe, and
   the command waited until the 30-minute scenario timeout. Fix: the copies
   drop what they cannot write; the capture is `data_truncated` and the Run
   goes on.

Smaller fixes from the arm64 dry run: a launcher failure is now reported on
stderr (a `noexec` profile directory gave exit 127 without a message), and
a perf that cannot start (an image without `CAP_PERFMON`) gives the
permission hint.

Observations, not defects of SiL:

- **perf on a full disk ends the Run.** When the file system of `DIR` fills
  before `--max-capture-bytes`, perf stops and sends SIGTERM to its command:
  the Run is `interrupted` and `profile.json` cannot be written (exit 1, no
  process left, no unrelated file touched). docs/profile-report.md says to
  keep `--max-capture-bytes` below the free space.
- **A killed collector keeps the Run.** SIGKILL to perf or to the Heaptrack
  interpreter: the Run completes with its ordinary Recording; the capture is
  `failed` and the command exits 1.
- **The file capability fails closed.** In the documented image without
  `--cap-add PERFMON`, exec of perf fails with EPERM: the preflight stops with
  exit 1 before any Participant starts.

## Observations: overhead and output growth

Single Runs on a shared CI VM, from the retained run. They are not gates,
benchmarks or a prediction for another host. No overhead is subtracted.

Wall time of the whole command in ms; sizes of the profile directory files.
Native: release install on the runner (perf 7.0.14, Heaptrack 1.5.0, Python
3.12); container: the documented image (perf 6.1.176, Heaptrack 1.4.0,
Python 3.13). The native heap Runs ran at paranoid 4, the native CPU Runs
at paranoid 2.

| Workload | Environment | `sil run` | `--diagnostics` | `--collector cpu --activations` | heap scope |
| --- | --- | --- | --- | --- | --- |
| `main` | native | 2795 | 2829 | 4093 | runner 2974, `fmu` 3217 |
| `main` | container | 3414 | 3477 | 6226 | runner 3628, `fmu` 5198 |
| `mixed` | native | 1410 | 1442 | 2136 | `group` 1820, `probe` 1739 |
| `mixed` | container | 2476 | 2556 | 5262 | `group` 4266, `probe` 3993 |

| Workload | Environment | `perf.data` | `samples.json` | `correlation.json` | `observations.jsonl` | samples |
| --- | --- | --- | --- | --- | --- | --- |
| `main` | native | 22.98 MB | 182 KB | 133 KB | 63 KB | 2719 |
| `main` | container | 26.69 MB | 346 KB | 151 KB | 63 KB | 3150 |
| `mixed` | native | 10.64 MB | 180 KB | 99 KB | 66 KB | 1252 |
| `mixed` | container | 18.16 MB | 457 KB | 135 KB | 66 KB | 2134 |

| Workload, scope | Environment | `heaptrack.data` | `allocations.json` | allocations |
| --- | --- | --- | --- | --- |
| `main`, runner | native / container | 438 KB / 438 KB | 177 KB / 175 KB | 22896 / 22869 |
| `main`, `fmu` | native / container | 711 KB / 4.31 MB | 1.48 MB / 6.04 MB | 17980 / 84036 |
| `mixed`, `group` | native / container | 717 KB / 4.31 MB | 1.49 MB / 6.05 MB | 18176 / 84211 |

The Python Process participants allocate more under Python 3.13 in the
image than under 3.12 natively; the counts of the known C sites are exact in
both. The HTML pages of the copied sets are 68 KB to 861 KB, rendered in 0.1
to 1.9 s.

perf with DWARF stacks writes about 8.5 KB per sample; the normalized
`samples.json` is 0.8 to 2.5 percent of `perf.data`. So at the default
limits the render path (256 MiB per file) and the export (twice
`--max-capture-bytes`) are far from their limits.

## Phase 1 acceptance of #313

Each item of #313 ("Qualification and completion", the required behavior)
and of #319, with its evidence or an explicit revised-scope decision.

### #313 "Qualification and completion"

| # | Item | Evidence or decision |
| --- | --- | --- |
| Q1 | Capture on optimized workloads with known hot functions and allocation sites: Native, Process, Clock shim, shared library/FMU; pinned versions; evidence under `proofs/` | #314 proof ([../profiling/README.md](../profiling/README.md)); here `cpu-main-matching-activations` (5 of 5 hot functions), `heap-runner-debug-matching` and `heap-participant-fmu` (exact calls and bytes); versions in `facts` and [Outcome](#outcome) |
| Q2 | Symbolized, stripped + separate debug, missing and mismatched debug, short-lived processes; identities kept before Run-directory cleanup | `cpu-main-debug-{matching,missing,mismatched}`, `heap-runner-debug-*`; 4 children with own identities; the FMU from the removed Run directory is named (`--modules-from`, retained Run directory) |
| Q3 | Normalization, joins, totals, partial outcomes and null reasons pinned with synthetic fixtures; no CI gate on noisy values | `tests/fixtures/profile/`, `tests/test_profile_*.py` (#315 to #318, run by `make test`); no check here uses a time, share, RSS or overhead |
| Q4 | Opt-in real-backend checks on a capable host, explicit unsupported and permission outcomes; mock-only success does not qualify | this workflow and `profile-cpu-integration`, `profile-heap-integration` (real perf and Heaptrack); `preflight-*` scenarios at paranoid 4 and without `CAP_PERFMON` |
| Q5 | Ordinary, diagnostics and profiled Recordings/outcomes equal on deterministic success and failure fixtures; deadlines, interruption, child/collector failure, storage limits, cleanup | `equivalence` group; `cpu-*` and `heap-*` "Run outcome and Recording equal the ordinary Run" for `main`, `mixed`, `failure`, `deadline`; `*-interrupted`, `*-collector-killed`, `*-limit`, `storage-*`; cleanup checks in every scenario |
| Q6 | Overhead and output growth on named workloads and hosts, with bounds, no guessed subtraction | [Observations](#observations-overhead-and-output-growth) |
| Q7 | Installed native and documented container workflows, version compatibility, offline rendering; guides for shipped capabilities | [Outcome](#outcome); perf 6.1/6.17, Heaptrack 1.4/1.5; `render-*`; SUPPORT.md, docs/running.md, docs/container.md, docs/agent-guide.md, docs/profile-report.md, GLOSSARY.md |
| Q8 | `make build`, `make test`, determinism gate, plus qualified backend evidence | local gates on the branch (PR #335); this run |

### #319 acceptance

| # | Item | Evidence or decision |
| --- | --- | --- |
| A1 | Gates; real-backend results kept separately | Q8; [Evidence](#evidence) |
| A2 | Recordings/outcomes match; timing-edge limits documented | Q5; docs/running.md "Profile a Run" and docs/profile-report.md: a collector adds work, so a Run that only just meets its deadline can miss it (decision D9) |
| A3 | No silent change of Manifest identity, process path/argument semantics, Clock-shim behavior or cleanup | Manifest SHA-256 and hash checks; probe digests of `argv`, environment and working directory inside the Recording; the shimmed participant's clock readings in the `main` Recording; cleanup checks. Fixed on this branch: perf's environment change and the adapter's `PWD` (Findings 1, 2) |
| A4 | Finite buffer, storage and analysis limits with partial results; no unrelated file overwritten or deleted | `cpu-capture-limit`, `cpu-activation-limit`, `heap-capture-limit` (`partial`, Run unchanged); export limit and render limit (`tests/test_profile_run.py`, `tests/test_profile_render.py`); `storage-*`; sentinel, `TMPDIR` and `preflight-existing-out` checks |
| A5 | Missing tools/permissions are actionable; no automatic host-policy change | `preflight-*`; `sil profile` changes no setting: the only host changes are the two disclosed workflow steps |
| A6 | Installed users capture and render a copied normalized set offline with documented steps | native install through the release's `install.sh`; the documented image; `render-container` (no profiler, `--network none`) and `render-native` (only the installed SiL on `PATH`) |
| A7 | Overhead and artifact growth measured on named workloads and hosts, no thresholds | Q6 |
| A8 | Support, running, report-contract and agent-guide docs for shipped behavior; precise optional backend support | Q7; SUPPORT.md "Optional profiling backends" |
| A9 | Every Phase 1 item has evidence or a revised-scope decision; Phase 2 does not block | this table; #320 and #321 stay open and do not block |

### Required behavior of #313, by area

| Area | Evidence |
| --- | --- |
| One fresh Run, one collector, new output directory, preflight before Participants, atomic summary | #315 tests; `preflight-*` ("no Participant started", existing evidence unchanged) |
| Run boundary only: Manifest bytes, working directory, arguments, environment, Step protocol | A3 |
| CPU scope: runner and all descendants, per-process/thread evidence | children check, `processes` in each `profile.json` (artifact) |
| Heap scope: runner or named Process participant; Native name rejected; FMU group one scope | `heap-runner-*`, `heap-participant-fmu`, `heap-mixed-group`, `preflight-heap-native-participant` |
| Evidence contract `sil_profile_report` 1, identities, debug match status, null reasons | #315 validator and fixtures; every `profile.json` here passes `validate_profile_report` |
| CPU semantics: explicit event and weighting, DWARF unwinding and source lines separate | `capture.sampling`, `capture.symbolization` in each `profile.json`; #314 caller check |
| Activation correlation: common clock, verified PID namespace and process mapping, bounded sidecar, truncation | `cpu-*-activations` (complete, verified), `cpu-activation-limit` (truncated, Run unchanged) |
| Allocation semantics and boundaries | exact site counts; docs/profile-report.md "Heap collector" |
| Failure and resource behavior: Run outcome first, exit 1 for a bad capture, stop and reap on interruption | `cpu-failure`, `cpu-deadline`, `heap-failure`, `*-interrupted`, `*-collector-killed`, cleanup checks |
| Offline presentation: no network, no binaries, copied set | `render-*`; #318 browser tests |

### Revised-scope decisions

| # | Decision |
| --- | --- |
| D1 | Heaptrack in a shimmed Process participant is rejected in preflight (exit 2); investigation #330 does not block Phase 1. |
| D2 | Heaptrack Activation correlation is unavailable (no validated clock mapping). |
| D3 | Only perf in the Run's own PID namespace is qualified (native and inside the container). perf on a host that samples into a container is not supported; `pid_namespace_mismatch` is covered by synthetic tests only. |
| D4 | PID reuse within one capture is covered by synthetic fixtures (#315, #316), not observed with a real backend. |
| D5 | No hardware PMU is used. The qualified host has none (`facts.hardware_pmu` false): the "missing PMU" case is the qualified case, with the software event `cpu-clock:u`. Kernel samples and hardware events are out of scope. |
| D6 | Custom allocator pools: the limit is documented (docs/profile-report.md "Coverage and limits"), not measured with a dedicated custom-pool workload. |
| D7 | A full profile file system: qualified for "no hang, explicit failure, nothing unrelated changed". For the CPU collector perf ends the Run (SIGTERM); the documented mitigation is `--max-capture-bytes` below the free space. |
| D8 | The native install uses the release's `install.sh` on a local mirror of release files built from this commit, not a published release. |
| D9 | Equivalence holds for deterministic fixtures with generous deadlines; timing-edge workloads are not covered. |
| D10 | An output collision is a usage error (exit 2), as #313 requires: an existing `--out` for the capture command and an existing or homeless `--html` for `sil profile render`. `sil profile render` checks the output path before it reads the set. Exit 1 is kept for a write that fails. |
| D11 | Linux x86-64/glibc only; arm64 (#311) and macOS make no profiling claim. A Process participant written in C is not a separate case: native code in Process participants is covered by the ctypes library, the child and the FMUs. |

## Evidence

[`evidence/`](evidence/) holds the retained output of workflow run
[39373889739](https://github.com/Stevie1704/sil/actions/runs/39373889739) at commit `dc711f9`, copied by [`retain.py`](retain.py):

| Path | Content |
| --- | --- |
| `<environment>/results.json`, `results.md` | environment identity, every command, check and observation, SHA-256 of each raw capture |
| `container-*/docker-options.txt`, `image-id.txt` | the `docker run` options and the profiling image |
| `render-*/result/` | the offline render of every copied set |
| `digests.txt` | SHA-256 of every file of the workflow artifact |
| `host-paranoid-before.txt` | `perf_event_paranoid` before the host change |

The profile summaries, the portable sets and the HTML pages stay in the
workflow artifact (`profiling-qualification-evidence`); the raw captures
(`perf.data`, `heaptrack.data`, observation streams, Recordings) stay in the
job's work directory and are identified by their SHA-256 in
`results.json`. `perf.data` holds copies of user stacks.

To repeat: push a change under `proofs/profiling-qualification/` in a pull
request. Locally, on an x86-64 Linux host with Docker:
`container.sh images IDS`, `container.sh build IDS WORK`, then
`container.sh run IDS WORK EVIDENCE --cap-add PERFMON -- --environment NAME
--perf available`.
