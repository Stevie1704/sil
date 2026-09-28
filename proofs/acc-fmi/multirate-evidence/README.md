# Retained ACC multi-rate evidence

`report.json` is the unedited machine-readable report from the installed
Linux x86-64 proof in [GitHub Actions run 36404579936](https://github.com/Stevie1704/sil/actions/runs/36404579936). It records the exact source revision,
image and FMU identities, the declared envelope, native Sample time
comparisons, repeated Recording hashes, and the first differing signal and
time for each negative control.

The run's `acc-multirate-evidence` artifact retains the complete pinned
bundle, including FMUs, independent FMPy runs and expected delivery
schedules, plus all authored Manifests, Recordings and logs. The report's
`bundle_sha256` names that artifact's `bundle.json`; the bundle index lists a
SHA-256 digest for every input file. The retained report covers only the
model and profile combinations named in its `scope` and `results` fields.

The report from the first run
([36399755835](https://github.com/Stevie1704/sil/actions/runs/36399755835))
used the earlier Row set and envelope. It was replaced because it does not
match the current contract. It remains in the Git history.
