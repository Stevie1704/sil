#!/usr/bin/env python3
"""Run a release bundle without importing anything from the source checkout.

The wheel and native archive are installed into a temporary environment, then
the same representative ACC Run is executed twice.  The optional container
run uses the exact image reference supplied by the workflow; a release job
supplies an ``image@sha256:...`` reference rather than a mutable tag.
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


def extract_archive(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise SmokeError("native archive is empty")
        roots = {member.name.split("/", 1)[0] for member in members}
        if len(roots) != 1:
            raise SmokeError("native archive must have one root directory")
        root = next(iter(roots))
        root_path = (destination / root).resolve()
        destination_resolved = destination.resolve()
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination_resolved and destination_resolved not in target.parents:
                raise SmokeError(f"native archive escapes its destination: {member.name}")
        archive.extractall(destination)
    return root_path


def assert_manifest_hash(result: subprocess.CompletedProcess[str], manifest: Path) -> str:
    expected = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if result.returncode != 0:
        raise SmokeError(f"representative Run failed: {result.stderr}")
    if result.stdout != f"manifest_hash {expected}\n":
        raise SmokeError(
            f"runner did not emit the expected Manifest hash: {result.stdout!r}"
        )
    return expected


def smoke_native(
    bundle: Path, version: str, revision: str, workdir: Path
) -> tuple[Path, str]:
    bundle = bundle.resolve()
    wheels = sorted(bundle.glob(f"sil-{version}-*.whl"))
    archives = sorted(bundle.glob(f"sil-native-dev-{version}-*.tar.gz"))
    if len(wheels) != 1 or len(archives) != 1:
        raise SmokeError("bundle must contain exactly one wheel and native archive")

    native_root = extract_archive(archives[0], workdir / "native")
    validate_native_archive(archives[0], version, revision)
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
    checked(
        [str(python_bin / "python"), "-c", (
            "import importlib.metadata as m; import sil; "
            f"assert m.version('sil') == {version!r}; "
            f"assert sil.__version__ == {version!r}; "
            "from sil.build_info import metadata; "
            f"assert metadata()['source_revision'] == {revision!r}"
        )],
        cwd=workdir,
        env=environment,
    )
    (workdir / "schema.json").write_text(
        json.dumps({"smoke.Empty": {"fields": []}}, sort_keys=True) + "\n"
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
        [str(python_bin / "sil-acc"), str(acc_manifest)],
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
    if first.read_bytes() != second.read_bytes():
        raise SmokeError("two native release Runs produced different Recordings")
    check = checked(
        [str(python_bin / "sil-check"), str(acc_manifest), "--runner", str(runner)],
        cwd=workdir,
        env=environment,
    )
    if not check.stdout.startswith("deterministic: "):
        raise SmokeError("sil-check did not report a deterministic Recording")
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
            (
                "import importlib.metadata as m; import sil; "
                f"assert m.version('sil') == {version!r}; "
                f"assert sil.__version__ == {version!r}; "
                "from sil.build_info import metadata; "
                f"assert metadata()['source_revision'] == {revision!r}"
            ),
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
    if first.read_bytes() != second.read_bytes():
        raise SmokeError("two container release Runs produced different Recordings")
    check = docker_run(
        image,
        manifest.parent,
        ["/workspace/acc.json", "--runner", "sil-run"],
        "sil-check",
    )
    if check.returncode or not check.stdout.startswith("deterministic: "):
        raise SmokeError(f"container sil-check failed: {check.stdout}\n{check.stderr}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args(argv)

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="sil-release-smoke-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        manifest, manifest_hash = smoke_native(
            args.bundle, args.version, args.source_revision, workdir
        )
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
