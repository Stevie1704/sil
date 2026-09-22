# Third-party notices

SiL itself is licensed under Apache-2.0 (see [LICENSE](LICENSE) and
[NOTICE](NOTICE)). This file inventories every third-party component that
enters a published SiL artifact, names its license, and says where its license
text is reproduced. All of them are permissive and impose no condition on a
Participant that runs under SiL.

## Compiled into the native binaries

These are header-only libraries compiled into `sil-run`. They are present in
the container image and in the native-development archive.

| Component | Version | License | Text in a published artifact |
| --- | --- | --- | --- |
| [nlohmann/json](https://github.com/nlohmann/json) | 3.12.0 | MIT | `/usr/share/licenses/sil/nlohmann-json/LICENSE.MIT` (image), `share/licenses/sil/nlohmann-json/LICENSE.MIT` (staged prefix) |
| [MCAP C++](https://github.com/foxglove/mcap) | 2.1.3 | MIT | `/usr/share/licenses/sil/mcap/LICENSE` (image), `share/licenses/sil/mcap/LICENSE` (staged prefix) |

The kernel builds MCAP with `MCAP_COMPRESSION_NO_LZ4` and
`MCAP_COMPRESSION_NO_ZSTD`, so no compression library is linked into the
native binaries.

## Installed in the container Python environment

| Component | Version | License | Text in the image |
| --- | --- | --- | --- |
| [mcap](https://pypi.org/project/mcap/) | 1.4.0 | MIT | `/usr/share/licenses/sil/mcap/LICENSE` |
| [lz4](https://pypi.org/project/lz4/) | 4.4.5 | BSD-3-Clause | `/usr/share/licenses/sil/lz4/LICENSE` |
| [zstandard](https://pypi.org/project/zstandard/) | 0.25.0 | BSD-3-Clause | `/usr/share/licenses/sil/zstandard/LICENSE` |

The `mcap` Python package and the MCAP C++ library come from the same
repository under the same MIT grant, so one copy of that text covers both.

The exact versions are pinned in
[container/requirements.lock](container/requirements.lock). The build backend
in [container/build-requirements.lock](container/build-requirements.lock) runs
only while the wheel is produced; none of it reaches a published artifact.

## Container base image

The runtime image derives from `python:3.13.7-slim-bookworm`, pinned by digest
in the [Dockerfile](Dockerfile). It contributes CPython under the PSF license
and Debian packages under their own terms. Those layers already carry their
copyright files under `/usr/share/doc/*/copyright` in the image, which is where
Debian records them.

## Reference artifacts, not in the production runtime

| Component | Version | License | Where |
| --- | --- | --- | --- |
| [Modelica Reference FMUs](https://github.com/modelica/Reference-FMUs) | 3.0 | BSD-2-Clause | `tests/fixtures/reference-fmus/LICENSE.txt`; copied into the `acceptance` image target only |

The `acceptance` image target exists for this repository's own checks. The
production `runtime` target contains no test fixture and no FMU supplied by
this repository.

## Downloaded by a proof, never shipped

| Component | Version | License | Where |
| --- | --- | --- | --- |
| [esmini](https://github.com/esmini/esmini) | v3.8.1 | MPL-2.0 | downloaded by [proofs/esmini/Dockerfile](proofs/esmini/Dockerfile) into a locally built consumer image; the upstream `LICENSE` is kept at `/opt/esmini/LICENSE` there |
| [fmi-ls-bus-examples](https://github.com/modelica/fmi-ls-bus-examples) | revisions `cc42cacd` (the fixture) and `de019a6e` (the revision it rejected, built beside it) | BSD-2-Clause | built by [proofs/fmi-ls-bus/build-fixture.sh](proofs/fmi-ls-bus/build-fixture.sh) into a locally built fixture image; upstream's own packaging script keeps `LICENSE.txt` inside each built FMU at `documentation/licenses/LICENSE.txt` |
| [fmi-ls-bus](https://github.com/modelica/fmi-ls-bus) headers | revision `468127f2` | BSD-2-Clause | fetched by upstream's packaging script into each built FMU's `sources/`, where each header carries the license text in its own comment block |
| [FMPy](https://github.com/CATIA-Systems/FMPy) | 0.3.32 | BSD-2-Clause | installed into the fixture image; it compiles the source-code FMUs and supplies the FMI 3.0 bindings the reference exchange is driven through |

The FMI-LS-BUS CAN demo FMUs are the subject of the acceptance fixture in
[proofs/fmi-ls-bus/](proofs/fmi-ls-bus/). No upstream file is in this
repository: the fixture is built from pinned revisions inside a locally built
image, and none of it reaches a published SiL artifact. All three grants are
permissive and allow that redistribution with the notice retained, which is
what the built FMUs and the image do.

esmini is the subject of the consumer adoption proof in
[proofs/esmini/](proofs/esmini/), not a dependency of SiL. No esmini file is in
this repository, nothing here links against it, and none of it reaches a
published SiL artifact — which is why it sits outside the permissive inventory
above rather than in it. Its binaries are redistributed unmodified, which is
what its weak-copyleft grant asks for. The same image build installs five
Debian X and OpenGL packages esmini links against, under their own terms and
recorded where Debian records them, in the image's `/usr/share/doc/*/copyright`.

## ACC FMU proof tools

[proofs/acc-fmi/](proofs/acc-fmi/) downloads PythonFMU3 **0.3.4**
([source](https://github.com/StephenSmith25/PythonFMU3/tree/0.3.4), MIT) and
FMPy **0.3.26** ([source](https://github.com/CATIA-Systems/FMPy/tree/v0.3.26),
BSD-2-Clause) into a locally built qualification image. PythonFMU3 supplies the
FMI implementation and is bundled by its own exporter into the generated FMUs;
its release license is retained in each FMU as `resources/LICENSE-PythonFMU3`
and in `proofs/acc-fmi/pythonfmu3-LICENSE`. FMPy supplies an independent call
path and the FMI schema tree (BSD-2-Clause, with notices in the schema files).
One qualification FMU pair is retained under `tests/fixtures/pythonfmu3/`,
shared by regression tests and the proof evidence. The archives include the
exporter implementation and its upstream notices. Neither tool becomes a SiL runtime dependency or enters the
published production runtime.
The proof image pins CPython 3.13.7 (PSF license) and all Python dependencies;
their installed distribution metadata and Debian copyright files retain their
respective notices. The ACC model source is covered by this repository’s
Apache-2.0 license, included in each FMU as `resources/LICENSE-SiL`.

## Python wheel and source distribution

The wheel and the source distribution contain SiL code only. They declare
`mcap` as a runtime dependency and bundle no third-party code, so they carry no
third-party notice beyond this file.
