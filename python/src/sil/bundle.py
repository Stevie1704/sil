"""`sil-bundle`: seal a regression bundle and run it offline (issue #200).

A regression bundle is an Acceptance bundle an adopter prepares for their
own targets: a shared library, one FMU, or several coupled FMUs. It is a
directory holding the authored Manifests, the targets, their resources, the
Recordings or the conversion inputs they came from, the prepared reference
Recordings, the comparison contracts, and one declaration, `bundle.json`,
that names every one of those files and states what the Runs need outside
the bundle.

A Manifest hash covers the Manifest's bytes only, and a Run's provenance
side-car covers only the files its commands name. Neither reaches a Python
module an adapter imports, a native library a target loads, or an
environment variable a participant reads. The declaration names those
explicitly, and `seal` records their identities:

    sil-bundle seal <bundle>
    sil-bundle verify <bundle>
    sil-bundle run <bundle> -o <evidence>

`seal` runs in the runtime that will execute the bundle. It digests every
bundle file, the runner and its build identity, and every declared
executable, Python module and file. It writes `bundle.lock.json` and prints
the digest of that lock. Nothing is prepared here: the targets, the
Manifests and the references are made beforehand.

`run` verifies every recorded identity before the first Run. When one
differs, it refuses the bundle (exit 2) and names every difference. With
`--expect-lock`, it also refuses a lock whose digest is not the one `seal`
printed, so a bundle that was changed and sealed again is refused too. `run`
then executes each declared Run in the declared environment only. It writes
the Recordings, provenance, logs, determinism checks, comparison reports and
a shareable `summary.json` into a separate evidence directory. It exits 1
when a verdict fails. The bundle is re-hashed after the Runs, so a Run that
wrote into it fails too.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sil.compare import ContractError, RecordingError, compare, read_contract

PROG = "sil-bundle"
FORMAT = 1
DECLARATION = "bundle.json"
LOCK = "bundle.lock.json"
SUMMARY = "summary.json"
ROLES = frozenset({
    "manifest", "target", "participant", "resource", "recording",
    "conversion-input", "receipt", "reference", "contract",
})
PASS, FAIL, REFUSED = "pass", "fail", "refused"
EXIT_FAIL = 1
EXIT_REFUSED = 2
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# The one variable a Run gets beyond the declared environment: an imported
# module must not leave bytecode behind in the immutable bundle.
_RUN_ENVIRONMENT = {"PYTHONDONTWRITEBYTECODE": "1"}
CLOSURE = (
    "Every declared artifact, executable, Python module and file matched its "
    "sealed digest, and the Runs saw only the declared environment. A "
    "Manifest hash covers the Manifest's bytes only; it does not close the "
    "modules, native libraries and environment a Run loads, which the lock "
    "names instead. Anything the declaration does not name is not verified."
)

# Run by the declared interpreter in the declared environment, so a module's
# identity is the one the Runs import. `-P` keeps the working directory off
# the import path.
_PROBE = r"""
import hashlib, importlib.util, json, pathlib, sys

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def tree(locations):
    h = hashlib.sha256()
    for location in locations:
        base = pathlib.Path(location)
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                h.update(f"{path.relative_to(base)}\0{digest(path)}\n".encode())
    return h.hexdigest()

def identity(name):
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        locations = sorted(spec.submodule_search_locations)
        return {"origin": locations, "sha256": tree(locations)}
    if spec.has_location:
        return {"origin": spec.origin, "sha256": digest(pathlib.Path(spec.origin))}
    return {"origin": spec.origin, "sha256": None}

print(json.dumps({"version": sys.version,
                  "modules": {name: identity(name) for name in sys.argv[1:]}}))
