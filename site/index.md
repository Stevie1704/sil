# SiL

Deterministic software-in-the-loop testing for ADAS/AD. SiL runs your
software, models and test logic together in Virtual time, and records every
Message they exchange. The same artifacts on the same machine class give a
bit-identical Recording, so a changed Recording means a changed behavior.

!!! info "Development documentation"
    This site is built from the `main` branch. The banner shows its source
    revision. It can describe behavior that no release has yet. An installed
    SiL contains the guides of its own version: run `sil docs` to list them.
    The [published releases](https://github.com/Stevie1704/sil/releases)
    have their release notes and installation assets.

## Who SiL helps

- **Teams that test an ADAS/AD function in closed loop** with plant models,
  sensors and test logic, and want each regression Run to reproduce exactly.
- **Integrators of supplier models**: an FMU, a C library or an executable
  joins a Run without a change to SiL.
- **CI owners** who need a pass or fail that does not depend on the load of
  the machine: SiL schedules by Virtual time, not by the wall clock.

## How it works

You write a **Manifest**: the Schemas, Channels, Participants and Duration
of one Run. `sil-run` steps the **Participants** in Virtual time, routes
their Messages through Channels, and writes a **Recording**. `sil check`
runs the Manifest twice and bit-compares the two Recordings.

[How SiL works](../docs/how-sil-works.md) explains the model with diagrams.
The [Glossary](../GLOSSARY.md) defines each term.

## Support boundaries

Read the [support statement](../SUPPORT.md) before you build a process on SiL:

- **Platforms:** releases support Linux x86-64 with glibc 2.36 or newer.
  Windows uses WSL2, and macOS uses the Linux amd64 container image
  ([supported machine class](../SUPPORT.md#supported-machine-class),
  [Windows and macOS](../docs/getting-started.md#windows-and-macos)).
- **Determinism:** same artifacts + same machine class ⇒ bit-identical
  Recordings. The guarantee does not extend across machine classes
  ([the exact boundary](../SUPPORT.md#determinism-the-exact-boundary)).
- **Safety:** SiL is not safety-qualified. It supplies no safety case and
  no tool qualification evidence
  ([not safety-qualified](../SUPPORT.md#not-safety-qualified)).

## Try the ACC example

The packaged ACC example is one closed loop: a plant with two vehicles, a
controller under the Clock shim, and a test participant that holds the Run to
a minimum-gap KPI ([ACC reference example](../docs/getting-started.md#acc-reference-example)).

1. **Install.** On Linux x86-64, the
   [install script](../docs/getting-started.md#install-a-release) of a
   release installs the `sil` tools and the native runtime. The
   [repository overview](../README.md#install-the-latest-release) gives the
   command for the latest release; give `--version VERSION` for another
   release.
2. **Write the Manifest and run it** in an empty directory:

    ```sh
    python -m sil.examples.acc.manifest acc.json
    sil run acc.json -o acc.mcap
    ```

    The first command writes the Manifest and prints its Manifest hash. The
    Run writes the Recording `acc.mcap` and its provenance side-car
    `acc.mcap.provenance.json`, and exits with `0`. Exit code `1` is a Run
    failure, for example a failed KPI; `2` is a Manifest error.

3. **Check its determinism:**

    ```sh
    sil check acc.json
    ```

    It prints `deterministic:` and the SHA-256 of the Recording, and exits
    with `0`. This establishes that two Runs of this Manifest, on this
    machine class, gave the same Recording bytes. It does not certify the
    machine class or the controller. On a difference it exits with `3` and
    reports where the Recordings first differ.

## Choose an integration

[Choose an integration](../docs/integrations.md) gives the guide for what you
have:

- an FMU: the [FMI importer](../docs/fmi.md), or
  [coupled and substituted FMUs](../docs/fmu-coupling.md),
- a C library with its own API: [shared-library participants](../docs/library.md),
- a C application against the C ABI: the
  [Native ADAS reference application](../docs/adas-reference.md),
- an executable in any language: the [Step protocol](../docs/step-protocol.md),
- recorded data: [recorded input](../docs/recorded-input.md) and
  [replay](../docs/replay.md).

## Onboard with a coding agent

Copy this prompt into your coding agent in the project where you want to use
SiL. It installs the latest release:

--8<-- "README.md:agent-prompt"

After the installation:

- `sil skill install` writes the agent skill of the installed version into
  the project. Then the agent finds SiL for tasks on a system under test, an
  FMU, a Manifest, a Recording or a Run.
- `sil docs` lists the guides of the installed version, and
  `sil docs agent-guide` prints the [agent guide](../docs/agent-guide.md),
  the entry point for an agent.

## Evidence

The [acceptance proofs](../docs/acceptance.md) run SiL against third-party and
maintained artifacts, for example the
[esmini consumer adoption proof](../proofs/esmini/README.md), the
[ACC acceptance bundle](../proofs/acc-fmi/README.md) and the
[ADAS closed loop](../proofs/adas-closed-loop/README.md). Each proof README
states what worked and what did not.

## Contribute

[CONTRIBUTING.md](../CONTRIBUTING.md) gives the contribution terms and the
required checks. The [architecture guide](../docs/architecture.md) tells you
where a change goes. The source is on
[GitHub](https://github.com/Stevie1704/sil).
