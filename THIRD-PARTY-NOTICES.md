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

esmini is the subject of the consumer adoption proof in
[proofs/esmini/](proofs/esmini/), not a dependency of SiL. No esmini file is in
this repository, nothing here links against it, and none of it reaches a
published SiL artifact — which is why it sits outside the permissive inventory
above rather than in it. Its binaries are redistributed unmodified, which is
what its weak-copyleft grant asks for. The same image build installs five
Debian X and OpenGL packages esmini links against, under their own terms and
recorded where Debian records them, in the image's `/usr/share/doc/*/copyright`.

## Python wheel and source distribution

The wheel and the source distribution contain SiL code only. They declare
`mcap` as a runtime dependency and bundle no third-party code, so they carry no
third-party notice beyond this file.
