---
name: sil
description: Test a system under test (SUT) deterministically with SiL, the `sil` command. Use it to bring a SUT (an FMU, a C library or an executable) into a Run as a Participant. Use it to inspect or couple FMUs, or to write and validate a Manifest. Use it to execute a Run into a Recording, or to read and compare Recordings. Use it for a determinism check, or to find why a Run failed. Use it to measure wall time, Task and Step times, CPU time or peak memory, benchmark repeated Runs, capture or explore CPU/heap profiles, or compare saved profiles. Use it when the user names SiL, a vECU, an FMU, a Manifest, a Recording, a determinism check, a Run failure, or a slow Run.
metadata:
  sil-version: "$version"
---

# SiL

This skill is for SiL $version. When you change the installed SiL version, run
`sil skill install` again with the same options.

## Steps

Each step names the docs to read. All docs are offline and match the
installed version.

1. **Inspect** the SUT: `sil docs fmi` (an FMU), `sil docs library` (a C
   library), `sil docs step-protocol` (an executable). When a guide uses
   `examples/...` files, get them with `sil examples --dir examples`:
   `sil docs examples`.
2. **Author** the Manifest: `sil docs getting-started`, `sil docs fmu-coupling`.
3. **Run** it into a Recording: `sil docs running`, `sil docs container`.
   A failed Run writes `<recording>.failure.json`: `sil docs running`.
4. **Check** the Recording: `sil docs comparison`, `sil docs regression-bundles`.
5. **Investigate** a failed Run or matrix case: `sil docs agent-guide`, the
   section "Investigate a failed Run".
6. **Measure** run time, CPU time or memory: `sil docs agent-guide`, the
   section "Measure a Run", then `sil docs diagnostics-report`.
7. **Profile or compare** CPU/heap evidence: `sil docs agent-guide`, the
   section "Profile a Run", then `sil docs profile-report`. Use `sil profile
   render` for flame graphs, the Activation timeline and per-Task drill-downs;
   `sil profile diff BASE/profile.json NEW/profile.json [--html OUT.html]`
   compares portable sets offline. Check identities, completeness and
   denominators first. Keep execution context and window association
   separate; small CPU share changes may be sampling noise.

## Rules

- Do not guess an argument, a field or a behavior. First run
  `sil <command> --help`, or `sil docs` to find the topic and then read it.
- Use exit codes and the `--json` reports and `--receipt` files. Do not parse
  message text: it can change in any release.

## Commands

| Command | What it does |
| --- | --- |
$commands

For everything else, read `sil docs agent-guide`.