"""


class Refusal(Exception):
    """The bundle cannot be sealed or run as declared. No Run started."""


@dataclass(frozen=True)
class Comparison:
    name: str
    contract: str
    reference: str


@dataclass(frozen=True)
class RunSpec:
    name: str
    manifest: str
    participant_timeout_ms: int | None
    determinism: bool
    comparisons: tuple[Comparison, ...]


@dataclass(frozen=True)
class Declaration:
    name: str
    artifacts: dict[str, str]
    runner: str
    environment: dict[str, str]
    executables: tuple[str, ...]
    interpreter: str | None
    modules: tuple[str, ...]
    files: tuple[str, ...]
    excluded_executables: tuple[str, ...]
    excluded_modules: tuple[str, ...]
    runs: tuple[RunSpec, ...]


def read_declaration(root: Path) -> Declaration:
    path = root / DECLARATION
    try:
        doc = json.loads(path.read_bytes())
    except OSError as error:
        raise Refusal(f"cannot read the declaration {path}: {error.strerror}")
    except ValueError as error:
        raise Refusal(f"{path} is not JSON: {error}")
    doc = _object(doc, DECLARATION,
                  {"sil_bundle", "name", "artifacts", "runtime", "runs"},
                  {"dependencies", "excluded"})
    if doc["sil_bundle"] != FORMAT:
        raise Refusal(f"{DECLARATION}: sil_bundle must be {FORMAT}")
    artifacts = _artifacts(doc["artifacts"])
    runtime = _object(doc["runtime"], "runtime", {"runner", "environment"}, set())
    dependencies = _object(doc.get("dependencies", {}), "dependencies", set(),
                           {"executables", "python", "files"})
    python = _object(dependencies.get("python", {}), "dependencies.python",
                     set(), {"interpreter", "modules"})
    excluded = _object(doc.get("excluded", {}), "excluded", set(),
                       {"executables", "modules"})
    declaration = Declaration(
        name=_name(doc["name"], "name"),
        artifacts=artifacts,
        runner=_string(runtime["runner"], "runtime.runner"),
        environment=_environment(runtime["environment"]),
        executables=_strings(dependencies.get("executables", []),
                             "dependencies.executables"),
        interpreter=(_string(python["interpreter"], "dependencies.python.interpreter")
                     if "interpreter" in python else None),
        modules=_strings(python.get("modules", []), "dependencies.python.modules"),
        files=_files(dependencies.get("files", [])),
        excluded_executables=_strings(excluded.get("executables", []),
                                      "excluded.executables"),
        excluded_modules=_strings(excluded.get("modules", []), "excluded.modules"),
        runs=_runs(doc["runs"], artifacts),
    )
    if (declaration.modules or declaration.excluded_modules) and not declaration.interpreter:
        raise Refusal("dependencies.python.interpreter must name the interpreter "
                      "whose modules are declared or excluded")
    return declaration


def _object(value, context: str, required: set, optional: set) -> dict:
    if not isinstance(value, dict):
        raise Refusal(f"{context} must be an object")
    missing = sorted(required - value.keys())
    if missing:
        raise Refusal(f"{context} lacks {missing}")
    unknown = sorted(value.keys() - required - optional)
    if unknown:
        raise Refusal(f"{context} has unknown keys {unknown}")
    return value


def _string(value, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise Refusal(f"{context} must be a non-empty string")
    return value


def _name(value, context: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise Refusal(f"{context} {value!r} must be letters, digits, '.', '_' or '-'")
    return value


def _strings(value, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise Refusal(f"{context} must be a list")
    names = tuple(_string(item, context) for item in value)
    if len(set(names)) != len(names):
        raise Refusal(f"{context} names an entry twice")
    return names


def _files(value) -> tuple[str, ...]:
    files = _strings(value, "dependencies.files")
    for name in files:
        if not Path(name).is_absolute():
            raise Refusal(f"dependencies.files entry {name!r} must be an absolute path")
    return files


def _environment(value) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in value.items()
    ):
        raise Refusal("runtime.environment must map variable names to strings")
    return dict(value)


def _artifacts(value) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise Refusal("artifacts must list every bundle file with its role")
    for name, role in value.items():
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts or not parts:
            raise Refusal(f"artifact {name!r} must be a path inside the bundle")
        if name in (DECLARATION, LOCK):
            raise Refusal(f"artifact {name!r} is the bundle's own index")
        if role not in ROLES:
            raise Refusal(f"artifact {name!r} has role {role!r}, not one of {sorted(ROLES)}")
    return dict(value)


def _runs(value, artifacts: dict[str, str]) -> tuple[RunSpec, ...]:
    if not isinstance(value, list) or not value:
        raise Refusal("runs must list at least one Run")
    runs = tuple(_run(item, index, artifacts) for index, item in enumerate(value))
    names = [run.name for run in runs]
    if len(set(names)) != len(names):
        raise Refusal("runs names a Run twice")
    return runs


def _run(value, index: int, artifacts: dict[str, str]) -> RunSpec:
    context = f"runs[{index}]"
    doc = _object(value, context, {"name", "manifest"},
                  {"participant_timeout_ms", "determinism", "comparisons"})
    timeout = doc.get("participant_timeout_ms")
    if timeout is not None and (type(timeout) is not int or timeout <= 0):
        raise Refusal(f"{context}.participant_timeout_ms must be a positive integer")
    determinism = doc.get("determinism", True)
    if not isinstance(determinism, bool):
        raise Refusal(f"{context}.determinism must be true or false")
    comparisons = doc.get("comparisons", [])
    if not isinstance(comparisons, list):
        raise Refusal(f"{context}.comparisons must be a list")
    parsed = []
    for n, item in enumerate(comparisons):
        where = f"{context}.comparisons[{n}]"
        c = _object(item, where, {"name", "contract", "reference"}, set())
        parsed.append(Comparison(
            _name(c["name"], f"{where}.name"),
            _artifact(c["contract"], "contract", artifacts, f"{where}.contract"),
            _artifact(c["reference"], "reference", artifacts, f"{where}.reference"),
        ))
    if len({c.name for c in parsed}) != len(parsed):
        raise Refusal(f"{context}.comparisons names a comparison twice")
    return RunSpec(
        name=_name(doc["name"], f"{context}.name"),
        manifest=_artifact(doc["manifest"], "manifest", artifacts, f"{context}.manifest"),
        participant_timeout_ms=timeout,
        determinism=determinism,
        comparisons=tuple(parsed),
    )


def _artifact(value, role: str, artifacts: dict[str, str], context: str) -> str:
    """A reference must be a sealed artifact: it is prepared, never made here."""
    if artifacts.get(value) != role:
        raise Refusal(f"{context} {value!r} is not an artifact with role {role!r}")
    return value


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _contents(root: Path) -> dict[str, str]:
    """Every bundle file except the lock that records the digests."""
    contents = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise Refusal(f"bundle file {name} is a symbolic link; copy its target in")
        if path.is_file() and name != LOCK:
            contents[name] = file_sha256(path)
    return contents


def _which(name: str, environment: dict[str, str]) -> Path | None:
    """The path as found, unresolved: a venv's interpreter is a symbolic
    link whose target, run directly, is not the venv's interpreter."""
    found = shutil.which(name, path=environment.get("PATH", ""))
    return Path(found).absolute() if found else None


