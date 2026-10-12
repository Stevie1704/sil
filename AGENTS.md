# AGENTS.md

Guidance for coding agents working in this repository.

## Start here

- Bringing software into a Run as a Participant: `docs/agent-guide.md`. In this checkout `sil docs` has no packaged docs; read `docs/` directly.
- Deciding where a change goes: `docs/architecture.md`.

## Build and test

- `make venv`: create `.venv` and install the Python package with dev deps.
- `make build`: build the kernel (`sil-run`) into `build/`.
- `make test`: run the C++ unit tests and the full pytest suite.
- Determinism gate, as CI runs it after `make build`: `PYTHONPATH=python/src .venv/bin/python tools/determinism_gate.py`.

`make help` lists the other targets.

## Agent skills

### Issue tracker

Issues live as GitHub issues in `Stevie1704/sil`, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The default five-role vocabulary, with each label string equal to its role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
