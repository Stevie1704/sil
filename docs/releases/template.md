# SiL @VERSION@

Source revision: `@SOURCE_REVISION@`

Runtime image: `@IMAGE_REFERENCE@` (`@IMAGE_DIGEST@`)

The release metadata above identifies the per-release bundle. Each Run emits
its own provenance side-car with the Manifest hash and the exact artifact
digests observed by that Run; release notes should reference that contract
without duplicating per-Run records.
