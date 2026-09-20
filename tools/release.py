#!/usr/bin/env python3
"""Build, validate, and package the artifacts of a SiL release.

This module deliberately uses only the Python standard library.  It is used
from GitHub Actions before the wheel (and its dependencies) are installed, and
from the Docker build while the source checkout is still only build input.
The version in ``python/pyproject.toml`` is the one release source of truth.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email import policy
from email.parser import Parser
from pathlib import Path

REPOSITORY = "https://github.com/Stevie1704/sil"
LICENSE = "Apache-2.0"
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
TAG_PATTERN = re.compile(r"v(" + VERSION_PATTERN.pattern[:-2] + r")\Z")


class ReleaseError(RuntimeError):
    """A release input is malformed or an artifact is incomplete."""


def fail(message: str) -> ReleaseError:
    return ReleaseError(message)


def project_version(root: Path) -> str:
    """Read and validate the single version declared by the Python project."""
    pyproject = root / "python" / "pyproject.toml"
    try:
        document = tomllib.loads(pyproject.read_text())
        version = document["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise fail(f"cannot read project version from {pyproject}: {exc}") from exc
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise fail(f"project version is not a release semantic version: {version!r}")
    return version


def checked_version(root: Path, requested: str | None) -> str:
    version = project_version(root)
    if requested is not None and requested != version:
        raise fail(
            f"requested version {requested!r} does not match project version {version!r}"
        )
    return version


def tag_version(tag: str) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise fail(f"tag {tag!r} must match vMAJOR.MINOR.PATCH")
    return match.group(1)


def source_metadata(version: str, revision: str) -> dict[str, str]:
    if not VERSION_PATTERN.fullmatch(version):
        raise fail(f"artifact version is not a release semantic version: {version!r}")
    if not revision:
        raise fail("source revision must not be empty")
    return {
        "license": LICENSE,
        "source_repository": REPOSITORY,
        "source_revision": revision,
        "version": version,
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def stamp_python(
    root: Path, version: str, revision: str, output_dir: Path
) -> Path:
    """Copy and stamp the Python tree without mutating the checkout."""
    version = checked_version(root, version)
    source_dir = root / "python"
    shutil.copytree(source_dir, output_dir)
    destination = output_dir / "src" / "sil" / "release.json"
    write_json(destination, source_metadata(version, revision))
    return destination


def read_release_metadata(raw: str | bytes, context: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise fail(f"{context}: invalid release.json: {exc}") from exc
    if not isinstance(value, dict):
        raise fail(f"{context}: release.json must contain an object")
    required = {"license", "source_repository", "source_revision", "version"}
    if set(value) != required or any(not isinstance(value[key], str) for key in required):
        raise fail(f"{context}: release.json must contain exactly {sorted(required)}")
    return value


def assert_metadata(
    value: dict[str, str], version: str, revision: str | None, context: str
) -> None:
    expected = source_metadata(version, revision) if revision is not None else None
    if value["version"] != version:
        raise fail(f"{context}: version {value['version']!r} != {version!r}")
    if value["license"] != LICENSE:
        raise fail(f"{context}: license is {value['license']!r}, not {LICENSE!r}")
    if value["source_repository"] != REPOSITORY:
        raise fail(f"{context}: unexpected source repository {value['source_repository']!r}")
    if expected is not None and value != expected:
        raise fail(f"{context}: source metadata does not match the release revision")


def _metadata_message(raw: str, context: str):
    message = Parser(policy=policy.default).parsestr(raw)
    version = message.get("Version")
    if version is None:
        raise fail(f"{context}: distribution metadata has no Version field")
    license_expression = message.get("License-Expression") or message.get("License")
    if license_expression != LICENSE:
        raise fail(f"{context}: distribution license is {license_expression!r}")
    repository = message.get_all("Project-URL", [])
    if not any(REPOSITORY in item for item in repository):
        raise fail(f"{context}: distribution metadata has no repository URL")
    return message, version


def _wheel_release(wheel: Path) -> tuple[dict[str, str], str]:
    context = f"wheel {wheel.name}"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_names = sorted(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if len(metadata_names) != 1:
            raise fail(f"{context}: expected one dist-info/METADATA")
        if "sil/release.json" not in names:
            raise fail(f"{context}: missing sil/release.json")
        message, version = _metadata_message(
            archive.read(metadata_names[0]).decode(), context
        )
        release = read_release_metadata(archive.read("sil/release.json"), context)
        for required in ("sil/__init__.py", "LICENSE", "NOTICE"):
            if not any(name == required or name.endswith("/" + required) for name in names):
                raise fail(f"{context}: missing {required}")
        return release, version


def _sdist_release(sdist: Path) -> tuple[dict[str, str], str]:
    context = f"sdist {sdist.name}"
    with tarfile.open(sdist, "r:gz") as archive:
        names = [member.name for member in archive.getmembers()]
        metadata_names = [name for name in names if name.endswith("/pyproject.toml")]
        release_names = [name for name in names if name.endswith("/src/sil/release.json")]
        if len(metadata_names) != 1 or len(release_names) != 1:
            raise fail(f"{context}: missing pyproject.toml or src/sil/release.json")
        pyproject = tomllib.loads(archive.extractfile(metadata_names[0]).read().decode())
        version = pyproject.get("project", {}).get("version")
        release = read_release_metadata(
            archive.extractfile(release_names[0]).read(), context
        )
        for suffix in ("/LICENSE", "/NOTICE"):
            if not any(name.endswith(suffix) for name in names):
                raise fail(f"{context}: missing {suffix[1:]}")
        return release, version


def validate_distributions(
    root: Path, dist: Path, version: str, revision: str | None
) -> list[Path]:
    version = checked_version(root, version)
    wheels = sorted(dist.glob(f"sil-{version}-*.whl"))
    sdists = sorted(dist.glob(f"sil-{version}.tar.gz"))
    if len(wheels) != 1:
        raise fail(f"expected exactly one sil-{version}-*.whl in {dist}")
    if len(sdists) != 1:
        raise fail(f"expected exactly one sil-{version}.tar.gz in {dist}")
    for artifact, reader in ((wheels[0], _wheel_release), (sdists[0], _sdist_release)):
        metadata, artifact_version = reader(artifact)
        if artifact_version != version:
            raise fail(f"{artifact.name}: metadata version {artifact_version!r} != {version!r}")
        assert_metadata(metadata, version, revision, artifact.name)
    return [wheels[0], sdists[0]]


REQUIRED_NATIVE_PATHS = (
    "bin/sil-run",
    "bin/silschema",
    "include/sil/arena.h",
    "include/sil/clock_region.h",
    "include/sil/participant.h",
    "share/licenses/sil/LICENSE",
    "share/licenses/sil/NOTICE",
    "share/licenses/sil/THIRD-PARTY-NOTICES.md",
    "share/sil/release.json",
)


def validate_native_prefix(
    prefix: Path, version: str, revision: str | None = None
) -> None:
    for relative in REQUIRED_NATIVE_PATHS:
        if not (prefix / relative).is_file():
            raise fail(f"native prefix is missing {relative}")
    if not (prefix / "lib" / "libsil_clock_shim.so").is_file():
        raise fail("native prefix is missing libsil_clock_shim.so")
    metadata = read_release_metadata(
        (prefix / "share/sil/release.json").read_text(), "native prefix"
    )
    assert_metadata(metadata, version, revision, "native prefix")


def _tar_add(archive: tarfile.TarFile, path: Path, name: str) -> None:
    info = archive.gettarinfo(str(path), arcname=name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if info.isreg():
        with path.open("rb") as source:
            archive.addfile(info, source)
    else:
        archive.addfile(info)


def make_native_archive(prefix: Path, output: Path, version: str) -> Path:
    """Create a deterministic Linux x86-64 development archive."""
    validate_native_prefix(prefix, version)
    root_name = f"sil-native-dev-{version}-linux-x86_64"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        with gzip.GzipFile(
            filename="", fileobj=stream, mode="wb", mtime=0
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                directories = sorted(
                    (path for path in prefix.rglob("*") if path.is_dir()),
                    key=lambda path: path.relative_to(prefix).as_posix(),
                )
                files = sorted(
                    (path for path in prefix.rglob("*") if not path.is_dir()),
                    key=lambda path: path.relative_to(prefix).as_posix(),
                )
                _tar_add(archive, prefix, root_name)
                for path in directories:
                    relative = path.relative_to(prefix).as_posix()
                    _tar_add(archive, path, f"{root_name}/{relative}")
                for path in files:
                    relative = path.relative_to(prefix).as_posix()
                    _tar_add(archive, path, f"{root_name}/{relative}")
    return output


def validate_native_archive(
    archive_path: Path, version: str, revision: str | None = None
) -> str:
    context = f"native archive {archive_path.name}"
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise fail(f"{context}: archive is empty")
        roots = {member.name.split("/", 1)[0] for member in members}
        if len(roots) != 1:
            raise fail(f"{context}: expected one archive root")
        root = next(iter(roots))
        expected_root = f"sil-native-dev-{version}-linux-x86_64"
        if root != expected_root:
            raise fail(f"{context}: archive root {root!r} != {expected_root!r}")
        names = {member.name.removeprefix(root + "/") for member in members}
        for required in REQUIRED_NATIVE_PATHS:
            if required not in names:
                raise fail(f"{context}: missing {required}")
        if "lib/libsil_clock_shim.so" not in names:
            raise fail(f"{context}: missing the installed Clock shim library")
        metadata_member = archive.extractfile(f"{root}/share/sil/release.json")
        assert metadata_member is not None
        metadata = read_release_metadata(metadata_member.read(), context)
        assert_metadata(metadata, version, revision, context)
        return root


def validate_runner(runner: Path, version: str, revision: str | None = None) -> None:
    try:
        report = subprocess.run(
            [str(runner), "--version"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise fail(f"cannot run {runner} --version: {exc}") from exc
    if report.stdout.strip() != version or report.stderr:
        raise fail(
            f"runner version report {report.stdout.strip()!r} does not equal {version!r}"
        )
    try:
        info = json.loads(
            subprocess.run(
                [str(runner), "--build-info"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise fail(f"cannot read {runner} build metadata: {exc}") from exc
    metadata = read_release_metadata(json.dumps(info), f"runner {runner}")
    assert_metadata(metadata, version, revision, f"runner {runner}")


def validate_image(image: str, version: str, revision: str | None = None) -> dict:
    try:
        report = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            check=True,
        )
        document = json.loads(report.stdout)[0]
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError) as exc:
        raise fail(f"cannot inspect image {image}: {exc}") from exc
    labels = document.get("Config", {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.version") != version:
        raise fail(f"image version label does not equal {version!r}")
    if labels.get("org.opencontainers.image.licenses") != LICENSE:
        raise fail("image does not carry the Apache-2.0 license label")
    if labels.get("org.opencontainers.image.source") != REPOSITORY:
        raise fail("image source label does not identify the repository")
    if revision is not None and labels.get("org.opencontainers.image.revision") != revision:
        raise fail("image source revision label does not match the release")
    return document


def render_notes(template: Path, output: Path, values: dict[str, str]) -> Path:
    text = template.read_text()
    for key, value in values.items():
        text = text.replace("@" + key + "@", value)
    unresolved = sorted(set(re.findall(r"@[A-Z_]+@", text)))
    if unresolved:
        raise fail(f"release notes contain unresolved placeholders: {', '.join(unresolved)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    return output


def validate_release_title(title: str, version: str) -> None:
    if title != version:
        raise fail(f"release title {title!r} does not equal version {version!r}")


def write_release_metadata(
    output: Path,
    version: str,
    revision: str,
    image_reference: str,
    image_digest: str,
) -> Path:
    value = source_metadata(version, revision)
    value.update(
        {
            "image_reference": image_reference,
            "image_digest": image_digest,
        }
    )
    write_json(output, value)
    return output


def write_checksums(directory: Path) -> Path:
    filename = "SHA256SUMS"
    paths = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.name != filename
    )
    if not paths:
        raise fail(f"no release files found in {directory}")
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in paths
    ]
    destination = directory / filename
    destination.write_text("\n".join(lines) + "\n")
    return destination


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    sub = command.add_subparsers(dest="command", required=True)

    def root_arg(parser_: argparse.ArgumentParser) -> None:
        parser_.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])

    project = sub.add_parser("project-version")
    root_arg(project)

    validate_tag_parser = sub.add_parser("validate-tag")
    root_arg(validate_tag_parser)
    validate_tag_parser.add_argument("--tag", required=True)

    stamp = sub.add_parser("stamp-python")
    root_arg(stamp)
    stamp.add_argument("--output-dir", type=Path, required=True)
    stamp.add_argument("--version", required=True)
    stamp.add_argument("--source-revision", required=True)

    distributions = sub.add_parser("validate-distributions")
    root_arg(distributions)
    distributions.add_argument("--dist", type=Path, required=True)
    distributions.add_argument("--version", required=True)
    distributions.add_argument("--source-revision")

    native = sub.add_parser("make-native-archive")
    native.add_argument("--prefix", type=Path, required=True)
    native.add_argument("--output", type=Path, required=True)
    native.add_argument("--version", required=True)

    native_check = sub.add_parser("validate-native-archive")
    native_check.add_argument("--archive", type=Path, required=True)
    native_check.add_argument("--version", required=True)
    native_check.add_argument("--source-revision")

    runner = sub.add_parser("validate-runner")
    runner.add_argument("--runner", type=Path, required=True)
    runner.add_argument("--version", required=True)
    runner.add_argument("--source-revision")

    image = sub.add_parser("validate-image")
    image.add_argument("--image", required=True)
    image.add_argument("--version", required=True)
    image.add_argument("--source-revision")

    notes = sub.add_parser("render-notes")
    notes.add_argument("--template", type=Path, required=True)
    notes.add_argument("--output", type=Path, required=True)
    notes.add_argument("--version", required=True)
    notes.add_argument("--source-revision", required=True)
    notes.add_argument("--image-reference", required=True)
    notes.add_argument("--image-digest", required=True)

    title = sub.add_parser("validate-release-title")
    title.add_argument("--title", required=True)
    title.add_argument("--version", required=True)

    metadata = sub.add_parser("write-release-metadata")
    metadata.add_argument("--output", type=Path, required=True)
    metadata.add_argument("--version", required=True)
    metadata.add_argument("--source-revision", required=True)
    metadata.add_argument("--image-reference", required=True)
    metadata.add_argument("--image-digest", required=True)

    checksums = sub.add_parser("checksums")
    checksums.add_argument("--directory", type=Path, required=True)

    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "project-version":
            print(project_version(args.root))
        elif args.command == "validate-tag":
            version = tag_version(args.tag)
            if version != project_version(args.root):
                raise fail(f"tag version {version!r} does not match project version")
            print(version)
        elif args.command == "stamp-python":
            print(
                stamp_python(
                    args.root,
                    args.version,
                    args.source_revision,
                    args.output_dir,
                )
            )
        elif args.command == "validate-distributions":
            for artifact in validate_distributions(
                args.root, args.dist, args.version, args.source_revision
            ):
                print(artifact)
        elif args.command == "make-native-archive":
            print(make_native_archive(args.prefix, args.output, args.version))
        elif args.command == "validate-native-archive":
            validate_native_archive(args.archive, args.version, args.source_revision)
        elif args.command == "validate-runner":
            validate_runner(args.runner, args.version, args.source_revision)
        elif args.command == "validate-image":
            validate_image(args.image, args.version, args.source_revision)
        elif args.command == "render-notes":
            values = {
                "VERSION": args.version,
                "SOURCE_REVISION": args.source_revision,
                "IMAGE_REFERENCE": args.image_reference,
                "IMAGE_DIGEST": args.image_digest,
            }
            print(render_notes(args.template, args.output, values))
        elif args.command == "validate-release-title":
            validate_release_title(args.title, args.version)
        elif args.command == "write-release-metadata":
            print(
                write_release_metadata(
                    args.output,
                    args.version,
                    args.source_revision,
                    args.image_reference,
                    args.image_digest,
                )
            )
        elif args.command == "checksums":
            print(write_checksums(args.directory))
        else:  # pragma: no cover - argparse enforces the command set
            raise fail(f"unknown command {args.command}")
    except (OSError, ReleaseError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
