# SHA-256 backend: OpenSSL EVP, linked statically

Status: accepted (#325).

The kernel hashes Manifest bytes, artifact files and Recordings. Its minimal
software SHA-256 was written for Manifest bytes. It is slow on large files. We
use a maintained backend, and we do not write new cryptographic primitives.

## Decision

`kernel/src/sha256.cpp` uses OpenSSL 3 EVP. The kernel links `libcrypto`
statically. All hashing goes through `kernel/src/sha256.hpp`, and only that one
source file includes OpenSSL.

| Option | Advantage | Disadvantage |
| --- | --- | --- |
| Keep the minimal code | No dependency | No hardware acceleration |
| Shared `libcrypto` | Host package receives security updates; smaller runner | New runtime requirement for the native archive. The library enters each Native Participant's process, where its symbols can take the place of the OpenSSL that the Participant expects |
| Static `libcrypto` (chosen) | No new runtime requirement. Native Participants do not see the symbols | Runner is about 4.4 MB larger on arm64 macOS. An OpenSSL fix needs a new SiL build |

Security updates are less important here than for a usual OpenSSL user. The
kernel calls only the SHA-256 digest functions, with no TLS and no parser of
untrusted keys or certificates.

## Consequences

- **Algorithm and identities.** The algorithm does not change. Identical stable
  artifacts keep their digests, so stored identities need no migration. The
  digest of a rebuilt runner changes, as with each rebuild.
- **Build dependency.** Each build from source needs OpenSSL 3.0 or newer with
  its static library: `libssl-dev` on Debian and Ubuntu, `openssl@3` from
  Homebrew on a macOS development host. CMake stops at configuration if it
  cannot find them. Each Dockerfile, workflow and script that configures the
  kernel installs the package; `tests/test_sha256.py` checks this.
- **Runtime dependency.** None. The runtime image and the native archive need
  no OpenSSL library. `libssl-dev` stays in the build stages.
- **CPU support.** `libcrypto` selects its code path from the CPU features at
  run time. It uses SHA extensions or ARMv8 instructions only when the CPU has
  them, and otherwise uses its generic code. SiL adds no compiler tuning flag.
- **Host configuration.** The backend initializes with
  `OPENSSL_INIT_NO_LOAD_CONFIG`. A Run does not read `openssl.cnf` or
  `OPENSSL_CONF` to calculate a digest.
- **License.** OpenSSL 3 is Apache-2.0. See
  [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).
- **Failure.** A backend failure throws. Callers report it as they report a
  file that cannot be read, so no digest is recorded that was not calculated.

Measurements are in [docs/bench/sha256-backend.md](../bench/sha256-backend.md).
