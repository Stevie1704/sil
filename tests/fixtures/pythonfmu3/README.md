# PythonFMU3 regression archives

This is the authoritative FMU pair shared by the ordinary regression tests and
[`proofs/acc-fmi/evidence/`](../../../proofs/acc-fmi/evidence/). These are unchanged
archives from [native run 35705741334](https://github.com/Stevie1704/sil/actions/runs/35705741334),
source revision `09fab1eb618c873aa07b0f957f4f4b305c635b84`. Archive/source hashes,
exporter revision, runtime requirements and licenses are recorded beside each
archive. No exporter rebuild or CI artifact download is needed to test them.

Model source, shared dynamics, PythonFMU3 support modules and native bridge
sources are packaged in the archives. Model license: Apache-2.0
(`resources/LICENSE-SiL`); exporter: PythonFMU3 0.3.4, MIT
(`resources/LICENSE-PythonFMU3`), including upstream notices in its source files.
The host supplies the interpreter. The qualified machine class is Linux x86-64
with glibc and Python; these fixtures never enter the production image.

A baseline replacement must update this pair, its identities and the evidence
together. Earlier development archives remain available in Git history.