def _executable(name: str, environment: dict[str, str]) -> dict | None:
    path = _which(name, environment)
    return None if path is None else {"path": str(path), "sha256": file_sha256(path)}


def _build_info(runner: Path, environment: dict[str, str]) -> dict:
    try:
        proc = subprocess.run([str(runner), "--build-info"], env=environment,
                              capture_output=True, text=True)
        return json.loads(proc.stdout)
    except (OSError, ValueError) as error:
        raise Refusal(f"runner {runner} did not report its build identity: {error}")


def _python(interpreter: str, modules, environment: dict[str, str]) -> dict:
    path = _which(interpreter, environment)
    if path is None:
        return {"interpreter": None, "version": None,
                "modules": {name: None for name in modules}}
    try:
        with tempfile.TemporaryDirectory() as empty:
            proc = subprocess.run([str(path), "-P", "-c", _PROBE, *modules],
                                  env=environment, cwd=empty,
                                  capture_output=True, text=True)
    except OSError as error:
        raise Refusal(f"interpreter {path} cannot start: {error}")
    try:
        probe = json.loads(proc.stdout)
    except ValueError:
        raise Refusal(f"interpreter {path} could not report its modules: "
                      f"{proc.stderr.strip()}")
    return {"interpreter": {"path": str(path), "sha256": file_sha256(path)},
            "version": probe["version"], "modules": probe["modules"]}


def _file(name: str) -> dict | None:
    path = Path(name)
    return {"sha256": file_sha256(path)} if path.is_file() else None


def _dependencies(declaration: Declaration) -> dict:
    """The identities of everything the declaration names outside the bundle."""
    env = declaration.environment
    runner = _which(declaration.runner, env)
    return {
        "runner": None if runner is None else {
            "path": str(runner), "sha256": file_sha256(runner),
            "build_info": _build_info(runner, env),
        },
        "executables": {name: _executable(name, env) for name in declaration.executables},
        "python": (_python(declaration.interpreter, declaration.modules, env)
                   if declaration.interpreter else None),
        "files": {name: _file(name) for name in declaration.files},
    }


