#!/usr/bin/env python3
"""Run a release bundle without importing anything from the source checkout.

The wheel and native archive are installed into a temporary environment, then
the same representative ACC Run is executed twice. ``--check-symbols`` checks
numeric ELF imports against bookworm's symbol limits (requires binutils).
The optional container run uses the exact image reference supplied by the
workflow; a release job supplies an ``image@sha256:...`` reference rather
than a mutable tag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from pathlib import Path

from release import (
    REPOSITORY,
    ReleaseError,
    validate_image,
    validate_native_archive,
    validate_runner,
    validate_native_symbols,
)


class SmokeError(RuntimeError):
    pass


def run(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeError(f"could not run {' '.join(command)}: {exc}") from exc


def checked(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    result = run(command, cwd=cwd, env=env, timeout=timeout)
    if result.returncode:
        raise SmokeError(
            f"{' '.join(command)} failed with {result.returncode}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def clean_environment(python_bin: Path, archive_bin: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PATH"] = os.pathsep.join(
        [str(python_bin), str(archive_bin), environment.get("PATH", "")]
    )
    return environment


def python_metadata_check(version: str, revision: str) -> str:
    return (
        "import importlib.metadata as m; import sil; "
        f"assert m.version('sil') == {version!r}; "
        f"assert sil.__version__ == {version!r}; "
        "from sil.build_info import metadata; "
        f"assert metadata()['source_revision'] == {revision!r}"
    )


def extract_archive(
    archive_path: Path, destination: Path, expected_root: str
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise SmokeError("native archive is empty")
        root_path = (destination / expected_root).resolve()
        destination_resolved = destination.resolve()
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination_resolved and destination_resolved not in target.parents:
                raise SmokeError(f"native archive escapes its destination: {member.name}")
        archive.extractall(destination)
    return root_path


def assert_manifest_hash(
    result: subprocess.CompletedProcess[str],
    manifest: Path,
    expected_exit_code: int = 0,
) -> str:
    expected = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if result.returncode != expected_exit_code:
        raise SmokeError(
            f"representative Run returned {result.returncode}, "
            f"expected {expected_exit_code}: {result.stderr}"
        )
    if expected_exit_code != 0:
        return expected
    if result.stdout != f"manifest_hash {expected}\n":
        raise SmokeError(
            f"runner did not emit the expected Manifest hash: {result.stdout!r}"
        )
    return expected


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SmokeError(f"cannot read {path.name}: {exc}") from exc


def assert_run_report(path: Path) -> None:
    report = load_json(path)
    if report.get("sil_run_diagnostics") != 1:
        raise SmokeError(f"{path.name} is not a sil_run_diagnostics v1 report")
    if report["run"]["exit_code"] != 0 or not report["completeness"]["run_complete"]:
        raise SmokeError(f"{path.name} does not report a complete Run")


def assert_benchmark_report(path: Path, measured: int) -> None:
    summary = load_json(path)
    if summary.get("sil_benchmark_report") != 1:
        raise SmokeError(f"{path.name} is not a sil_benchmark_report v1 report")
    if not summary["status"]["complete"] or summary["counts"]["completed"] != measured:
        raise SmokeError(f"benchmark is incomplete: {summary['status']}")


def assert_offline_html(path: Path) -> None:
    try:
        page = path.read_text()
    except OSError as exc:
        raise SmokeError(f"cannot read HTML report {path.name}: {exc}") from exc
    if not page.lower().startswith("<!doctype html>"):
        raise SmokeError(f"{path.name} is not an HTML page")
    for reference in ("<script src", "<link rel=\"stylesheet\"", "@import"):
        if reference in page:
            raise SmokeError(f"{path.name} loads an external asset: {reference}")


def smoke_diagnostics(
    sil: list[str], workspace: Path, manifest: str, reference: Path,
    execute,
) -> None:
    """Run the parent spec's diagnostics examples with installed commands.

    `sil` is the command prefix and `manifest` the Manifest path as that
    command sees it; `execute(args)` returns a completed process. Every
    Recording written with diagnostics must equal the ordinary one.
    """
    def checked_sil(args: list[str], label: str):
        result = execute(sil + args)
        if result.returncode:
            raise SmokeError(
                f"{label} failed with {result.returncode}:\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result

    def inside(name: str) -> str:
        return manifest.rsplit("/", 1)[0] + "/" + name

    single = checked_sil(
        ["run", manifest, "--diagnostics", "-o", inside("diagnostics.mcap")],
        "sil run --diagnostics",
    )
    if not single.stdout.startswith("manifest_hash "):
        raise SmokeError(f"diagnostics Run lost the hash line: {single.stdout!r}")
    if (workspace / "diagnostics.mcap").read_bytes() != reference.read_bytes():
        raise SmokeError("diagnostics changed the Recording bytes")
    assert_run_report(workspace / "diagnostics.mcap.diagnostics.json")

    (workspace / "results").mkdir()
    checked_sil(
        ["run", manifest, "--no-recording", "--diagnostics",
         "--diagnostics-out", inside("results/run.json"),
         "--html", inside("results/run.html")],
        "sil run --no-recording --diagnostics-out --html",
    )
    assert_run_report(workspace / "results" / "run.json")
    assert_offline_html(workspace / "results" / "run.html")

    checked_sil(
        ["benchmark", manifest, "--repeats", "2", "--warmup", "1",
         "--out", inside("bench"), "--html", inside("results/report.html")],
        "sil benchmark",
    )
    assert_benchmark_report(workspace / "bench" / "summary.json", 2)
    assert_offline_html(workspace / "results" / "report.html")
    for label in ("warmup-001", "run-001", "run-002"):
        if (workspace / "bench" / label / "run.mcap").read_bytes() != reference.read_bytes():
            raise SmokeError(f"benchmark {label} changed the Recording bytes")

    # Rendering needs only the saved JSON: copy it away from its Run files.
    (workspace / "copied").mkdir()
    shutil.copyfile(workspace / "bench" / "summary.json",
                    workspace / "copied" / "summary.json")
    checked_sil(
        ["diagnostics", "render", inside("copied/summary.json"),
         "--html", inside("copied/report.html")],
        "sil diagnostics render",
    )
    assert_offline_html(workspace / "copied" / "report.html")


def smoke_native(
    bundle: Path, version: str, revision: str, workdir: Path,
    *, check_symbols: bool,
) -> tuple[Path, str]:
    bundle = bundle.resolve()
    wheels = sorted(bundle.glob(f"sil-{version}-*.whl"))
    archives = sorted(
        bundle.glob(f"sil-native-dev-{version}-linux-x86_64.tar.gz")
    )
    if len(wheels) != 1 or len(archives) != 1:
        raise SmokeError("bundle must contain exactly one wheel and native archive")

    archive_root = validate_native_archive(archives[0], version, revision)
    native_root = extract_archive(archives[0], workdir / "native", archive_root)
    if check_symbols:
        validate_native_symbols(native_root)
    venv_dir = workdir / "python"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python_bin = venv_dir / "bin"
    checked(
        [str(python_bin / "python"), "-m", "pip", "install", "--no-index",
         "--no-deps", str(wheels[0])],
        cwd=workdir,
        env=clean_environment(python_bin, native_root / "bin"),
    )
    environment = clean_environment(python_bin, native_root / "bin")
    for topic in ("agent-guide", "recording-queries", "examples"):
        checked([str(python_bin / "sil"), "docs", topic],
                cwd=workdir, env=environment)
    checked([str(python_bin / "sil"), "examples", "--dir", "examples"],
            cwd=workdir, env=environment)
    for relative in ("library/adapter.py", "library/binding.py", "library/ports.py",
                     "library/gap_binding.py", "bundle/prepare.py"):
        if not (workdir / "examples" / relative).is_file():
            raise SmokeError(f"missing exported example {relative}")
    checked(
        [str(python_bin / "python"), "-c", (
            python_metadata_check(version, revision)
        )],
        cwd=workdir,
        env=environment,
    )
    (workdir / "schema.json").write_text(
        json.dumps(
            {"smoke.Counter": {"fields": [{"name": "seq", "type": "u64"}]}},
            sort_keys=True,
        )
        + "\n"
    )
    checked(
        [str(native_root / "bin" / "silschema"), "schema.json", "generated.h"],
        cwd=workdir,
        env=environment,
    )
    (workdir / "consumer.c").write_text(
        "#include <sil/participant.h>\n"
        "int main(void) { return SIL_ABI_VERSION == 1 ? 0 : 1; }\n"
    )
    checked(
        [
            "cc",
            "-std=c11",
            f"-I{native_root / 'include'}",
            "-fsyntax-only",
            "consumer.c",
        ],
        cwd=workdir,
        env=environment,
    )

    acc_manifest = workdir / "acc.json"
    checked(
        [str(python_bin / "python"), "-m", "sil.examples.acc.manifest", str(acc_manifest)],
        cwd=workdir,
        env=environment,
    )
    runner = native_root / "bin" / "sil-run"
    validate_runner(runner, version, revision)
    first = workdir / "native-1.mcap"
    second = workdir / "native-2.mcap"
    first_result = run([str(runner), str(acc_manifest), "-o", str(first)], cwd=workdir, env=environment)
    manifest_hash = assert_manifest_hash(first_result, acc_manifest)
    second_result = run([str(runner), str(acc_manifest), "-o", str(second)], cwd=workdir, env=environment)
    assert_manifest_hash(second_result, acc_manifest)
    invalid_manifest = workdir / "invalid.json"
    invalid_document = json.loads(acc_manifest.read_text())
    invalid_document["sil_manifest"] = 99
    invalid_manifest.write_text(json.dumps(invalid_document, sort_keys=True) + "\n")
    invalid_result = run(
        [str(runner), str(invalid_manifest), "--no-recording"],
        cwd=workdir,
        env=environment,
    )
    assert_manifest_hash(invalid_result, invalid_manifest, expected_exit_code=2)
    if first.read_bytes() != second.read_bytes():
        raise SmokeError("two native release Runs produced different Recordings")
    check = checked(
        [str(python_bin / "sil"), "check", str(acc_manifest), "--runner", str(runner)],
        cwd=workdir,
        env=environment,
    )
    if not check.stdout.startswith("deterministic: "):
        raise SmokeError("sil check did not report a deterministic Recording")
    native_workspace = workdir / "native-diagnostics"
    native_workspace.mkdir()
    shutil.copyfile(acc_manifest, native_workspace / "acc.json")
    smoke_diagnostics(
        [str(python_bin / "sil")],
        native_workspace,
        str(native_workspace / "acc.json"),
        first,
        lambda command: run(command, cwd=native_workspace, env=environment,
                            timeout=300),
    )
    return acc_manifest, manifest_hash


def docker_run(
    image: str, workspace: Path, args: list[str], entrypoint: str, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker", "run", "--rm", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={workspace},dst=/workspace",
        "--workdir", "/workspace", "--entrypoint", entrypoint, image, *args,
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def smoke_container(
    image: str, version: str, revision: str, manifest: Path, manifest_hash: str
) -> None:
    validate_image(image, version, revision)
    version_report = docker_run(image, manifest.parent, ["--version"], "sil-run")
    if version_report.returncode or version_report.stdout.strip() != version:
        raise SmokeError(f"container runner version mismatch: {version_report.stdout!r}")
    info_report = docker_run(image, manifest.parent, ["--build-info"], "sil-run")
    if info_report.returncode:
        raise SmokeError(info_report.stderr)
    info = json.loads(info_report.stdout)
    if info != {
        "license": "Apache-2.0",
        "source_repository": REPOSITORY,
        "source_revision": revision,
        "version": version,
    }:
        raise SmokeError(f"container build metadata mismatch: {info!r}")
    package_report = docker_run(
        image,
        manifest.parent,
        [
            "-c",
            python_metadata_check(version, revision),
        ],
        "python3",
    )
    if package_report.returncode:
        raise SmokeError(f"container Python metadata mismatch: {package_report.stderr}")

    first = manifest.parent / "container-1.mcap"
    second = manifest.parent / "container-2.mcap"
    for output in (first, second):
        result = docker_run(
            image,
            manifest.parent,
            ["/workspace/acc.json", "-o", f"/workspace/{output.name}"],
            "sil-run",
        )
        if result.returncode or result.stdout != f"manifest_hash {manifest_hash}\n":
            raise SmokeError(f"container Run failed: {result.stdout}\n{result.stderr}")
    invalid_result = docker_run(
        image,
        manifest.parent,
        ["/workspace/invalid.json", "--no-recording"],
        "sil-run",
    )
    if invalid_result.returncode != 2:
        raise SmokeError(
            f"container Manifest error returned {invalid_result.returncode}, "
            f"expected 2: {invalid_result.stderr}"
        )
    if first.read_bytes() != second.read_bytes():
        raise SmokeError("two container release Runs produced different Recordings")
    check = docker_run(
        image,
        manifest.parent,
        ["check", "/workspace/acc.json", "--runner", "sil-run"],
        "sil",
    )
    if check.returncode or not check.stdout.startswith("deterministic: "):
        raise SmokeError(f"container sil check failed: {check.stdout}\n{check.stderr}")
    container_workspace = manifest.parent / "container-diagnostics"
    container_workspace.mkdir()
    shutil.copyfile(manifest, container_workspace / "acc.json")
    smoke_diagnostics(
        [],
        container_workspace,
        "/workspace/container-diagnostics/acc.json",
        first,
        lambda command: docker_run(image, manifest.parent, command, "sil",
                                   timeout=300),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image")
    mode.add_argument("--native-only", action="store_true",
                      help="test the native bundle without a runtime image")
    parser.add_argument("--check-symbols", action="store_true",
                        help="check bookworm's numeric ELF symbol limits (requires binutils objdump)")
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args(argv)

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="sil-release-smoke-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        manifest, manifest_hash = smoke_native(
            args.bundle, args.version, args.source_revision, workdir,
            check_symbols=args.check_symbols,
        )
        if args.image:
            smoke_container(args.image, args.version, args.source_revision, manifest, manifest_hash)
    except (
        OSError,
        ReleaseError,
        SmokeError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"release smoke: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f"release smoke passed: version {args.version}, Manifest hash {manifest_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
