# Contributing

## The terms a contribution is accepted under

SiL is licensed under the Apache License 2.0. Contributions come in under the
same license.

This is Section 5 of the license itself:

> Unless You explicitly state otherwise, any Contribution intentionally
> submitted for inclusion in the Work by You to the Licensor shall be under the
> terms and conditions of this License, without any additional terms or
> conditions.

So:

- You keep the copyright in what you write. Nothing is assigned to anyone.
- You license your contribution under Apache-2.0, including its patent grant,
  to the project and to everyone who receives the project.
- There is no separate contributor license agreement to sign.

When you open a pull request you confirm that you wrote the contribution, or
that you have the right to submit it under Apache-2.0. If your employer holds
rights in your work, get their permission before you submit.

Do not paste code you found elsewhere unless its license permits redistribution
under Apache-2.0. Name the source and its license in the pull request when you
do. A new third-party dependency must be permissive and must be added to
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) in the same pull request.

Do not add a per-file copyright header. The root [LICENSE](LICENSE) and
[NOTICE](NOTICE) cover the whole repository.

## Before you write code

Work starts from a [GitHub issue](https://github.com/Stevie1704/sil/issues).
The issue states what to build and the acceptance criteria that decide when it
is done. Open one and agree on the shape before you write the change; that is
cheaper than rewriting the pull request.

Read [CONTEXT.md](CONTEXT.md) for the vocabulary and [DESIGN.md](DESIGN.md) for
the decisions already taken. Use the terms as `CONTEXT.md` defines them.
[SUPPORT.md](SUPPORT.md) says which interfaces are compatibility surfaces; a
change to one of them belongs in the pull request description.

## Before you open a pull request

Build and run the full suite, as [README.md](README.md) describes:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
ctest --test-dir build --output-on-failure
uv venv .venv && uv pip install -p .venv/bin/python -e "python/[dev]"
.venv/bin/python -m pytest tests/
```

A behavior change needs a test at the run boundary. The determinism gate in
continuous integration runs every reference Manifest twice and bit-compares the
Recordings; a change that breaks it is a defect, not a flaky test.

## Reporting

- A defect or a feature request: a [GitHub issue](https://github.com/Stevie1704/sil/issues).
- A vulnerability: never an issue. Follow [SECURITY.md](SECURITY.md).
