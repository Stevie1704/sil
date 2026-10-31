# SHA-256 backend comparison

One observational comparison for #325 of the minimal software SHA-256 with the
OpenSSL EVP backend ([decision](../adr/0006-sha256-backend-static-openssl.md)).
It is evidence, not a threshold: no test asserts a duration from it. Raw results
are in [sha256-backend.json](sha256-backend.json).

**This machine was not quiet.** Other agents built and tested on it during the
measurement, and a Docker image build ran. The maximum values show this load.
[The qualification](provenance-qualification.md) repeats the measurement under less load
([raw](sha256-backend-329.json)).

**This machine is not the supported machine class.** SUPPORT.md names Linux
x86-64. There is no x86-64 result here, and an arm64 ratio does not predict an
x86-64 ratio, because the acceleration of each CPU is different.

## Procedure

```sh
.venv/bin/python tools/sha256_backend_measure.py --build-dir build \
  --old-runner OLD/sil-run --machine "<name>" --conditions "<conditions>" \
  --json docs/bench/sha256-backend.json
```

`OLD/sil-run` is a Release build of the revision before the change (`600fa42`).

## Machine and conditions

The JSON file has the machine fields. Build: CMake `Release`, OpenSSL 3.6.2 from
Homebrew, macOS, arm64.

| Part | Storage and cache conditions |
| --- | --- |
| File hashing | Each file is in memory before the timer starts, so storage and page cache have no effect. Each digest is checked against hashlib. 15 alternating repeats |
| Runs | Recordings go to the system temporary directory on the local SSD. The page cache is warm, because the runner reads the Recording directly after it writes it. 5 alternating pairs. Both runners wrote identical Recording bytes |

## File hashing, CPU time

| File | Minimal: min / median / max (ms) | EVP: min / median / max (ms) |
| --- | --- | --- |
| 1 MiB | 4.97 / 5.29 / 5.36 | 1.01 / 1.10 / 1.28 |
| 16 MiB | 80.13 / 84.01 / 86.13 | 7.62 / 8.33 / 9.49 |
| 256 MiB | 1342.09 / 1385.15 / 1434.90 | 127.13 / 136.90 / 163.64 |

## Runs, wall time of the whole process

These workloads are from `tools/diagnostics_overhead.py`. They are not the
#324 baseline workloads, which did not exist at this measurement. The new
runner is about 4.4 MB larger, and its own digest is part of each Run.

| Workload | Recording | Old: min / median / max (ms) | New: min / median / max (ms) |
| --- | --- | --- | --- |
| native-multitask-100s | 7.6 MB | 83.6 / 84.0 / 299.0 | 47.4 / 49.7 / 56.7 |
| native-multitask-1000s | 75.6 MB | 695.4 / 801.8 / 896.2 | 359.9 / 414.8 / 1087.1 |
| native-and-process-60s | 0.9 MB | 662.5 / 837.1 / 11661.6 | 538.8 / 824.8 / 3955.3 |

With 5 pairs under this load, the third workload shows no difference that this
measurement can resolve. Its large maximums are from the load.
