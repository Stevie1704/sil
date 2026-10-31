# SiL: unreleased

Move this text into the notes of the next release.

## Provenance mode

`--provenance-mode full|external` selects who owns artifact identity. The
default is `full`, which keeps the existing hashing. In `external` mode, the
runner omits the digests of its inputs and of the Recording. It keeps the
Manifest hash, path and file checks, topology checks and Replay verification.
Recording bytes do not change. `sil-run`, `sil run`, `sil benchmark` and
`sil profile` accept the option. See
[docs/running.md](../running.md#provenance-side-car).

The platform that uses `external` must bind the actual runner and inputs to its
execution record and hash the finalized Recording. The Manifest hash does not
identify the contents of the files that a Manifest names.

The Manifest hash is the SHA-256 of the exact file bytes. Replay verification
stays on in both modes. `sil check`, `sil bundle`, comparisons and the artifact
digests of a profile directory keep their hashes. `sil profile` in `external`
mode still hashes a Recording in its directory, as profile evidence.
[`examples/managed-platform/`](../running.md#managed-platform-example) shows the
duties of the platform.

## Less hash work in `full` mode

| Change | Effect | Dependency or limit |
| --- | --- | --- |
| SHA-256 uses OpenSSL EVP | Digests stay the same. Hardware acceleration is used where the CPU has it. | A build from source needs OpenSSL 3.0 or newer with its static library. A built runner has no OpenSSL runtime dependency ([decision](../adr/0006-sha256-backend-static-openssl.md)). |
| One digest for each canonical input path in one preparation | Participants that share an input cause one hash. | The reuse is for one preparation only. A later Run sees replaced bytes. |
| The Recording digest is calculated while the Run writes | A normal Run does not read the Recording again. | The same CPU work occurs during the Run, so the reported Run wall time now includes it. `full` mode has no measured end-to-end gain from this change. A failed write can cause one read of the file. |
| A profile command builds its artifact list once | Each finalized artifact has one hash. | The profile tool does not reuse the digest of the runner ([why](../profile-report.md#no-recording-digest-handoff)). |

[The qualification](../bench/provenance-qualification.md) gives the measured
results and limitations. The retained results are for one arm64 macOS machine
unless the report states otherwise. They are not results for the supported
machine class, and they do not promise a speedup on your workload.

## Compatibility

| Surface | Status |
| --- | --- |
| Provenance side-car | Schema version 2. Each digest has `digest_state`, and the record has `provenance_mode`. `recording` is always an object. A reader of version 1 must change; see [the migration](../running.md#migrate-from-schema-version-1). |
| `sil_run_diagnostics` | Version 1, with the new `configuration.provenance_mode`. A report without it stays valid and reads as `full`. |
| `sil_benchmark_report` | Version 1, with the new `policy.provenance_mode`. A report without it stays valid and reads as `full`. |
| `sil_profile_report` | Version 1, with the new `configuration.provenance_mode`. A set without it stays valid and reads as `full`; null reads as `unknown`. `sil profile diff` warns when the modes differ. |
| Runner command line and exit codes | One new option. Existing use is unchanged. |
| Manifest document, Manifest hash, Recording contract | Unchanged |