def _missing(dependencies: dict) -> list[str]:
    missing = []
    if dependencies["runner"] is None:
        missing.append("runner")
    missing += [f"executable {n}" for n, v in dependencies["executables"].items() if v is None]
    python = dependencies["python"]
    if python is not None:
        if python["interpreter"] is None:
            missing.append("Python interpreter")
        missing += [f"Python module {n}" for n, v in python["modules"].items() if v is None]
    missing += [f"file {n}" for n, v in dependencies["files"].items() if v is None]
    return missing


def _excluded(declaration: Declaration) -> list[str]:
    """No toolchain, exporter or independent importer may be reachable."""
    env = declaration.environment
    present = [f"executable {name} at {path}" for name in declaration.excluded_executables
               if (path := _which(name, env)) is not None]
    if declaration.excluded_modules:
        modules = _python(declaration.interpreter, declaration.excluded_modules, env)["modules"]
        present += [f"Python module {name}" for name, found in modules.items() if found]
    return present


def _undeclared_paths(root: Path, declaration: Declaration) -> list[str]:
    """Absolute paths a Manifest names that the bundle does not account for."""
    allowed = set(declaration.files)
    problems = []
    for run in declaration.runs:
        manifest = root / run.manifest
        try:
            doc = json.loads(manifest.read_bytes())
        except ValueError as error:
            raise Refusal(f"Manifest {run.manifest} is not JSON: {error}")
        for text in _strings_in(doc):
            if not text.startswith("/") or not Path(text).exists():
                continue
            path = Path(text).resolve()
            if not path.is_relative_to(root) and text not in allowed:
                problems.append(f"{run.manifest} names {text}")
    return problems


def _strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


def seal(root: Path) -> dict:
    """Record the identity of the bundle and of everything it declares."""
    root = root.resolve()
    if (root / LOCK).exists():
        raise Refusal(f"{root} is already sealed; a sealed bundle is immutable")
    declaration = read_declaration(root)
    contents = _contents(root)
    declared = set(declaration.artifacts) | {DECLARATION}
    problems = [f"artifact {n} is declared but missing" for n in sorted(declared - contents.keys())]
    problems += [f"file {n} is in the bundle but not declared"
                 for n in sorted(contents.keys() - declared)]
    if not problems:
        problems += _undeclared_paths(root, declaration)
    dependencies = _dependencies(declaration)
    problems += [f"missing dependency: {name}" for name in _missing(dependencies)]
    problems += [f"excluded and present: {name}" for name in _excluded(declaration)]
    if problems:
        raise Refusal("cannot seal the bundle:\n  " + "\n  ".join(problems))
    lock = {
        "sil_bundle_lock": FORMAT,
        "name": declaration.name,
        "root": str(root),
        "contents_sha256": contents,
        "dependencies": dependencies,
    }
    _write_json(root / LOCK, lock)
    return lock


def verify(root: Path, expected_lock: str | None = None) -> tuple[Declaration, dict]:
    """Re-derive every sealed identity; any difference refuses the bundle.

    The lock is inside the bundle, so it can show an accidental change but
    not a bundle that was changed and sealed again. `expected_lock`, the
    digest `seal` printed and kept apart from the bundle, closes that."""
    root = root.resolve()
    lock = _read_lock(root, expected_lock)
    if lock["root"] != str(root):
        raise Refusal(f"the bundle was sealed at {lock['root']}, and its Manifests "
                      f"name their artifacts there; it is at {root}")
    problems = _content_problems(lock["contents_sha256"], _contents(root))
    if problems:
        raise Refusal("the bundle differs from its seal:\n  " + "\n  ".join(problems))
    declaration = read_declaration(root)
    problems = _dependency_problems(lock["dependencies"], _dependencies(declaration))
    problems += [f"excluded and present: {name}" for name in _excluded(declaration)]
    if problems:
        raise Refusal("the runtime differs from the seal:\n  " + "\n  ".join(problems))
    return declaration, lock


def _read_lock(root: Path, expected: str | None) -> dict:
    path = root / LOCK
    try:
        data = path.read_bytes()
        lock = json.loads(data)
    except OSError:
        raise Refusal(f"{root} is not sealed: {LOCK} is missing; run `{PROG} seal`")
    except ValueError as error:
        raise Refusal(f"{LOCK} is not JSON: {error}")
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise Refusal(f"{LOCK} has digest {hashlib.sha256(data).hexdigest()}, "
                      f"not the expected {expected}")
    if not isinstance(lock, dict) or lock.get("sil_bundle_lock") != FORMAT or not (
        {"root", "contents_sha256", "dependencies"} <= lock.keys()
    ):
        raise Refusal(f"{LOCK} is not a sil_bundle_lock {FORMAT}")
    return lock


