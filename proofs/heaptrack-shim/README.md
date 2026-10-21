# Heap profiling of Clock-shimmed Process participants (#330)

This investigation follows [#314](../profiling/README.md) and #317. It tests
whether restoring the Clock shim in descendants is enough to qualify Heaptrack
for a shimmed Process participant, and evaluates the other candidate directions
against pinned sources. It changes no production interface or runtime code.

The decision is **defer support**: keep the existing `sil profile --collector
heap --participant NAME` rejection for a shimmed Participant. A descendant
preload repair alone cannot provide a host timestamp clock or correct profiler
sleep behavior. The source analysis also identifies initialization and
application-thread calls that prevent a simple timer-thread exemption. This is
not a claim that a separately designed collector or modified Heaptrack cannot
work. No build is proposed by this investigation, and #317 is not widened.

## Candidate decisions

| Candidate | Disposition | Reason |
| --- | --- | --- |
| Attach with `heaptrack -p PID` | Rejected from pinned source | Injection retains the same clock/sleep engine. Attaching after startup misses earlier allocations; pausing later cannot recover them. |
| Exempt a profiler thread or caller in the shim | Rejected from pinned source for the existing contract | Timer-thread exemption misses initialization, shutdown and allocation-lock calls on application threads. Caller classification through libstdc++ needs a new specified boundary and has unmeasured cost. It also needs to account for the shim's thread policy. |
| Restore the shim in descendants | Tested target-resident prototype; insufficient by itself | Force Heaptrack initialization, then restore only the shim in `LD_PRELOAD`. Compare child and grandchild clocks and the entire Recording. Clock and sleep interference remain independent requirements. |
| Out-of-process allocator tracing | Deferred from pinned BCC/bpftrace source | A new collector needs allocator coverage, startup/descendant coverage, permission checks, stack qualification, loss accounting and a measured overhead/growth budget. `memleak` outstanding allocations are not a complete allocation history. |
| Keep the Phase 1 rejection | Retained | Does not change the determinism or profiling contracts. Runner heap profiling covers Native participants; an explicitly unshimmed Process variant is a different experiment. |

[`candidates.md`](candidates.md) records primary-source links and source commits
for Heaptrack 1.4.0/1.5.0, bpftrace 0.21.0 and BCC 0.29.1. Source rejections are
reasoned evaluations of these candidates on the supported Linux x86-64/glibc
machine class, **not runtime tests** of attach, a modified shim or eBPF. No
privilege, stack-quality or numerical-overhead claim is made for an untested
BPF collector. The previous proof retains measured Heaptrack 1.4 and 1.5 results.

## Reproduce

The [workflow](../../.github/workflows/proof-heaptrack-shim.yml) runs natively on
Ubuntu 24.04 x86-64/glibc, as the unprivileged runner user. It pins
`heaptrack=1.5.0+dfsg1-2ubuntu3` and GCC/G++ 12.4 packages, records package and
binary identities, and makes no host policy change. From a checkout with the
same packages and the Python dev environment installed:

```sh
CC=gcc-12 CXX=g++-12 bash proofs/heaptrack-shim/run-proof.sh \
  "$PWD/build/heaptrack-shim-evidence" "$PWD/build/heaptrack-shim-build"
```

The output directory must be new. The script compiles the ordinary runner,
diagnostics runner and Clock shim. The small workload and restoration library
are compiled with `-O2 -g`. The fixture performs 80 allocations of 2048 bytes at
`proof_alloc_site` over ten Steps, publishing libc monotonic clock readings from
the Participant, an exec child and an exec grandchild at every Step. It also
publishes the return value and errno of one Participant sleep call per Step.
A direct host syscall waits 25 ms per Step solely to keep this short capture
observable; this fixture pacing is not Participant Virtual time.

For each of `sleep="reject"` and `sleep="immediate"`, the driver runs:

1. The ordinary `sil-run` baseline.
2. Heaptrack before the Clock shim, with the existing lazy-initialization
   behavior (control reproducing the original incompatibility).
3. A target-resident restoration library before Heaptrack and the Clock shim.
   Its constructor forces a small malloc/free, then restores only the shim for
   future execs. This is a disposable experiment, not an installed adapter or
   a generally qualified constructor-order solution.

Each policy uses exactly the same Manifest bytes for all three Runs. The
existing diagnostics runner launch boundary wraps only the profiled Participant;
ordinary `sil-run` is unchanged. The driver compares complete Recording bytes
against the ordinary baseline, checks all three clock readings against each
Message's Virtual time, and checks sleep policy results. It interprets the
capture while fixture binaries still exist and independently checks the known
allocation count and size through the production normalizer. Run failures are
retained with stderr and failure sidecars; an incomplete Recording cannot count
as equal. A usage/Manifest error fails the proof, as does a missing or
undecodable capture from a successful Run.

Every capture has a 30 s wall limit and a 128 MiB raw-file stop threshold checked
at 20 ms intervals (a threshold can be exceeded between checks). A limit hit
fails evidence collection. Size, timestamp-record count/range, wall duration
and raw bytes per wall second are observations, not timing gates or a prediction
for another workload. Timestamp origin can be initialized before the shim maps
its region; later clock reads can be Virtual time. There is no validated host
mapping. Equality of Recordings alone does not qualify the collector.

## Native observations

The [retained native run](https://github.com/Stevie1704/sil/actions/runs/39570810756)
used Heaptrack **1.5.0+dfsg1-2ubuntu3**, GCC **12.4.0**, glibc **2.39**, and
libstdc++ **14.2.0** on Linux **6.17.0-1022-azure**, x86-64, UID 1001. The source
head was `b7fde1f`; the tested PR merge revision and artifact digest are in
[`origin.json`](evidence/origin.json) and [`results.json`](evidence/results.json).

| Sleep policy | Capture | Recording equal | Parent/child/grandchild clocks virtual | Raw bytes | Timestamp records | Wall ms |
| --- | --- | --- | --- | ---: | ---: | ---: |
| reject | preload | no | yes / no / no | 12,863,856 | 464,239 | 864 |
| reject | preserve | yes | yes / yes / yes | 12,975,186 | 467,391 | 864 |
| immediate | preload | no | yes / no / no | 12,813,709 | 462,304 | 884 |
| immediate | preserve | yes | yes / yes / yes | 12,752,329 | 458,827 | 864 |

All six Runs completed, including the ordinary baselines (844 and 783 ms).
Participant sleep results matched both policies in every Run. Each capture
resolved exactly **80 allocations of 2048 bytes** at the known site, with no
undecodable records or truncated input. The Heaptrack 1.5 startup failure from
#314 **did not reproduce** in this fixture; its absence here does not erase
that earlier observation or qualify all shimmed Participants.

The four captures grew at **14.5–15.0 MB per wall second** (decimal MB). Each
had ten distinct timestamp values over a 900 ms range, while emitting hundreds
of thousands of repeated records. All timestamp values exceeded 2^63. This is
consistent with the mixed host-origin/Virtual-time subtraction explained in
`candidates.md`, not a validated host-clock mapping. Neither a numerical growth
threshold nor a percentile is a pass/fail criterion. These single Runs describe
this fixture and host only.

The experiment therefore isolates descendant preload repair as useful but
insufficient: it restores the Recording, and Participant sleep calls match both policies
with and without repair. The collector's timestamp clock and timer growth
remain unsuitable. A separately
specified profiler integration would have to solve those problems and cover
initialization, allocation-thread calls and thread policy. This investigation
**defers** that work and keeps the existing rejection.

## Evidence

The workflow uploads `heaptrack-shim-evidence` for 30 days. `results.json`
contains host/tool identities, build commands, binary digests, the full matrix,
Recording comparisons, clock and sleep checks, allocation checks, timestamp
statistics and growth. Each Run retains its messages, stdout/stderr, Recording,
provenance/failure/diagnostics sidecars and raw/interpreted Heaptrack data.
`digests.json` inventories the complete experiment output. Small results, Manifests, sidecars, decoded
Messages and logs are copied into [`evidence/`](evidence/) after the native run;
large captures stay in the workflow artifact and are identified by digest.
An ARM64 Linux run with `--allow-other-machine` is only a smoke check and is
never retained as x86-64 qualification evidence.

To reproduce the retained subset after downloading the workflow artifact:

```sh
gh run download 39570810756 -R Stevie1704/sil \
  -n heaptrack-shim-evidence -D build/heaptrack-shim-artifact
python proofs/heaptrack-shim/retain.py \
  build/heaptrack-shim-artifact/heaptrack-shim-evidence \
  proofs/heaptrack-shim/evidence
```

The retention script verifies copied files against the original inventory.
`tests/test_heaptrack_shim_proof.py` independently checks the retained matrix,
clock/sleep observations, verdict consistency and integrity, including rejection
of modified or truncated artifact files.
`origin.json` separately identifies the workflow and compressed artifact; it
is not part of the original experiment inventory.
