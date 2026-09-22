# Immutable PythonFMU3 regression archives

These are the original, unmodified archives from native qualification
[run 35703127743](https://github.com/Stevie1704/sil/actions/runs/35703127743),
source revision `4c1e01f5db68deafb20a44aeed7bc0ba4bfc8ade`. They match
`proofs/acc-fmi/evidence/*.identity.json`; archive and source SHA-256 values,
exporter revision, runtime and licenses are also recorded beside each archive.
They make that historical evidence independently hash-verifiable without
Actions artifact retention, and exercise resource-dependent FMUs in the normal
repository test suite without rebuilding or installing the exporter.

Model source, shared dynamics, PythonFMU3's Python support and native bridge
sources are packaged in the archives. Model license: Apache-2.0
(`resources/LICENSE-SiL`); exporter: PythonFMU3 0.3.4, MIT
(`resources/LICENSE-PythonFMU3`), including upstream notices in its source files.
The interpreter is supplied by the host, not embedded. These are qualified on
Linux x86-64 with glibc and Python, and are never copied to the production image.
Do not replace these when the proof is rebuilt: new builds have new identities.