def _content_problems(sealed: dict, actual: dict) -> list[str]:
    problems = [f"missing artifact {n}" for n in sorted(sealed.keys() - actual.keys())]
    problems += [f"unsealed file {n}" for n in sorted(actual.keys() - sealed.keys())]
    problems += [f"altered artifact {n}" for n in sorted(sealed.keys() & actual.keys())
                 if sealed[n] != actual[n]]
    return problems


def _dependency_problems(sealed: dict, actual: dict) -> list[str]:
    problems = [f"missing dependency: {name}" for name in _missing(actual)]
    if problems:
        return problems
    for kind, name, was, now in _entries(sealed, actual):
        if was != now:
            problems.append(f"{kind} {name} differs from its seal: {was} -> {now}")
    return problems


def _entries(sealed: dict, actual: dict):
    yield "runner", "", sealed["runner"], actual["runner"]
    for name, identity in sealed["executables"].items():
        yield "executable", name, identity, actual["executables"].get(name)
    if sealed["python"] is not None:
        for key in ("interpreter", "version"):
            yield "Python", key, sealed["python"][key], actual["python"][key]
        for name, identity in sealed["python"]["modules"].items():
            yield "Python module", name, identity, actual["python"]["modules"].get(name)
    for name, identity in sealed["files"].items():
        yield "file", name, identity, actual["files"].get(name)


def run(root: Path, evidence: Path, expected_lock: str | None = None) -> dict:
    """Verify, execute every declared Run, and write the evidence."""
    root = root.resolve()
    evidence = _evidence_directory(root, evidence)
    # No absolute path: a consumer with a private bundle shares this file.
    summary = {"sil_bundle_evidence": FORMAT, "bundle": root.name}
    try:
        declaration, lock = verify(root, expected_lock)
    except Refusal as refusal:
        summary.update(verdict=REFUSED, refusal=str(refusal), runs=[])
        _write_json(evidence / SUMMARY, summary)
        raise
    runner = Path(lock["dependencies"]["runner"]["path"])
    environment = {**declaration.environment, **_RUN_ENVIRONMENT}
    results = [_execute(root, spec, runner, environment, evidence / "runs" / spec.name)
               for spec in declaration.runs]
    unchanged = not _content_problems(lock["contents_sha256"], _contents(root))
    passed = unchanged and all(r["verdict"] == PASS for r in results)
    summary.update(
        name=declaration.name,
        lock_sha256=file_sha256(root / LOCK),
        runtime=lock["dependencies"]["runner"]["build_info"],
        verdict=PASS if passed else FAIL,
        dependency_closure=CLOSURE,
        bundle_unchanged=unchanged,
        runs=results,
    )
    _write_json(evidence / SUMMARY, summary)
    return summary


def _evidence_directory(root: Path, evidence: Path) -> Path:
    evidence = evidence.resolve()
    if evidence.is_relative_to(root) or root.is_relative_to(evidence):
        raise Refusal(f"the evidence directory {evidence} must be outside the bundle")
    if evidence.exists() and (not evidence.is_dir() or any(evidence.iterdir())):
        raise Refusal(f"the evidence directory {evidence} must be new or empty")
    evidence.mkdir(parents=True, exist_ok=True)
    return evidence


def _execute(root: Path, spec: RunSpec, runner: Path, environment: dict,
             directory: Path) -> dict:
    directory.mkdir(parents=True)
    manifest = root / spec.manifest
    result = {
        "name": spec.name,
        "manifest": spec.manifest,
        "manifest_sha256": file_sha256(manifest),
    }
    first = _run_once(runner, manifest, spec, environment, directory, 1)
    result.update(exit_code=first["exit_code"], recording_sha256=first["recording_sha256"])
    if first["exit_code"] != 0:
        result.update(verdict=FAIL, determinism=None, comparisons={})
        return result
    passed = True
    if spec.determinism:
        second = _run_once(runner, manifest, spec, environment, directory, 2)
        hashes = [first["recording_sha256"], second["recording_sha256"]]
        deterministic = second["exit_code"] == 0 and hashes[0] == hashes[1]
        result["determinism"] = {"verdict": PASS if deterministic else FAIL,
                                 "second_exit_code": second["exit_code"],
                                 "recording_sha256": hashes}
        passed = deterministic
    else:
        result["determinism"] = None
    result["comparisons"] = {}
    for comparison in spec.comparisons:
        verdict = _compare(root, comparison, first["recording"], directory)
        result["comparisons"][comparison.name] = verdict
        passed = passed and verdict["verdict"] == PASS
    result["verdict"] = PASS if passed else FAIL
    return result


