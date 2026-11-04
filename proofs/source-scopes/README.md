# Source-scope qualification (#372)

This is the Linux evidence for the API and capture lifecycle rows of the
[source-scope contract](../../docs/proposals/native-source-scopes.md)
(Q1–Q10, Q13–Q21). The workflow
[`proof-source-scopes.yml`](../../.github/workflows/proof-source-scopes.yml)
runs on GitHub Actions `ubuntu-24.04` with the pinned GCC 12.4.0
(`12.4.0-2ubuntu1~24.04.1`). It builds SiL with GCC 12, then:

1. runs the source-scope tests with the scenario participants
   `tests/fixtures/participants/scope_*` (Q2–Q10, Q13–Q19), with perf and an
   8 MiB tmpfs that the workflow prepares;
2. runs `proof.py` with the ADAS reference (Q1, Q20, Q21).

`proof.py` copies `examples/adas-reference` and adds one
`SIL_SOURCE_SCOPE` to each of the four functions of the #320 proof
(`check_object` 1, `confirmed` 2, `confirms` 3, `select_lead` 4), with the
supported header. The shipped example is unchanged. Expected values are
#320's worked example, not the decoder: (2, 1, 1, 1) and (16, 8, 36, 1)
calls per Activation for one and eight objects, in #320's order.

```sh
# On ubuntu-24.04, with the workflow's packages and the Python environment:
bash proofs/source-scopes/run-proof.sh "$PWD/build/scope-evidence" \
  "$PWD/build/scope-gcc12"
```

No check uses a time threshold. Elapsed host time and storage are retained
as observations only. See [evidence/README.md](evidence/README.md) for the
retained files and the row-by-row results.
