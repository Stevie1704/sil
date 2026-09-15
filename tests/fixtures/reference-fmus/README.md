# Reference FMUs

The Modelica Association Project "FMI" publishes these FMUs as the conformance
target for an FMI implementation. Two of the FMI 3.0 models are vendored here
as test fixtures for the co-simulation importer (`sil.fmi`).

| Fixture | What it is for |
| --- | --- |
| `3.0/Feedthrough.fmu` | Every output equals its input, so a Run proves the Channel→variable mapping round-trips. |
| `3.0/BouncingBall.fmu` | A model with state and events, so a Run exercises a trajectory rather than a pass-through. |

## Provenance

- Source: <https://github.com/modelica/Reference-FMUs/releases/tag/v0.0.41>
- Release asset: `Reference-FMUs.zip`
- SHA-256 of the vendored files:
  - `cbb038007286be266707fb919ed4614ea3c5eba3c18941e3d50482a2b8e467c9  3.0/Feedthrough.fmu`
  - `f14f1f46c97c21bc208c139fa30d19ebce5b1d0fa0789f09a1ff1b9a6b9ee8c5  3.0/BouncingBall.fmu`

The bytes are pinned rather than downloaded at test time: "the same artifacts"
is what Determinism is scoped to, and a pinned fixture also keeps CI off the
network. One artifact carries `aarch64-darwin`, `x86_64-darwin`,
`aarch64-linux` and `x86_64-linux` binaries, so the CI runner and a developer
Mac read identical bytes and the suite is not platform-forked.

## License

The Reference FMUs are released under the 2-Clause BSD license, which permits
redistribution provided the copyright notice is retained. `LICENSE.txt` is the
notice as published, copied verbatim.
