# Baseline qualification retained for issues #152 and #153

This directory preserves the pre-arbitration qualification. Issue #154's
model and configuration artifacts are retained separately under
[`issue-154/`](issue-154/README.md), and issue #155's deterministic fault
traces and fixture provenance are under
[`issue-155/`](issue-155/README.md). The earlier upstream proof stays under
`proofs/fmi-ls-bus/`.

Issue #156's first-party live/replay comparison is retained separately under
[`issue-156/`](issue-156/README.md), including source and replay Recordings,
fault configuration, per-Channel comparisons and provenance.

Linux x86-64 (Docker on an ARM development host), Python 3.13.7, FMPy 0.3.32,
GNU C++ 12.2.0. The image used was
`sha256:f02c5c1b1e47efe66a066f3a6619983c6c87363980acfabbe56a03de063828e9`,
and `qualification.json` hashes it. If you rebuild, `build/can/image.txt`
holds the authoritative image ID. New source inputs or toolchain images
produce new qualification identities.

- All 56 `models/can/tests` checks passed. They include exact external payload,
  recipient, confirmation and event-time assertions through independent FMPy
  calls and the existing SiL FMU group: the upstream 83-bit frame at 100 kbit/s
  is delivered at 300830000 ns.
- Hand-derived timing vectors (ID 0, 0/1/8 zero data bytes: 50/56/124 bits) at
  125 and 500 kbit/s passed through the FMI boundary and matched the
  independent `tests/wire.py` reference.
- The burst (A, then B requested while A is on the wire, then C from the other
  node during B) produced frame ends at 249000, 501000 and 673000 ns. The same
  trace resulted on an independent FMPy master at four outer Step grids and in
  SiL Recordings at Step periods 1 ms, 249 us and 1 us (`burst.json`). Every
  Message lies inside the Step that published it, and each completion is
  recorded once.
- A repeated Run of the same burst Manifest and two Runs of the nominal
  Manifest each produced byte-identical Recordings.
- Two controlled `SilCanBus.fmu` builds were byte-identical.
- Rejections: unsupported, unrepresentable and inconsistent bitrates, a
  Transmit before both terminals configured the bitrate, two frames eligible at
  one arbitration opportunity, and time beyond the supported range. Two
  competing upstream senders caused the SiL Run to exit with code 1.
- FMI model XML and the released layered-standard schema validated. Source
  digests, licenses, library exports and prohibited dependencies were checked.
- The independent model core passed timing, rejection, truncation and
  single-byte mutation tests under Linux UBSan with recovery disabled.

The FMU embeds Git revision `43788507bd40d716bd57d53c6a4880ad32cdd9f1`
and `source_dirty: false`, alongside its compiler flags and source digests.
The full `models/can/run.sh` pipeline passed from that committed tree.

`independent.json` retains the independent call-path result and the upstream
receiver's log. `sil.json` retains every observed operation with its FMI event
time and its publication Slot. `burst.json` retains the normalized burst trace
for each Step period. `qualification.json` binds artifacts and inputs by
SHA-256. The full artifacts stay in `build/can/`, and the dedicated CI workflow
uploads them. Regenerate them with `models/can/run.sh`.

Repository regression validation on the host: the pytest suite passed 857
tests and skipped 15 tests that need an unavailable platform or tool.
Linux qualification is separate from those host results. No ASan pass is
claimed: the retained sanitizer result is UBSan.
