# Complete native snapshot after review fixes

Produced by [native Linux run 35705741334](https://github.com/Stevie1704/sil/actions/runs/35705741334)
from implementation commit `7561af5`, with GitHub merge snapshot
`09fab1eb618c873aa07b0f957f4f4b305c635b84` recorded as the source revision.
Every file in the workflow's `acc-fmi-evidence` artifact is retained here,
including the two FMUs and zero-byte successful-command logs. This README is
an index added after download; the original outputs have not been rewritten.

- `*.fmu` and `*.identity.json`: exact archives, source/exporter identities,
  runtime requirements and licenses. Their SHA-256 values can be recomputed
  directly without an expiring Actions artifact.
- `rebuild.json`: both clean exporter invocation hashes for each FMU, checked
  with a byte comparison. `rebuild.log` is empty because successful builds are
  silent, not because rebuilding was skipped.
- `results.json`: both Recording filenames, SHA-256 hashes and successful exit
  codes per Manifest. Both `.mcap` files and provenance are retained.
- `*.sil-lifecycle.json`: initialized values, stepped states, and successful
  `close()`/FMI termination at 1.0 s for both instances. `*.sil.json` separately
  retains the kernel-driven Run's post-step outputs.
- `*.fmpy.json`: independent initialization, pre-step values, inputs, post-step
  values and final termination.
- `null-resource-initialization.json` and `null-resource-path.log`: explicit
  expected instantiation rejection when the resource path is omitted.
- `gate-tests.log`: negative-gate and XML-preservation tests, including rejection
  with `python -O`, missing/duplicate output and an absent optional date.
- `archives.json`, `schema-identity.json`, `*.ldd.txt`, `environment.json` and
  `image-id.txt`: schema validation, symbol/capability audit and runtime identity.

The original snapshot remains in `../evidence/`. Its two FMUs are retained
unchanged under `tests/fixtures/pythonfmu3/` for ordinary-suite regression tests.
New builds have new source-bound identities; they replace neither archive set.
