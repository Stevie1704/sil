# Agent guide

This guide is the entry point for a coding agent that helps a user test a
system under test (SUT) with SiL. Read it with `sil docs agent-guide`. These
docs are for the installed version: `sil docs` lists the topics, and
`sil docs --path` prints their directory, so you can search them with `grep`.

## What SiL is

SiL runs virtual ECUs (vECUs) and models deterministically in Virtual time.
One Run executes one Manifest and writes one Recording. The same artifacts on
the same machine class give bit-identical Recordings. The terms in this guide
(Run, Manifest, Participant, Channel, Schema, Recording) have exact meanings:
read `sil docs glossary`.

| Part | What it is |
| --- | --- |
| Manifest | A canonical JSON file, SHA-256 hashed, the single input of a Run. Write it with the `sil.manifest` Python builder. |
| Participant | The code that a Run schedules: a Native participant (a shared library against the C ABI), a Process participant (an executable that speaks the Step protocol), or a Replay participant. |
| Channel | A typed connection between Participants. Its Schema gives the payload layout. |
| Recording | An MCAP file with virtual timestamps only. |

## Onboard a SUT

Do the steps in this order. Each step names the topic to read.

1. **Inspect the SUT.** Find out how it can join a Run:
   - An FMU: run `sil fmi inspect model.fmu --json`. Read `sil docs fmi`.
   - A C library: read `sil docs library`. For a large C interface, `sil schema
     import` writes a Schema from the DWARF data of the object file (it needs
     the `sil[dwarf]` extra).
   - An executable: it must speak the Step protocol. Read
     `sil docs step-protocol`. A Python Participant uses `sil.participant`.
   - A C application against the Native ABI: read `sil docs adas-reference`.
2. **Author the Manifest.** Use the `sil.manifest` builder (`Manifest`,
   `add_schemas`, `add_channel`, `add_process`, `write`). `sil docs
   getting-started` names the packaged ACC example
   (`python -m sil.examples.acc.manifest acc.json`), a complete Manifest to
   copy from. For FMUs, `sil fmi couple`, `sil fmi replay` and
   `sil fmi substitute` write the Manifest for you: read `sil docs
   fmu-coupling` and `sil docs recorded-input`.
3. **Run.** `sil run manifest.json -o out.mcap` starts the native `sil-run`,
   and `sil check` uses it too. The wheel does not contain `sil-run`: it must
   be on `PATH` from a native installation, or you run in the container image
   (read `sil docs container`). Read `sil docs running` for the exit codes,
   the guards and the Clock shim.
4. **Check.** `sil recording channels out.mcap` lists the Channels of the
   Recording, and `sil recording show out.mcap --channel NAME --from T --to T`
   prints what a Channel carried in a virtual-time interval, decoded with its
   Schema: read `sil docs running`. `sil check manifest.json` runs the
   Manifest twice and fails on any Recording difference. `sil compare`
   compares a Recording with a reference under a contract: read `sil docs
   comparison`. To keep a case for CI, seal it as a regression bundle: read
   `sil docs regression-bundles`.

## Machine-readable output

Use exit codes, not message text. Message text can change in any release.

| Command | Output |
| --- | --- |
| `sil fmi inspect --json` | the `sil_fmi_inspection` report |
| `sil compare --json` | the `sil_comparison_report` report |
| `sil recording channels --json` | the `sil_recording_channels` report |
| `sil recording show --json` | one `sil_recording_show` JSON object per line and per Message |
| `sil recording csv --receipt`, `sil recording window --receipt` | a JSON receipt |
| `sil fmi couple --receipt`, `sil fmi replay --receipt`, `sil fmi substitute --receipt` | a JSON receipt |
| `sil run` | the Recording and its `<recording>.provenance.json` side-car |
| every command | the exit code: `sil docs running` lists those of a Run |

`sil docs support` says which interfaces and report versions are stable, and
which are implementation details.
