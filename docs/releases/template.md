# SiL @VERSION@

Source revision: `@SOURCE_REVISION@`

Runtime image: `@IMAGE_REFERENCE@` (OCI index `@IMAGE_DIGEST@`)

## Machine classes

This release supports two Linux machine classes. Each has glibc, Debian
bookworm level or newer.

| Machine class | Native archive | Image platform |
| --- | --- | --- |
| Linux x86-64 | `sil-native-dev-@VERSION@-linux-x86_64.tar.gz` | `linux/amd64` |
| Linux arm64 | `sil-native-dev-@VERSION@-linux-aarch64.tar.gz` | `linux/arm64` |

A Recording is bit-identical within a machine class. It is not bit-identical
across the two classes in general. See
[SUPPORT.md](../../SUPPORT.md#across-machine-classes) for the causes.

## Runtime image

The runtime image is a multi-arch OCI index. `docker pull` of the index
reference selects the image of the host platform, so an arm64 host, such as a
Mac with Apple silicon, runs the arm64 image natively, with no `--platform`
flag.

| Platform | Image digest |
| --- | --- |
| `linux/amd64` | `@IMAGE_DIGEST_LINUX_AMD64@` |
| `linux/arm64` | `@IMAGE_DIGEST_LINUX_ARM64@` |

A deterministic Run pins one platform digest, for example
`ghcr.io/stevie1704/sil@<platform digest>`, not the index digest. The two
platforms are separate machine classes: SiL does not promise that a Recording
made with one image equals a Recording made with the other.

The release metadata above identifies the per-release bundle. Each Run emits
its own provenance side-car with the Manifest hash and the exact artifact
digests observed by that Run; release notes should reference that contract
without duplicating per-Run records.
