# Qualification retained for issue #152

Linux x86-64 (Docker on an ARM development host), Python 3.13.7, FMPy 0.3.32,
GNU C++ 12.2.0. The exact image ID is hashed in `qualification.json`; preparation
used image `sha256:4ef220a9a21a33668276ca1e3354a8cb0a76bf77a9c4369fafba83df43848a6b`.
The image ID recorded in `build/can/image.txt` is authoritative if rebuilding;
new source inputs/toolchain images produce new qualification identities.

- All 38 `models/can/tests` checks passed, including exact external payload,
  recipient, confirmation and event-time assertions through independent FMPy
  calls and the existing SiL FMU group.
- Initialization Binary assignments, readable InitialUnknowns and boundary
  guards under `python -O` are covered by six regression cases.
- Two controlled `SilCanSmoke.fmu` builds were byte-identical.
- Two Runs of the nominal Manifest produced byte-identical Recordings; the
  nominal Recording digest is also unchanged from the pre-review qualification.
- Replacing the receive-only peer with the external sender caused competing
  requests and failed with exit 1, as required by the supported profile.
- FMI model XML and the released layered-standard schema validated; source
  digests, licenses, library exports and prohibited dependencies were checked.
- The independent model core passed truncation and single-byte mutation tests
  under Linux UBSan with recovery disabled.

The FMU embeds Git revision `e6e89abc8fd9f3c39d5f35a01b8ec98ffb88f7f4`
and `source_dirty: false`, alongside its compiler flags and source digests.
The full `models/can/run.sh` pipeline passed from that committed tree.

`independent.json` retains the independent call-path result and the upstream
receiver's log. `sil.json` retains every observed operation, its FMI event time
and its publication Slot. `qualification.json` binds artifacts and inputs by
SHA-256. The entire FMUs, Manifests, Recordings, provenance, logs, test XML and
image/source identities remain in `build/can/`, uploaded by the dedicated CI
workflow; regenerate them with `models/can/run.sh`.

Repository regression validation: five CTest tests passed; the host pytest
suite passed 851 tests and skipped 15 platform/tool-dependent tests. Six
installed-bundle checks initially failed in wheel-build setup due to sandbox
restrictions; rerunning their complete module with package access passed all
12 tests. Linux qualification is separate from those host regression results.

ASan could not complete on this development setup: the emulated process was
killed for memory pressure and an optional native attempt was stopped after
four minutes. No ASan pass is claimed; the retained sanitizer result is UBSan.
