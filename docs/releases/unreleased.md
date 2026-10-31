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

## Compatibility

| Surface | Status |
| --- | --- |
| Provenance side-car | Schema version 2. Each digest has `digest_state`, and the record has `provenance_mode`. `recording` is always an object. A reader of version 1 must change; see [the migration](../running.md#migrate-from-schema-version-1). |
| `sil_run_diagnostics` | Version 1, with the new `configuration.provenance_mode`. A report without it stays valid and reads as `full`. |
| `sil_benchmark_report` | Version 1, with the new `policy.provenance_mode`. A report without it stays valid and reads as `full`. |
| `sil_profile_report` | Version 1, with the new `configuration.provenance_mode`. A set without it stays valid and reads as `full`; null reads as `unknown`. `sil profile diff` warns when the modes differ. |
| Runner command line and exit codes | One new option. Existing use is unchanged. |
| Manifest document, Manifest hash, Recording contract | Unchanged |
