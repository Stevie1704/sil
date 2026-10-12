---
name: sil
description: Test a system under test deterministically with SiL, the `sil` command. Use it to onboard a SUT (an FMU, a C library or an executable) as a Participant, inspect or couple FMUs, write and validate a Manifest, execute a Run into a Recording, read or compare Recordings, do a determinism check, or find why a Run failed. Use it when the user names SiL, a vECU, an FMU, a Manifest, a Recording, a determinism check or a Run failure.
metadata:
  sil-version: "$version"
---

# SiL

This skill is for SiL $version. If `python -c "import sil; print(sil.__version__)"`
prints a different version, run `sil skill install` again with the same options.

## Steps

Each step names the docs to read. All docs are offline and match the
installed version.

1. **Inspect** the SUT: `sil docs fmi` (an FMU), `sil docs library` (a C
   library), `sil docs step-protocol` (an executable).
2. **Author** the Manifest: `sil docs getting-started`, `sil docs fmu-coupling`.
3. **Run** it into a Recording: `sil docs running`, `sil docs container`.
4. **Check** the Recording: `sil docs comparison`, `sil docs regression-bundles`.
   A failed Run writes `<recording>.failure.json`: `sil docs running`.

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
