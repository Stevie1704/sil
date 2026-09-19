# Publishing a SiL release

The release version is declared once, as `project.version` in
`python/pyproject.toml`. A release tag must be exactly `vMAJOR.MINOR.PATCH`
with no prefix, suffix, or leading zero component. The tag is the only event
that can publish artifacts; pull requests run the same builds and smoke tests
with publishing disabled.

## Prepare and publish

1. Bump `project.version` and add `docs/releases/v<VERSION>.md`. Copy the
   previous note and state the machine class, determinism boundary, Native ABI,
   Manifest, Step protocol, Recording, and Python API compatibility. List
   changes to Manifest bytes, Manifest hashes, Run behavior, exit-code
   classification, and Recording bytes explicitly; write `none` when there is
   no change.
2. Open a pull request. The release workflow builds the wheel and source
   distribution, stages and archives the Linux native development prefix,
   builds the pinned runtime image, and runs the clean-environment smoke job.
3. After merge, create and push the matching annotated tag:

   ```sh
   git tag -a v<VERSION> -m "SiL <VERSION>" <commit>
   git push origin v<VERSION>
   ```

   Configure the `release` GitHub environment with required maintainer review.
   The publish job has only `contents: write` and `packages: write`; it checks
   that the tag, Python metadata, runner report, OCI version label, and release
   title all equal `<VERSION>` before it attaches anything.
4. After the job succeeds, download every release asset and verify it:

   ```sh
   sha256sum -c SHA256SUMS
   docker pull ghcr.io/stevie1704/sil@sha256:<digest>
   ```

   Use the digest, not only `ghcr.io/stevie1704/sil:<VERSION>`, in a
   deterministic CI Run. The release notes contain the exact digest and source
   revision used by the bundle.

## Recovery without replacement

Publishing is intentionally fail-closed. A malformed tag, a version mismatch,
an existing GitHub release, or an existing registry version tag stops before
the version can be replaced. Do not delete and recreate a released tag, image,
or GitHub release.

If a job fails after the image was pushed, keep that image digest and inspect
the failed release with `gh release view v<VERSION>`. If the GitHub release was
created with missing assets, upload only the missing files with
`gh release upload v<VERSION> <file> --clobber=false`, then rerun the checksum
and title checks. If the release was not created, rerun the publish job only
after confirming that the image digest is the one recorded by the workflow;
the job must refuse an existing version tag rather than rebuild over it. A
partial release is repaired by adding missing assets, never by overwriting a
published artifact.
