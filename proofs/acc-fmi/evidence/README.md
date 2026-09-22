# Native Linux qualification evidence

Produced by [Actions run 35703127743](https://github.com/Stevie1704/sil/actions/runs/35703127743)
on the native `ubuntu-latest` x86-64 runner, using implementation commit
`382de7b` and GitHub's merge snapshot
`4c1e01f5db68deafb20a44aeed7bc0ba4bfc8ade` (the source revision inside the
FMUs and runner). These are original outputs, not regenerated expectations.

- `results.json`: all three Manifests passed their numeric checks and their own
  two-Recording byte comparison; 20 output samples checked per Manifest.
- `*.fmpy.json`: two simultaneous FMI instances, initialization, pre-step and
  post-step values, communication points, FMI flags, and termination.
- `*.sil-initialization.json` and `*.sil.json`: SiL initialization and Run values.
- `*.mcap`, `*.mcap.provenance.json` and the three case Manifests: both complete
  Recordings per Run configuration, with their exact inputs and identities.
- `archives.json`, `*.identity.json`, `schema-identity.json`, `*.ldd.txt`,
  `environment.json`, `image-id.txt`: FMU/source/exporter/runtime/schema/image
  identification and actual capabilities/symbols/dependencies.
- `null-resource-path.log`: the external exporter regression with null resources;
  successful initialization is recorded beside it.
- `oracle-negative-tests.log`: rejection of wrong, missing, NaN and infinite data.

The two generated FMU archives are in the workflow's `acc-fmi-evidence` artifact;
their source and clean pinned build live one directory up. They are not vendored
here. Rebuilding from a different source revision gives new archive identities.
These retained traces are not overwritten by subsequent qualification Runs.