def _run_once(runner: Path, manifest: Path, spec: RunSpec, environment: dict,
              directory: Path, repeat: int) -> dict:
    recording = directory / f"run-{repeat}.mcap"
    command = [str(runner), str(manifest), "-o", str(recording),
               "--provenance", str(directory / f"run-{repeat}.provenance.json")]
    if spec.participant_timeout_ms is not None:
        command += ["--participant-timeout-ms", str(spec.participant_timeout_ms)]
    # The Run working directory is created beneath the invocation directory,
    # so it lives in the evidence directory and never in the bundle.
    log = directory / f"run-{repeat}.log"
    try:
        proc = subprocess.run(command, cwd=directory, env=environment,
                              capture_output=True, text=True, errors="replace")
    except OSError as error:
        log.write_text(f"{PROG}: cannot start {runner}: {error}\n")
        return {"exit_code": None, "recording": recording, "recording_sha256": None}
    log.write_text(proc.stdout + proc.stderr)
    return {
        "exit_code": proc.returncode,
        "recording": recording,
        "recording_sha256": file_sha256(recording) if recording.is_file() else None,
    }


def _compare(root: Path, comparison: Comparison, recording: Path,
             directory: Path) -> dict:
    report_path = directory / f"{comparison.name}.comparison.json"
    try:
        report = compare(read_contract(root / comparison.contract), recording,
                         root / comparison.reference)
    except (ContractError, RecordingError) as error:
        report_path.write_text(json.dumps({"error": str(error)}, indent=2) + "\n")
        return {"verdict": FAIL, "error": str(error)}
    _write_json(report_path, report)
    return {"verdict": report["verdict"], "divergences": report["divergences"],
            "coverage_problems": len(report["coverage"])}


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def render(summary: dict) -> str:
    lines = [f"bundle {summary.get('name', summary['bundle'])}: {summary['verdict']}"]
    for result in summary["runs"]:
        parts = [f"exit {result['exit_code']}"]
        if result["determinism"] is not None:
            parts.append(f"determinism {result['determinism']['verdict']}")
        parts += [f"{name} {verdict['verdict']}"
                  for name, verdict in result["comparisons"].items()]
        lines.append(f"  run {result['name']}: {result['verdict']} ({', '.join(parts)})")
    if summary.get("bundle_unchanged") is False:
        lines.append("  the bundle changed during the Runs")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Seal a regression bundle, or verify it and "
                               "run it offline into a separate evidence directory.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seal", help="record the identity of the bundle and its "
                                     "runtime").add_argument("bundle", type=Path)
    verify_parser = commands.add_parser("verify", help="check every sealed identity "
                                                       "without running")
    run_parser = commands.add_parser("run", help="verify, then execute every declared Run")
    for checking in (verify_parser, run_parser):
        checking.add_argument("bundle", type=Path)
        checking.add_argument("--expect-lock", metavar="SHA256",
                              help="the lock digest seal printed; refuse any other")
    run_parser.add_argument("-o", "--evidence", type=Path, required=True,
                            help="a new or empty directory outside the bundle")
    args = parser.parse_args(argv)
    try:
        if args.command == "seal":
            lock = seal(args.bundle)
            sys.stdout.write(f"sealed {lock['name']}: {len(lock['contents_sha256'])} "
                             f"artifacts at {lock['root']}\n"
                             f"lock sha256 {file_sha256(Path(lock['root']) / LOCK)}\n")
        elif args.command == "verify":
            declaration, _ = verify(args.bundle, args.expect_lock)
            sys.stdout.write(f"verified {declaration.name}\n")
        else:
            summary = run(args.bundle, args.evidence, args.expect_lock)
            sys.stdout.write(render(summary))
            return 0 if summary["verdict"] == PASS else EXIT_FAIL
    except Refusal as refusal:
        sys.stderr.write(f"{PROG}: refused: {refusal}\n")
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
