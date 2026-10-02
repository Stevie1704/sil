# Reference FMUs

The Modelica Association Project "FMI" publishes these FMUs as the conformance
target for an FMI implementation. Two of the FMI 3.0 models and five of the
FMI 2.0 models are vendored here as test fixtures for the co-simulation
importer (`sil.fmi`).

| Fixture | What it is for |
| --- | --- |
| `3.0/Feedthrough.fmu` | Every output equals its input, so a Run proves the Channel→variable mapping round-trips. |
| `3.0/BouncingBall.fmu` | A model with state and events, so a Run exercises a trajectory rather than a pass-through. |
| `2.0/Dahlquist.fmu`, `2.0/VanDerPol.fmu` | Real outputs with continuous state, for the FMI 2.0 profile (issue #191). |
| `2.0/BouncingBall.fmu` | Real outputs with events inside a communication step. |
| `2.0/Stair.fmu` | An Integer output driven by time events. |
| `2.0/Feedthrough.fmu` | Declares `String` and `Enumeration` variables, so the FMI 2.0 profile must refuse it. |

## Provenance

- Source: <https://github.com/modelica/Reference-FMUs/releases/tag/v0.0.41>
- Release asset: `Reference-FMUs.zip`
- SHA-256 of the vendored files:
  - `cbb038007286be266707fb919ed4614ea3c5eba3c18941e3d50482a2b8e467c9  3.0/Feedthrough.fmu`
  - `f14f1f46c97c21bc208c139fa30d19ebce5b1d0fa0789f09a1ff1b9a6b9ee8c5  3.0/BouncingBall.fmu`
  - `3959d0dea751e788b7b9591da6beb11955352a1ac46138244ee5343efb7ce5e4  2.0/BouncingBall.fmu`
  - `cecf1fb0f04cbb9de102c783dd7d905a17aec0c6e8ce4641f54a0b26673c2c7f  2.0/Dahlquist.fmu`
  - `2d4ac516803691be0bb69e92a85d5ba70abab795323903bbb3e719af6964ddad  2.0/Feedthrough.fmu`
  - `917f5a7123cf5b2857ac6bc7a121fe96e0a37a9a734b84d0166fd0554298789c  2.0/Stair.fmu`
  - `c22b935903fab007c57319d4134b6a9c08a7139d90c97150310dc05afe823a3d  2.0/VanDerPol.fmu`
- SHA-256 of the release asset these were taken from: `62babca76b9c23a51c3096be4bb5930ff8b4388659056be3c0c4ef7a3aeb5403`, the pin in [`proofs/public-workloads/sources.json`](../../../proofs/public-workloads/sources.json)

The bytes are pinned rather than downloaded at test time: "the same artifacts"
is what Determinism is scoped to, and a pinned fixture also keeps CI off the
network. One artifact carries `aarch64-darwin`, `x86_64-darwin`,
`aarch64-linux` and `x86_64-linux` binaries, so the CI runner and a developer
Mac read identical bytes and the suite is not platform-forked. The FMI 2.0 archives
carry `linux64`, `darwin64`, `win32` and `win64` binaries, all x86 builds, so
a Run of one is tested on Linux x86-64 only; reading and inspecting them is
tested everywhere.

## License

The Reference FMUs are released under the 2-Clause BSD license, which permits
redistribution provided the copyright notice is retained. `LICENSE.txt` is the
notice as published, copied verbatim.
